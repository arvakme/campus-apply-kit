// map-fields.mjs · 表单字段 → 问题库条目 → 要填的值/要选的选项（Jev / Vercel AI Gateway，批量请求）
// 输入：stdin {fields:[{idx,label,type,required,value,options?,parts?,partValues?,section,repeatable,row}]}；环境变量 FM_QB=问题库 JSON（id/question/aliases/answer/kind/category）
// 输出：stdout {plan:[...], mismatches:[...], notes:[...], usage, cost, degraded}
//
// 三条车道，宁可少填不可填错：
//   flat    不重复的分组（个人信息、求职意向…）：字段名 → 类别 → 条目
//   edu     教育经历的每一行：先认出这一行是硕士还是本科，再只在该阶段的条目里找；认不出就整行交回 worker
//   records 其它可重复分组（实习/项目/获奖/家庭成员…）：问题库是扁平的，对不上「第几段」，一律不碰，只汇总给 worker
// 答案不会整体发给 Jev：只发字段名与条目标题；仅下拉选项匹配时才发该条答案。
import { experimental_evaluate as evaluate } from 'ai';
import fs from 'node:fs';

const MIN_CONFIDENCE = 0.8;
const BATCH = 24;
const AUTO_TYPES = new Set(['input', 'textarea', 'select', 'date', 'cascader']);   // 脚本会动手的控件；其余（多选、边输边搜）只给 worker 提示
const MODEL = 'typesafe-ai/jev';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const qb = JSON.parse(fs.readFileSync(process.env.FM_QB, 'utf8')).filter(e => (e.answer || '').trim());
const fields = (input.fields || []).filter(f => f.label && !['file', 'checkbox'].includes(f.type));
const out = { plan: [], mismatches: [], notes: [], usage: 0, cost: 0, degraded: false };

if (!process.env.AI_GATEWAY_API_KEY) { out.degraded = true; out.reason = '没有 AI_GATEWAY_API_KEY，跳过映射'; console.log(JSON.stringify(out)); process.exit(0); }

const brief = e => `${e.question}${e.aliases?.length ? '（又叫：' + e.aliases.join('/') + '）' : ''}`.slice(0, 70);
const cats = [...new Set(qb.map(e => e.category || '其他'))];
const CAT_DESC = { 身份: '姓名/性别/出生/证件/民族/籍贯/政治面貌/婚姻/身高体重', 联系: '手机/邮箱/微信/地址/现居地', 教育: '学校/学历/学位/专业/就读时间/成绩/英语/证书编号',
  偏好: '期望薪资/城市/到岗时间/是否接受调剂/信息来源渠道', 家庭: '父母/家庭成员/独生子女', 声明: '疾病/不良记录/亲属回避/处分/材料附件', 经历: '实习/项目/获奖/技能/自我评价/爱好/校内职务', 账号: '登录邮箱/密码' };
const batches = (arr, n) => Array.from({ length: Math.ceil(arr.length / n) }, (_, i) => arr.slice(i * n, i * n + n));
const top = a => a.probabilities ? Math.max(...Object.values(a.probabilities)) : 0;
const norm = s => String(s || '').replace(/[\s·,，。/／\-—_()（）]/g, '').toLowerCase();
const same = (a, b) => !!norm(a) && !!norm(b) && (norm(a).includes(norm(b)) || norm(b).includes(norm(a)));

