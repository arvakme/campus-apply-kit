// scrape.mjs · 打开 Moka 申请页 → 传简历等解析 → 按 apply-field 容器抓全表字段（含下拉选项）
// ego 的 nodejs 不继承外部环境变量，参数由 run.sh 替换占位符传入；lib.mjs 由 run.sh 拼在本文件前面
const SPACE = "__FM_SPACE__", URL_ = "__FM_URL__", RESUME = "__FM_RESUME__", PAGE = "__FM_PAGE__";

// Moka（sd- 组件库）表单结构，class 带构建哈希，只能按前缀匹配：
//   div.apply-field-<h>.<type>_info-<h>  >  div.title-<h>（字段名） + span.required-asterisk-<h> + div.ctrl-<h>（控件）
const spaces = await listTaskSpaces();
const sp = spaces.find(s => s.name === SPACE);
if (!sp) console.log("FM_JSON " + JSON.stringify({ error: `找不到空间 ${SPACE}` }));
else if (sp.ownership !== "agent") console.log("FM_JSON " + JSON.stringify({ error: `空间 ${SPACE} 在用户手里（${sp.ownership}），等用户点 Return to agent 后再跑` }));
else {
  const task = await taskSpace(sp.id);
  page = await pickPage(task, URL_, PAGE);
  // 已经停在这个职位的申请页就不再跳转：goto 同一地址可能整页重载，把 worker 已填的内容冲掉
  const here = await page.evaluate(() => location.href).catch(() => "");
  if (!here.includes(URL_.split("#")[1] || URL_)) await page.goto(URL_);
  // SPA 渲染慢，轮询到表单项出现（最多 20 秒）
  let n = 0;
  for (let i = 0; i < 20 && !n; i++) {
    await page.waitForTimeout(1000);
    n = await page.evaluate(() => document.querySelectorAll('[class*="apply-field-"]').length);
  }
  if (!n) console.log("FM_JSON " + JSON.stringify({ error: "页面上没有申请表单项：确认链接是『职位申请页』(#/job/<id>/apply) 且已登录" }));
  else {
    // 简历：专用控件 input[name=resumeKey]（另一个 file input 是照片，不能混）
    let uploaded = false, uploadNote = "";
    if (RESUME) {
      const has = await page.evaluate(() => { const f = document.querySelector('input[name="resumeKey"]'); return f ? (f.value ? "filled" : "empty") : "none"; });
      if (has === "empty") {
        try {
          await page.setInputFiles('input[name="resumeKey"]', RESUME);
          // 等解析回填：姓名出现值或 12 秒
          for (let i = 0; i < 12; i++) {
            await page.waitForTimeout(1000);
            const ok = await page.evaluate(() => { const f = [...document.querySelectorAll('[class*="apply-field-"]')].find(x => (x.querySelector('[class*="title-"]')?.innerText || '').trim().startsWith('姓名')); return !!(f && f.querySelector('input')?.value); });
            if (ok) break;
          }
          uploaded = true;
        } catch (e) { uploadNote = "简历上传失败：" + String(e).split("\n")[0].slice(0, 80); }
      } else if (has === "filled") { uploaded = true; uploadNote = "页面已有简历，未重传（重传会覆盖已改字段）"; }
      else uploadNote = "这个租户没有简历上传控件（纯在线表单）";
    }

    const fields = await page.evaluate(() => {
      const norm = s => (s || '').replace(/\*/g, '').replace(/\s+/g, ' ').trim();
      return [...document.querySelectorAll('[class*="apply-field-"]')].map((f, idx) => {
        const title = norm(f.querySelector('[class*="title-"]')?.innerText);
        const typeCls = (f.className.toString().split(' ').find(c => /_info-/.test(c)) || '').replace(/-[A-Za-z0-9_]+$/, '');
        const ctrl = f.querySelector('[class*="ctrl-"]') || f;
        // 真正能打字的输入框：排除下拉自带的那个 input（手机号 = 区号下拉 + 号码输入框，证件号 = 证件类型下拉 + 号码输入框）
        const typed = [...ctrl.querySelectorAll('textarea, input')].find(i => !i.readOnly && i.type !== 'file' && i.type !== 'checkbox' && !i.closest('[class*="sd-Select-container"]'));
        const selects = [...ctrl.querySelectorAll('[class*="sd-Select-container"]')];
        const shown = el => norm(el?.querySelector('[class*="sd-Input-display-value"]')?.innerText);
        let kind = 'other';
        if (ctrl.querySelector('input[type=file]')) kind = 'file';
        else if (/^(day|date|month|time|period)[a-z_]*_info$/.test(typeCls) || /(date|month)-range|range-select/.test(ctrl.innerHTML.slice(0, 400))) kind = 'date';   // 起止时间里带「至今」勾选框，必须先于 checkbox 判
        else if (typeCls === 'location_info') kind = 'cascader';
        else if (typeCls === 'multi_select_info') kind = 'multiselect';
        else if (typeCls === 'confirm_info' || ctrl.querySelector('[type=checkbox]')) kind = 'checkbox';
        else if (typeCls === 'text_info' || ctrl.querySelector('textarea')) kind = 'textarea';
        else if (typed) kind = typed.closest('[class*="sd-Dropdown-container"]') ? 'typeahead' : 'input';   // typeahead = 学校/专业库这类「边输边搜」
        else if (selects.length) kind = 'select';
        const block = f.closest('[class*="apply-block-"]');
        const head = block?.querySelector('[class*="blockTitle"]');
        const rows = block ? [...block.querySelectorAll('[class*="apply-fields-"]')] : [];
        const rowEl = f.closest('[class*="apply-fields-"]');
        const out = { idx, label: title, typeCls, type: kind, required: !!f.querySelector('[class*="required-asterisk"]'),
          section: norm(head?.innerText).replace(/\s*添加\s*$/, ''), repeatable: /添加/.test(head?.innerText || '') || /multi/.test(rowEl?.className?.toString() || ''), row: Math.max(0, rows.indexOf(rowEl)) };
        if (kind === 'input' || kind === 'textarea' || kind === 'typeahead') { out.value = norm(typed?.value); if (selects.length) out.prefix = shown(selects[0]); }
        else if (kind === 'date') { out.parts = selects.map(x => x.querySelector('input')?.placeholder || '');
          if (out.parts.some(p => !p)) out.parts = ({ 2: ['年', '月'], 3: ['年', '月', '日'], 4: ['年', '月', '年', '月'], 6: ['年', '月', '日', '年', '月', '日'] })[selects.length] || out.parts;   /* 选过值的下拉 placeholder 是空的，按个数推 */
          out.partValues = selects.map(shown); out.value = out.partValues.filter(Boolean).join('-'); if (!selects.length) { out.picker = true; out.value = norm(ctrl.querySelector('input')?.value); } }
        else if (kind === 'cascader' || kind === 'multiselect') out.value = norm([...ctrl.querySelectorAll('[class*="sd-Tag-text"]')].map(e => e.innerText).join(' ')) || norm(ctrl.querySelector('input[readonly]')?.value);   // 地区选完后值在只读 input 里
        else out.value = selects.map(shown).filter(Boolean).join(' ');
        out.value = (out.value || '').slice(0, 80);
        return out;
      }).filter(f => f.label);
    });

    // 空着的下拉：逐个展开读选项，映射时要拿选项去对答案
    for (const f of fields) {
      if (f.type !== 'select' || f.value) continue;
      try { f.options = (await openDropdown(f.idx, 0, f.label)) ? await popupOptions() : []; }
      catch (e) { f.options = []; }
      await closePopups();
    }
    console.log("FM_JSON " + JSON.stringify({ page: page.label, uploaded, uploadNote, fields }));
  }
}
