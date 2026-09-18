// apply.mjs · 按 plan 往 Moka 表单里写：文本框写值、下拉点选项、年月下拉选日期、地区弹层逐级点；每一项写完都回读校验
// 参数由 run.sh 替换占位符传入（ego 的 nodejs 不继承环境变量）；lib.mjs 由 run.sh 拼在本文件前面
const SPACE = "__FM_SPACE__", DRY = "__FM_DRY__" === "1", PAGE = "__FM_PAGE__" || "p1";
const fs = await import("node:fs");
const plan = JSON.parse(fs.readFileSync("__FM_PLAN__", "utf8"));
const ACTIONS = new Set(["fill", "select", "date", "day", "cascade"]);

const spaces = await listTaskSpaces();
const sp = spaces.find(s => s.name === SPACE);
if (!sp || sp.ownership !== "agent") console.log("FM_JSON " + JSON.stringify({ error: `空间 ${SPACE} 不可用（${sp ? sp.ownership : "不存在"}）` }));
else {
  const task = await taskSpace(sp.id);
  page = task.page(PAGE);
  const done = [], failed = [];
  const show = (r, v) => r.secret ? "〈已掩码〉" : String(v).slice(0, 40);
  const wanted = r => r.action === "fill" ? r.value : r.action === "select" ? r.option : (r.action === "date" || r.action === "day") ? r.dates.map(d => [d.y, d.m, d.d].filter(Boolean).join("-")).join(" 至 ") : r.value;
  const fail = (r, reason) => failed.push({ label: r.label, type: r.type, required: r.required, reason, hint: r.secret ? "见问题库 " + r.qb : show(r, wanted(r)) });

  for (const r of plan.plan || []) {
    if (!ACTIONS.has(r.action)) continue;
    const where = r.stage ? { section: r.section, row: r.row, stage: r.stage } : {};
    if (DRY) { done.push({ label: r.label, ...where, qb: r.qb, action: r.action, value: show(r, wanted(r)), how: r.how, dry: true }); continue; }
    try {
      if (r.action === "fill") {
        await stablePos(r.idx, 0, '[class*="ctrl-"]');
        // React 受控输入：必须走原生 setter + input 事件，直接改 value 不进 state
        const got = await page.evaluate(({ FIELD, idx, value }) => {
          const f = document.querySelectorAll(FIELD)[idx];
          const el = [...f.querySelectorAll("textarea, input")].find(i => !i.readOnly && i.type !== "file" && i.type !== "checkbox" && !i.closest('[class*="sd-Select-container"]'));
          if (!el) return null;
          const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          el.focus();
          Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          el.blur();
          return el.value;
        }, { FIELD, idx: r.idx, value: String(r.value) });
        const complaint = got === String(r.value) ? await settle(r.idx) : "";
        if (complaint) fail(r, "写进去了但表单提示：" + complaint);
        else if (got === String(r.value)) done.push({ label: r.label, ...where, qb: r.qb, action: "fill", value: show(r, r.value) });
        else fail(r, got === null ? "没找到可写的输入框" : "写入后回读不一致");
      } else if (r.action === "select") {
        if (!await openDropdown(r.idx, 0, r.label)) { fail(r, "下拉没展开"); continue; }
        if (!await clickPopupOption(r.option)) { await closePopups(); fail(r, `展开后没找到选项「${r.option}」`); continue; }
        const complaint = await settle(r.idx);
        const shown = await shownValue(r.idx);
        if (complaint) fail(r, `选上了「${shown}」但表单仍提示：${complaint}`);
        else if (shown.includes(r.option) || shown.includes(r.option.split(" ").pop())) done.push({ label: r.label, ...where, qb: r.qb, action: "select", value: show(r, r.option), how: r.how });
        else fail(r, "点选后控件没显示该选项");
      } else if (r.action === "date") {
        // 年/月(/日) 各是一个下拉，起止时间是两组；遇到「年」就换下一个日期。选项文字可能是 2026 / 2026年、12 / 12月 / 12 月
        const forms = { 年: v => [v, v + "年"], 月: v => [String(+v), String(+v).padStart(2, "0"), +v + "月", String(+v).padStart(2, "0") + "月"], 日: v => [String(+v), String(+v).padStart(2, "0"), +v + "日"] };
        let ok = true, di = -1;
        for (let n = 0; n < r.parts.length && ok; n++) {
          const part = r.parts[n];
          if (part === "年") di++;
          const d = r.dates[di] || {}, v = { 年: d.y, 月: d.m, 日: d.d }[part];
          if (!v) { ok = false; fail(r, `日期缺「${part}」`); break; }
          if (!await openDropdown(r.idx, n, r.label + part)) { ok = false; fail(r, `第 ${n + 1} 个「${part}」下拉没展开`); break; }
          if (!await clickPopupOption(forms[part](v), undefined, v + part)) { ok = false; await closePopups(); fail(r, `「${part}」里没有 ${v}`); break; }
        }
        const complaint = await settle(r.idx);
        if (ok) {
          const shown = await shownValue(r.idx);
          if (complaint) fail(r, `选上了「${shown}」但表单仍提示：${complaint}`);
          else if (r.dates.every(d => shown.includes(String(d.y)))) done.push({ label: r.label, ...where, qb: r.qb, action: "date", value: r.secret ? "〈已掩码〉" : shown });
          else fail(r, "选完后控件没显示年份");
        }
      } else if (r.action === "day") {
        const err = await pickDay(r.idx, r.dates[0], r.label) || await settle(r.idx);
        const shown = await shownValue(r.idx);
        const d = r.dates[0];
        const got = shown.replace(/[（(].*?[)）]/g, "").match(/((?:19|20)\d{2})\D+(\d{1,2})(?:\D+(\d{1,2}))?/);   // 控件可能只到「年月」；先去掉「(24岁)」这类括注
        if (!err && got && got[1] === d.y && +got[2] === +d.m && (!got[3] || +got[3] === +d.d)) done.push({ label: r.label, ...where, qb: r.qb, action: "day", value: r.secret ? "〈已掩码〉" : shown });
        else fail(r, err || "选完后控件显示的日期对不上");
      } else if (r.action === "cascade") {
        // 地区弹层：省份 → 城市 → 县区，每级点「名字被答案包含」的那个标签；哪一级对不上就停，回读有值才算成功
        if (!await openDropdown(r.idx, 0, r.label)) { fail(r, "地区弹层没展开"); continue; }
        const answer = String(r.value).replace(/\s/g, "");
        const picked = [];
        for (let level = 0; level < 3; level++) {
          const tags = await popupOptions('[class*="sd-Tag-container"]', 600);
          const core = t => t.replace(/(省|市|自治区|特别行政区|壮族|回族|维吾尔|地区|区|县)$/g, "");
          // 热门地区里的城市会和省份同屏出现：优先最长匹配，避免「吉林」省/市之类的歧义靠后处理
          const hit = tags.filter(t => !picked.includes(t) && core(t).length >= 2 && answer.includes(core(t))).sort((a, b) => answer.indexOf(core(a)) - answer.indexOf(core(b)) || b.length - a.length)[0];
          if (!hit) break;
          if (!await clickPopupOption(hit, '[class*="sd-Tag-container"]', hit)) break;
          picked.push(hit);
          if (!await popupOpen()) break;
        }
        const complaint = await settle(r.idx);
        const shown = await shownValue(r.idx);
        if (complaint && picked.length) fail(r, `选了 ${picked.length} 级但表单仍提示：${complaint}`);
        else if (picked.length && shown) done.push({ label: r.label, ...where, qb: r.qb, action: "cascade", value: r.secret ? "〈已掩码〉" : shown, note: "请回读核对层级是否完整" });
        else fail(r, picked.length ? "逐级点完后控件没显示值" : "弹层里没找到与答案对应的省市");
      }
    } catch (e) {
      await closePopups().catch(() => {});
      fail(r, "操作异常：" + String(e.message || e).split("\n")[0].slice(0, 80));
    }
  }
  console.log("FM_JSON " + JSON.stringify({ done, failed }));
}