// 字面值：value/secret 条目取批注前的部分；手机、证件、邮箱按格式抽
const literalOf = (entry, label = '') => {
  const a = String(entry.answer || '');
  if (/手机|电话/.test(label)) { const m = a.match(/1[3-9]\d{9}/); if (m) return m[0]; }
  if (/证件|身份证/.test(label)) { const m = a.match(/\d{17}[\dXx]/); if (m) return m[0]; }
  if (/邮箱|email/i.test(label)) { const m = a.match(/[\w.+-]+@[\w-]+\.[\w.]+/); if (m) return m[0]; }
  const lit = a.split(/[（(；;]/)[0].replace(/\*\*/g, '').trim();
  return lit.length <= 60 ? lit : '';
};
const isList = a => /[①②③]|(^|\n)\s*\d+[.、)]/.test(String(a));                       // 多条并列的答案（获奖列表等）不能塞进单行输入框
// 是/否 类下拉：问题库写「接受」、页面显示「是」算一致
const polarity = s => /^(否|不|无|没有|未|非)/.test(norm(s)) ? -1 : /^(是|有|接受|愿意|同意|可以|服从|能)/.test(norm(s)) ? 1 : 0;
// 规则条目里的优先级链（如「城市A>城市B>城市C」）：下拉里有哪个就按顺序选哪个，这是确定性的，不用问模型
const chainOf = entry => { const m = String(entry.answer || '').match(/[一-龥A-Za-z]+(?:\s*>\s*[一-龥A-Za-z]+)+/); return m ? m[0].split('>').map(x => x.trim()) : null; };
const datesOf = s => [...String(s || '').matchAll(/((?:19|20)\d{2})\s*[-./年]\s*(\d{1,2})(?:\s*[-./月]\s*(\d{1,2}))?/g)].map(m => ({ y: m[1], m: String(+m[2]), d: m[3] ? String(+m[3]) : '' }));
// 页面上年/月(/日) 下拉当前显示的值 → 日期数组（起止时间是两组）
const pageDates = f => { const pv = f.partValues || [], ps = f.parts || [], res = []; let cur = null;
  ps.forEach((p, i) => { if (p === '年') { cur = { y: pv[i] || '', m: '', d: '' }; res.push(cur); } else if (cur && p === '月') cur.m = String(+pv[i] || ''); else if (cur && p === '日') cur.d = String(+pv[i] || ''); });
  return res; };

// 教育阶段：条目靠 id/问题里的 master|bachelor|硕士|本科 归阶段；没标的算通用
const stageOfEntry = e => /high-school|gaokao/.test(e.id) || /高中|高考/.test(e.question) ? 'highschool'
  : /bachelor/.test(e.id) || /本科|学士/.test(e.question) ? 'bachelor'
  : /master|highest/.test(e.id) || /硕士|研究生|最高学[历位]/.test(e.question) ? 'master' : null;
const STAGE_CN = { master: '硕士', bachelor: '本科' };
const schoolLit = { master: literalOf(qb.find(e => e.id === 'school-master') || {}), bachelor: literalOf(qb.find(e => e.id === 'school-bachelor') || {}) };
const stageOfRow = rowFields => {
  const val = re => rowFields.filter(f => re.test(f.label)).map(f => f.value).find(Boolean) || '';
  const school = val(/学校|院校/);
  for (const st of ['master', 'bachelor']) if (schoolLit[st] && same(school, schoolLit[st])) return st;
  const level = val(/^学历|学历层次|^学位/);
  if (/硕士|研究生/.test(level)) return 'master';
  if (/本科|学士/.test(level)) return 'bachelor';
  return null;
};

