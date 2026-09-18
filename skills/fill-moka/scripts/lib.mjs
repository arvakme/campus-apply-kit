// lib.mjs · scrape.mjs / apply.mjs 共用的页面操作（run.sh 会把它拼在两个脚本前面；ego 的 nodejs 只吃单文件）
// 约定：脚本拿到页面后赋给这里的 page。page.evaluate 只接受一个参数，多个值一律包成对象传。
let page = null;
const FIELD = '[class*="apply-field-"]';

// 滚到视野中间并等位置稳定再取坐标：页面是平滑滚动，滚动途中取到的坐标点下去会落空（下拉时开时不开的根因）
async function stablePos(idx, nth = 0, inner = '[class*="sd-Input-container"]') {
  await page.evaluate(({ FIELD, idx }) => document.querySelectorAll(FIELD)[idx].scrollIntoView({ block: "center", behavior: "instant" }), { FIELD, idx });
  let last = null;
  for (let i = 0; i < 12; i++) {
    const p = await page.evaluate(({ FIELD, idx, nth, inner }) => {
      const el = document.querySelectorAll(FIELD)[idx].querySelectorAll(inner)[nth];
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
    }, { FIELD, idx, nth, inner });
    if (!p) return null;
    if (last && last.x === p.x && last.y === p.y) return p;
    last = p;
    await page.waitForTimeout(120);
  }
  return last;
}

const popupOpen = () => page.evaluate(() => !![...document.querySelectorAll('[class*="sd-Dropdown-dropdown"]')].find(e => e.getClientRects().length));

// 普通下拉按 Escape 就收；地区弹层不认 Escape，要「点到外面」才收——留着不收会挡住下一个下拉（取到的是它的选项）
async function closePopups() {
  for (let i = 0; i < 2 && await popupOpen(); i++) { await page.keyboard.press("Escape"); await page.waitForTimeout(180); }
  if (!await popupOpen()) return;
  await page.evaluate(() => { for (const t of ["mousedown", "mouseup", "click"]) document.body.dispatchEvent(new MouseEvent(t, { bubbles: true, clientX: 2, clientY: 2 })); });
  await page.waitForTimeout(200);
  if (!await popupOpen()) return;
  // 兜底：真点一下表单里不可交互的空白（字段标题左侧的留白）
  const pos = await page.evaluate(({ FIELD }) => { const f = [...document.querySelectorAll(FIELD)].find(e => { const r = e.getBoundingClientRect(); return r.top > 80 && r.bottom < innerHeight - 80; }); if (!f) return null; const r = f.getBoundingClientRect(); return { x: Math.max(3, Math.round(r.x) - 12), y: Math.round(r.y + 6) }; }, { FIELD });
  if (pos) { await page.mouse.click(pos.x, pos.y, { label: "收起弹层" }); await page.waitForTimeout(250); }
}

// 展开某字段的第 nth 个下拉。已展开的下拉再点一次会收起，所以先收干净再点；点一下没开再补 ArrowDown
async function openDropdown(idx, nth = 0, label = "") {
  await closePopups();
  const pos = await stablePos(idx, nth);
  if (!pos) return false;
  await page.mouse.click(pos.x, pos.y, { label: "展开 " + label });
  for (let i = 0; i < 4 && !await popupOpen(); i++) await page.waitForTimeout(150);
  if (!await popupOpen()) { await page.keyboard.press("ArrowDown"); await page.waitForTimeout(400); }
  return await popupOpen();
}

// 当前弹层里的选项文字（普通下拉、年/月下拉都是 sd-Menu-container；地区弹层是 sd-Tag）
const popupOptions = (sel = '[class*="sd-Menu-container"]', cap = 80) => page.evaluate(({ sel, cap }) => {
  const pop = [...document.querySelectorAll('[class*="sd-Dropdown-dropdown"]')].filter(e => e.getClientRects().length).pop();
  return pop ? [...pop.querySelectorAll(sel)].map(e => (e.innerText || "").replace(/\s+/g, " ").trim()).filter(Boolean).slice(0, cap) : [];   // 带分组的选项是两行（如「浙江⏎杭州市」），压成一行
}, { sel, cap });

// 在弹层里点文字等于 text（或 texts 里任一）的选项；长列表先滚到可见再点
async function clickPopupOption(texts, sel = '[class*="sd-Menu-container"]', label = "") {
  const want = [].concat(texts).map(String);
  const found = await page.evaluate(({ sel, want }) => {
    const pop = [...document.querySelectorAll('[class*="sd-Dropdown-dropdown"]')].filter(e => e.getClientRects().length).pop();
    const it = pop && [...pop.querySelectorAll(sel)].find(e => want.includes((e.innerText || "").replace(/\s+/g, " ").trim()));
    if (!it) return false;
    it.scrollIntoView({ block: "center", behavior: "instant" });
    it.setAttribute("data-fm-target", "1");
    return true;
  }, { sel, want });
  if (!found) return false;
  await page.waitForTimeout(200);
  const pos = await page.evaluate(() => { const it = document.querySelector('[data-fm-target="1"]'); it.removeAttribute("data-fm-target"); const r = it.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }; });
  await page.mouse.click(pos.x, pos.y, { label: "选 " + (label || want[0]) });
  await page.waitForTimeout(350);
  return true;
}