try {
  const flat = fields.filter(f => !f.repeatable);
  const edu = fields.filter(f => f.repeatable && /教育|学历|学习经历/.test(f.section || ''));
  const records = fields.filter(f => f.repeatable && !edu.includes(f));

  // 教育经历：逐行认阶段
  const stageOf = {};                                                          // idx → 'master' | 'bachelor' | null
  const eduRows = {};
  for (const f of edu) (eduRows[f.section + '#' + f.row] ||= []).push(f);
  const seenStages = [];
  for (const [key, rowFields] of Object.entries(eduRows)) {
    const st = stageOfRow(rowFields); if (st) seenStages.push(st);
    rowFields.forEach(f => { stageOf[f.idx] = st; });
    if (!st) out.notes.push(`「${key.split('#')[0]}」第 ${+key.split('#')[1] + 1} 行认不出是硕士还是本科（学校/学历都空或对不上问题库），整行交回 worker`);
  }
  if (edu.length) for (const st of ['master', 'bachelor']) if (schoolLit[st] && !seenStages.includes(st)) out.notes.push(`教育经历里没有${STAGE_CN[st]}（${schoolLit[st]}）那一行：需要 worker 点「添加」补一行`);
  const candidatesFor = f => stageOf[f.idx] ? qb.filter(e => (e.category || '其他') === '教育' && [null, stageOf[f.idx]].includes(stageOfEntry(e))) : null;

  // ⓪ 字段名和某条目的问题/别名一字不差（且只命中一条）：直接对上，不问模型。给问题库补别名 = 让这个字段以后稳定命中
  const catOf = {}, entryOf = {};
  for (const f of [...flat, ...edu.filter(f => stageOf[f.idx])]) {
    const pool = candidatesFor(f) || qb;
    const hits = pool.filter(e => [e.question, ...(e.aliases || [])].some(a => norm(a) === norm(f.label)));
    if (hits.length === 1) { catOf[f.idx] = { cat: hits[0].category || '其他', p: 1 }; entryOf[f.idx] = { id: hits[0].id, p: 1, how: '别名一致' }; }
  }
  // 教育行里由阶段直接推得的是否题：只在问题库里能确定「最高 / 第一」是哪一段时才推（只认本科、硕士；
  // 有博士、专升本等其它阶段条目时不推，交回 worker）
  const derived = {};
  const hasOther = qb.some(e => (e.category || '') === '教育' && /phd|doctor|博士|专科|专升本/i.test(e.id + e.question));
  const highest = hasOther ? null : schoolLit.master ? 'master' : schoolLit.bachelor ? 'bachelor' : null;
  const first = hasOther ? null : schoolLit.bachelor ? 'bachelor' : null;
  for (const f of edu) {
    const st = stageOf[f.idx]; if (!st || entryOf[f.idx]) continue;
    if (highest && /是否.*最高(学历|学位)/.test(f.label)) derived[f.idx] = st === highest ? '是' : '否';
    else if (first && /是否.*第一学历/.test(f.label)) derived[f.idx] = st === first ? '是' : '否';
  }

  // ① flat 字段 → 类别
  for (const chunk of batches(flat.filter(f => !entryOf[f.idx]), BATCH)) {
    const questions = Object.fromEntries(chunk.map(f => ['f' + f.idx, {
      type: 'choice', instructions: `校招申请表「${f.section || '基本信息'}」分组里的字段「${f.label}」要填候选人的哪一类信息？`,
      criteria: { ...Object.fromEntries(cats.map(c => [c, CAT_DESC[c] || c])), __none__: '都不是（公司自定义问题等）' },
    }]));
    const r = await evaluate({ model: MODEL, state: { 场景: '中国校园招聘申请表（Moka 门户）', 字段: chunk.map(f => f.label) }, questions });
    out.usage += r.usage.inputTokens;
    chunk.forEach(f => { const a = r.answers['f' + f.idx]; catOf[f.idx] = { cat: a.choice, p: top(a) }; });
  }
  // ② 类别内（教育行：该阶段的教育条目内）→ 具体条目
  edu.filter(f => stageOf[f.idx] && !entryOf[f.idx] && !(f.idx in derived)).forEach(f => { catOf[f.idx] = { cat: '教育', p: 1 }; });
  const need = [...flat, ...edu].filter(f => catOf[f.idx] && !entryOf[f.idx] && catOf[f.idx].cat !== '__none__');
  for (const chunk of batches(need, BATCH)) {
    const questions = Object.fromEntries(chunk.map(f => {
      const list = candidatesFor(f) || qb.filter(e => (e.category || '其他') === catOf[f.idx].cat);
      const where = stageOf[f.idx] ? `教育经历里「${STAGE_CN[stageOf[f.idx]]}阶段」这一行的字段` : `「${f.section || '基本信息'}」分组里的字段`;
      return ['f' + f.idx, { type: 'choice', instructions: `${where}「${f.label}」（控件 ${f.type}）对应候选人档案里的哪一项？`,
        criteria: { ...Object.fromEntries(list.map(e => [e.id, brief(e)])), __none__: '档案里没有对应项' } }];
    }));
    const r = await evaluate({ model: MODEL, state: { 场景: '中国校园招聘申请表（Moka 门户）', 候选人: '应届毕业生' }, questions });
    out.usage += r.usage.inputTokens;
    chunk.forEach(f => { const a = r.answers['f' + f.idx]; entryOf[f.idx] = { id: a.choice, p: top(a) }; });
  }
  // ③ 下拉：在选项里挑答案（先字面/优先级链，匹配不上再问 Jev）
  const optionOf = {};
  const askOpt = [];
  for (const f of [...flat, ...edu]) {
    if (f.type !== 'select' || f.value || !(f.options || []).length) continue;
    if (f.idx in derived) { const o = f.options.find(x => norm(x) === norm(derived[f.idx])); if (o) optionOf[f.idx] = { option: o, p: 1, how: '由教育阶段推得' }; continue; }
    const m = entryOf[f.idx]; if (!m || m.id === '__none__' || m.p < MIN_CONFIDENCE) continue;
    const e = qb.find(x => x.id === m.id); const lit = literalOf(e, f.label);
    const chain = e.kind === 'rule' && chainOf(e);
    if (chain) {
      const pick = chain.map(c => f.options.find(o => norm(o).includes(norm(c)))).find(Boolean);
      if (pick) optionOf[f.idx] = { option: pick, p: 1, how: '按优先级 ' + chain.join('>') };
      continue;                                                              // 链上的都不在选项里：交回 worker，不让模型猜
    }
    const exact = lit && f.options.find(o => norm(o) === norm(lit));
    const loose = lit && f.options.filter(o => norm(o).includes(norm(lit)) || (norm(lit).includes(norm(o)) && norm(o).length >= 2));
    if (exact) optionOf[f.idx] = { option: exact, p: 1, how: '字面一致' };
    else if (loose && loose.length === 1) optionOf[f.idx] = { option: loose[0], p: 0.95, how: '字面包含' };
    else askOpt.push({ f, e });
  }
  for (const chunk of batches(askOpt, 12)) {
    const questions = Object.fromEntries(chunk.map(({ f, e }) => ['f' + f.idx, {
      type: 'choice', instructions: `字段「${f.label}」是下拉框。候选人档案这一项写的是：「${e.answer.slice(0, 160)}」。应该选哪个选项？`,
      criteria: { ...Object.fromEntries(f.options.map((o, i) => ['o' + i, o])), __none__: '没有合适的选项' },
    }]));
    const r = await evaluate({ model: MODEL, state: { 场景: '校招申请表下拉选项匹配' }, questions });
    out.usage += r.usage.inputTokens;
    chunk.forEach(({ f }) => { const a = r.answers['f' + f.idx]; if (a.choice !== '__none__') optionOf[f.idx] = { option: f.options[Number(a.choice.slice(1))], p: top(a), how: 'Jev 选项匹配' }; });
  }

  for (const f of fields) {
    const row = { idx: f.idx, label: f.label, type: f.type, required: f.required, section: f.section || '', qb: null, confidence: 0, action: 'none', reason: null };
    if (f.repeatable) row.row = f.row + 1;
    if (records.includes(f)) { row.lane = 'records'; row.hasValue = !!f.value; row.reason = '分段经历由 worker 对照简历核对'; out.plan.push(row); continue; }
    if (edu.includes(f)) { row.lane = 'edu'; row.stage = stageOf[f.idx]; if (!stageOf[f.idx]) { row.hasValue = !!f.value; row.reason = '这一行认不出教育阶段'; out.plan.push(row); continue; } }
    if (f.idx in derived) {
      row.confidence = 1; row.qb = '(由教育阶段推得)';
      if (f.value) { row.action = 'keep'; if (polarity(f.value) !== polarity(derived[f.idx])) out.mismatches.push({ label: `${f.label}（${STAGE_CN[row.stage]}行）`, 页面值: f.value, 问题库: derived[f.idx], qb: row.qb }); }
      else if (optionOf[f.idx]) { row.action = 'select'; row.option = optionOf[f.idx].option; row.how = optionOf[f.idx].how; }
      else { row.reason = '是否题不是下拉或没有「是/否」选项'; row.hint = derived[f.idx]; }
      out.plan.push(row); continue;
    }
    const c = catOf[f.idx], m = entryOf[f.idx];
    if (!m || !c || c.cat === '__none__' || m.id === '__none__') { row.reason = '问题库无对应条目'; out.plan.push(row); continue; }
    const e = qb.find(x => x.id === m.id);
    row.qb = m.id; row.confidence = Number(Math.min(c.p, m.p).toFixed(2)); row.secret = e.kind === 'secret';
    const tag = row.stage ? `（${STAGE_CN[row.stage]}行）` : '';
    const hint = n => row.secret ? '见问题库 ' + m.id : e.answer.slice(0, n);
    const lit = literalOf(e, f.label);
    const want = f.type === 'date' ? datesOf(e.answer.split(/[（(；;]/)[0]) : [];
    if (row.confidence < MIN_CONFIDENCE) row.reason = `置信度 ${row.confidence}`;
    else if (f.value) {                                                      // 已有值（多为简历解析带入）：只核对，不覆盖
      row.action = 'keep';
      if (f.type === 'date') { const have = f.picker ? datesOf(f.value) : pageDates(f); if (want.length && (have.length !== want.length || want.some((w, i) => w.y !== have[i].y || w.m !== have[i].m))) out.mismatches.push({ label: f.label + tag, 页面值: row.secret ? '〈已掩码〉' : f.value, 问题库: row.secret ? '〈已掩码，见问题库 ' + m.id + '〉' : want.map(w => `${w.y}-${w.m}`).join(' 至 '), qb: m.id }); }
      else if (lit && polarity(lit) && polarity(lit) === polarity(f.value)) { /* 是否类，同向即一致 */ }
      else if (f.type === 'cascader' && lit) {                                // 地区：页面是「广东省 广州市 …」，问题库可能写「广东广州」，按去掉省市区后缀的词比
        const cores = f.value.split(/[\s/／\-—]+/).map(t => t.replace(/(省|市|自治区|特别行政区|地区|区|县)$/, '')).filter(t => t.length >= 2);
        if (!cores.some(t => norm(lit).includes(norm(t)))) out.mismatches.push({ label: f.label + tag, 页面值: row.secret ? '〈已掩码〉' : f.value, 问题库: row.secret ? '〈已掩码〉' : lit, qb: m.id });
      }
      else if (lit && e.kind !== 'rule' && !same(f.value, lit) && !norm(e.answer).includes(norm(f.value))) out.mismatches.push({ label: f.label + tag, 页面值: row.secret ? '〈已掩码〉' : f.value, 问题库: row.secret ? '〈已掩码〉' : lit, qb: m.id });
    }
    else if (!AUTO_TYPES.has(f.type)) { row.reason = `控件类型 ${f.type} 需 worker 操作`; row.hint = hint(80); }
    else if (f.type === 'date') {
      const parts = f.parts || [], years = parts.filter(p => p === '年').length;
      if (f.picker || !parts.length) {
        if (want.length === 1 && want[0].d) { row.action = 'day'; row.dates = want; }
        else { row.reason = '日历控件要精确到日，问题库答案没有「日」'; row.hint = hint(80); }
      }
      else if (!want.length || want.length !== years || !parts.every(p => ['年', '月', '日'].includes(p)) || (parts.includes('日') && want.some(w => !w.d))) { row.reason = '问题库答案和日期控件对不上（个数或精度）'; row.hint = hint(80); }
      else { row.action = 'date'; row.dates = want; row.parts = parts; }
    }
    else if (f.type === 'cascader') {
      if (e.kind !== 'rule' && lit) { row.action = 'cascade'; row.value = lit; }
      else { row.reason = '地区需 worker 判断'; row.hint = hint(80); }
    }
    else if (f.type === 'select') {
      const o = optionOf[f.idx];
      if (o && o.p >= MIN_CONFIDENCE) { row.action = 'select'; row.option = o.option; row.how = o.how; }
      else { row.reason = '选项里没找到对应答案'; row.hint = hint(60) + ' ｜选项：' + (f.options || []).slice(0, 8).join('/'); }
    }
    else if (e.kind === 'rule') { row.reason = '问题库是规则条目，需 worker 判断'; row.hint = e.answer.slice(0, 100); }
    else if (f.type === 'textarea' && !row.secret) { row.action = 'fill'; row.value = e.answer.replace(/\*\*/g, '').trim(); }     // 多行文本：给完整答案（含列表）
    else if (isList(e.answer)) { row.reason = '答案是多条并列，单行输入框放不下，需 worker 取其一'; row.hint = hint(100); }
    else if (!lit) { row.reason = '答案较长或带说明，需 worker 确认'; row.hint = hint(80); }
    else { row.action = 'fill'; row.value = lit; }
    if (entryOf[f.idx]?.how && row.action !== 'none') row.how = row.how || entryOf[f.idx].how;
    out.plan.push(row);
  }
  out.cost = Number((out.usage / 1e6 * 0.042).toFixed(6));
} catch (e) {
  out.degraded = true; out.reason = 'Jev 调用失败：' + String(e.message || e).slice(0, 160); out.plan = [];
}
console.log(JSON.stringify(out));