// 字段控件当前显示的值（下拉显示值 + 地区标签 + 文本框）
const shownValue = idx => page.evaluate(({ FIELD, idx }) => {
  const c = document.querySelectorAll(FIELD)[idx].querySelector('[class*="ctrl-"]');
  const parts = [...c.querySelectorAll('[class*="sd-Input-display-value"], [class*="sd-Tag-text"]')].map(e => (e.innerText || "").trim()).filter(Boolean);
  const typed = [...c.querySelectorAll("textarea, input")].filter(i => i.type !== "file" && !i.closest('[class*="sd-Select-container"]')).map(i => i.value).filter(Boolean);
  return parts.concat(typed).join(" ");
}, { FIELD, idx });

// 日历式日期控件（day_info）：弹层顶上是 «  ‹  2026年 九月  ›  »，下面是日期格。按年、月差点箭头翻页，再点当月的那一天
async function pickDay(idx, d, label = "") {
  if (!await openDropdown(idx, 0, label)) return "日历没展开";
  const MONTHS = ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"];
  const header = () => page.evaluate(() => { const p = [...document.querySelectorAll('[class*="sd-Dropdown-dropdown"]')].filter(e => e.getClientRects().length).pop(); return p ? { y: (p.querySelector('[class*="sd-basic-selector-year"]')?.innerText || "").trim(), m: (p.querySelector('[class*="sd-basic-selector-month"]')?.innerText || "").trim() } : null; });
  const nav = (icon, times) => page.evaluate(({ icon, times }) => { const p = [...document.querySelectorAll('[class*="sd-Dropdown-dropdown"]')].filter(e => e.getClientRects().length).pop(); for (let i = 0; p && i < times; i++) p.querySelector(`[class*="sd-Icon-icon${icon}-"]`)?.click(); }, { icon, times });
  for (let round = 0; round < 4; round++) {                      // 连点后重读表头校准，最多四轮
    const h = await header(); if (!h) return "日历弹层不见了";
    if (h.y && !h.m) {                                          // 月份面板（表头只有年，下面是 一月…十二月）：先把年翻对，再点月份进到日期面板
      const dy0 = +d.y - parseInt(h.y, 10);
      if (dy0) { await nav(dy0 < 0 ? "doubleLeft" : "doubleRight", Math.abs(dy0)); await page.waitForTimeout(250); }
      await page.evaluate(name => { const p = [...document.querySelectorAll('[class*="sd-Dropdown-dropdown"]')].filter(e => e.getClientRects().length).pop(); [...p.querySelectorAll("*")].find(e => !e.children.length && (e.innerText || "").trim() === name)?.click(); }, MONTHS[+d.m - 1]);
      await page.waitForTimeout(350);
      if (!await popupOpen()) return null;                      // 「年月」精度的控件：点完月份就选完了，弹层自己收起
      continue;
    }
    const dy = +d.y - parseInt(h.y, 10), dm = +d.m - (MONTHS.indexOf(h.m) + 1);
    if (Number.isNaN(dy) || MONTHS.indexOf(h.m) < 0) return `读不懂日历表头「${h.y} ${h.m}」`;
    if (!dy && !dm) break;
    if (dy) await nav(dy < 0 ? "doubleLeft" : "doubleRight", Math.abs(dy));
    if (dm) await nav(dm < 0 ? "left" : "right", Math.abs(dm));
    await page.waitForTimeout(250);
  }
  const h = await header();
  if (!h || parseInt(h.y, 10) !== +d.y || MONTHS.indexOf(h.m) + 1 !== +d.m) { await closePopups(); return "日历翻不到目标年月"; }
  const pos = await page.evaluate(day => {
    const p = [...document.querySelectorAll('[class*="sd-Dropdown-dropdown"]')].filter(e => e.getClientRects().length).pop();
    const cell = [...p.querySelectorAll('td[class*="sd-basic-item-wrapper"]')].find(td => !/sd-basic-fade/.test(td.className) && (td.innerText || "").trim() === String(+day));
    if (!cell) return null; const r = cell.getBoundingClientRect(); return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  }, d.d);
  if (!pos) { await closePopups(); return `当月没有 ${d.d} 号`; }
  await page.mouse.click(pos.x, pos.y, { label: "选 " + (label || "日期") });
  await page.waitForTimeout(350);
  await closePopups();
  return null;
}

// 找表单所在的标签页：worker 往往已经在某个标签里打开并登录了申请页。指定了 --page 用指定的；否则找地址里带这个职位 id 的；都没有就用 p1
async function pickPage(task, url, wantLabel) {
  if (wantLabel) return task.page(wantLabel);
  const jobId = (String(url).match(/#\/job\/([0-9a-f-]{8,})/i) || [])[1];
  if (jobId) for (const p of await task.pages()) {
    try { if (String(await p.evaluate(() => location.href)).includes(jobId)) return p; } catch (e) { /* 页面不可执行脚本（about:blank 等），跳过 */ }
  }
  return task.page("p1");
}

// 让刚操作的控件失焦：Moka 的下拉/日期在「失焦」时才重新校验，最后操作的那个没人让它失焦，
// 旧的「必填项未填写」就一直挂着（值其实已经写进去了）。点本字段的标题文字——它是普通 div，不会触发任何操作
async function settle(idx) {
  await closePopups();
  const pos = await stablePos(idx, 0, '[class*="title-"]');
  if (pos) { await page.mouse.click(pos.x, pos.y, { label: "失焦" }); await page.waitForTimeout(300); }
  return await page.evaluate(({ FIELD, idx }) => [...document.querySelectorAll(FIELD)[idx].querySelectorAll('[class*="sd-Input-message"]')].map(e => (e.innerText || "").trim()).filter(Boolean).join("/"), { FIELD, idx });
}
