"""服务端渲染的次级页面：/bank /setup /handoffs /handoff/<id>。

视觉沿用看板模板的 token：灰阶为主，衬线正文 + 等宽数字，蓝色只给主线（主按钮、已完成进度），
红橙只用于"未填 / 未通过 / 待处理"这类需要注意的状态。
"""

import html
import json

import bank
import handoffs

BASE_CSS = """
  :root {
    --canvas: #fdfdfc; --card-bg: #ffffff; --card-shadow: rgba(30,33,44,.14);
    --ink: #1d1d1f; --ink-soft: #6a6f7a; --ink-faint: #9aa0ac;
    --grid: #c9cdd6; --line-strong: #55595f;
    --blue: #4263eb; --blue-fill: #dbe2fb;
    --seq1: #f1ac4b; --seq2: #e16919; --seq3: #e03131;
    --row-hi: #f2f3f6;
    --font-serif: Charter, Georgia, 'Songti SC', 'Noto Serif CJK SC', 'Times New Roman', serif;
    --font-mono: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    --lw-card: 1.5px; --lw-content: 1.25px; --lw-grid: .8px;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --canvas: #131316; --card-bg: #1c1c21; --card-shadow: rgba(0,0,0,.5);
      --ink: #e8e8ea; --ink-soft: #9aa0ac; --ink-faint: #6a6f7a;
      --grid: #34363d; --line-strong: #b9bdc6;
      --blue: #748ffc; --blue-fill: #2b3452;
      --seq1: #f4b75f; --seq2: #f4801f; --seq3: #f25050;
      --row-hi: #24242b;
    }
  }
  :root[data-theme="dark"] {
    --canvas: #131316; --card-bg: #1c1c21; --card-shadow: rgba(0,0,0,.5);
    --ink: #e8e8ea; --ink-soft: #9aa0ac; --ink-faint: #6a6f7a;
    --grid: #34363d; --line-strong: #b9bdc6;
    --blue: #748ffc; --blue-fill: #2b3452;
    --seq1: #f4b75f; --seq2: #f4801f; --seq3: #f25050;
    --row-hi: #24242b;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--canvas); color: var(--ink); font-family: var(--font-serif);
         font-size: 15px; line-height: 1.5; padding-block: 28px 96px; padding-inline: 20px; }
  .wrap { max-width: 860px; margin: 0 auto; }
  .num, code { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  a { color: inherit; text-decoration: none; border-bottom: 1px solid var(--grid); }
  a:hover { border-bottom-color: var(--ink); }
  .nav { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 13.5px; margin: 0 0 14px; color: var(--ink-soft); }
  .nav a { border-bottom-color: transparent; }
  .nav a:hover, .nav a.on { border-bottom-color: var(--ink); color: var(--ink); }
  .nav .cnt { font-family: var(--font-mono); font-size: 11.5px; color: var(--seq3); margin-left: 3px; }
  h1 { font-size: 34px; font-weight: 700; line-height: 1.1; margin: 0 0 6px; letter-spacing: -.01em; }
  .sub { color: var(--ink-faint); font-size: 14px; margin: 0 0 22px; }
  h2 { font-size: 18.5px; font-weight: 700; margin: 30px 0 10px; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  h2 .hint { font-weight: 400; font-size: 13px; color: var(--ink-faint); font-style: italic; }
  .card { background: var(--card-bg); border: var(--lw-card) solid var(--ink); box-shadow: 3px 4px 10px var(--card-shadow); padding: 14px 16px; }
  .missing { border: var(--lw-content) dashed var(--ink-faint); color: var(--ink-faint); font-style: italic; font-size: 13.5px; padding: 14px 16px; }
  .tag { display: inline-block; font-family: var(--font-mono); font-size: 11px; padding: 0 6px; border: 1px solid var(--ink-faint); border-radius: 2px; color: var(--ink-soft); margin-left: 6px; vertical-align: 1px; white-space: nowrap; }
  .tag.hot { border-color: var(--seq3); color: var(--seq3); }
  .tag.warm { border-color: var(--seq2); color: var(--seq2); }
  .tag.blue { border-color: var(--blue); color: var(--blue); }
  .btn { font-family: var(--font-serif); font-size: 14px; color: var(--ink); background: var(--card-bg); border: var(--lw-card) solid var(--ink); border-radius: 0; padding: 5px 14px; cursor: pointer; }
  .btn:hover { background: var(--row-hi); }
  .btn.primary { border-color: var(--blue); color: var(--blue); font-weight: 700; }
  .btn:disabled { color: var(--ink-faint); border-color: var(--grid); cursor: default; background: transparent; }
  .meter { display: flex; align-items: center; gap: 10px; margin: 6px 0 0; }
  .meter .track { flex: 1; height: 12px; border: var(--lw-content) solid var(--ink-faint); }
  .meter .fill { height: 100%; background: var(--blue-fill); border-right: var(--lw-card) solid var(--blue); }
  .meter .n { font-family: var(--font-mono); font-size: 13px; }
  .list { border-top: var(--lw-grid) solid var(--grid); }
  .row { display: grid; grid-template-columns: 22px 1fr; gap: 10px; padding: 10px 0; border-bottom: var(--lw-grid) solid var(--grid); align-items: baseline; }
  .mark { font-family: var(--font-mono); font-size: 13px; }
  .mark.ok { color: var(--blue); }
  .mark.bad { color: var(--seq3); }
  .mark.opt { color: var(--ink-faint); }
  .detail { color: var(--ink-soft); font-size: 13px; }
  footer { color: var(--ink-faint); font-size: 12.5px; margin-top: 44px; border-top: var(--lw-grid) solid var(--grid); padding-top: 10px; }
  @media (max-width: 640px) { body { padding-inline: 16px; } h1 { font-size: 28px; } }
"""

NAV = [("/", "看板"), ("/bank", "问题库"), ("/setup", "前置条件"), ("/handoffs", "待我处理")]


def e(v):
    return html.escape("" if v is None else str(v), quote=True)


def shell(title, active, body, instance, extra_css="", script=""):
    cnt = handoffs.open_count(instance)
    nav = "".join('<a href="%s"%s>%s%s</a>' % (href, ' class="on"' if href == active else "", label,
                                                 '<span class="cnt">%d</span>' % cnt if href == "/handoffs" and cnt else "")
                  for href, label in NAV)
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
            "<title>%s</title><style>%s%s</style></head><body><div class=\"wrap\">"
            "<nav class=\"nav\">%s</nav>%s</div>%s</body></html>") % (
        e(title), BASE_CSS, extra_css, nav, body, script)


# --------------------------------------------------------------------------
# /bank
# --------------------------------------------------------------------------

BANK_CSS = """
  .toolbar { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 14px 0 0; font-size: 13px; color: var(--ink-soft); }
  .toolbar label { display: inline-flex; gap: 5px; align-items: center; cursor: pointer; }
  .cbx { accent-color: var(--blue); }
  .q { padding: 12px 0 14px; border-bottom: var(--lw-grid) solid var(--grid); }
  .q .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 0; }
  .q .title { font-weight: 700; }
  .q .id { font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-faint); margin-left: auto; padding-left: 10px; }
  .q .hint { font-style: italic; color: var(--ink-faint); font-size: 13px; margin: 2px 0 6px; }
  .q.unfilled .title::before { content: '○ '; color: var(--seq3); font-family: var(--font-mono); font-size: 12px; }
  .q.filled .title::before { content: '● '; color: var(--blue); font-family: var(--font-mono); font-size: 12px; }
  .q textarea, .q input[type=password] { width: 100%; font-family: var(--font-serif); font-size: 14.5px; color: var(--ink); background: var(--canvas); border: var(--lw-content) solid var(--grid); border-radius: 0; padding: 6px 8px; resize: vertical; }
  .q textarea:focus, .q input:focus { outline: none; border-color: var(--ink); }
  .q.dirty textarea, .q.dirty input[type=password] { border-color: var(--blue); }
  .q .meta { font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-faint); margin-top: 3px; }
  .rules li { margin: 0 0 6px; font-size: 14px; }
  .savebar { position: fixed; left: 0; right: 0; bottom: 0; background: var(--card-bg); border-top: var(--lw-card) solid var(--ink);
             padding: 10px 20px calc(10px + env(safe-area-inset-bottom, 0px)); display: flex; gap: 12px; align-items: center; justify-content: center; flex-wrap: wrap; }
  .savebar .msg { font-size: 13px; color: var(--ink-soft); }
  .savebar .msg.err { color: var(--seq3); }
  .hidden { display: none !important; }
"""


def bank_page(instance, doc, version):
    view = bank.public_view(doc)
    c = view["completeness"]
    pct = 100 * c["required_filled"] / c["required"] if c["required"] else 100
    parts = ['<h1>问题库</h1><p class="sub">question-bank.yaml · <span class="num">%d</span> 条 · 版本 <span class="num">%s</span></p>'
             % (c["total"], e(version[:12]))]
    parts.append('<div class="card"><div>必填完成度 <span class="num">%d / %d</span>%s</div>'
                 '<div class="meter"><div class="track"><div class="fill" style="width:%.1f%%"></div></div>'
                 '<span class="n">%d%%</span></div></div>'
                 % (c["required_filled"], c["required"],
                    ' <span class="tag hot">未填 %d</span>' % len(c["missing_ids"]) if c["missing_ids"] else "",
                    pct, round(pct)))
    parts.append('<div class="toolbar"><label><input type="checkbox" class="cbx" id="only-missing"> 只看未填</label>'
                 '<label><input type="checkbox" class="cbx" id="only-required"> 只看必填</label>'
                 '<span>secret 条目只写不读：留空表示不改</span></div>')

    items = view["items"]
    cats = [cat for cat in bank.CATEGORIES if any(i["category"] == cat for i in items)]
    for cat in cats:
        group = [i for i in items if i["category"] == cat]
        group.sort(key=lambda i: (i["filled"], not i.get("required")))
        missing = sum(1 for i in group if not i["filled"])
        parts.append('<section class="cat"><h2>%s <span class="hint"><span class="num">%d</span> 条%s</span></h2>'
                     % (e(cat), len(group), " · 未填 <span class=\"num\">%d</span>" % missing if missing else ""))
        for it in group:
            tags = ""
            if it.get("required"):
                tags += '<span class="tag %s">必填</span>' % ("hot" if not it["filled"] else "")
            if it["kind"] == "rule":
                tags += '<span class="tag">规则</span>'
            if it["kind"] == "secret":
                tags += '<span class="tag warm">secret</span>'
            if it["kind"] == "secret":
                field = ('<input type="password" autocomplete="new-password" data-id="%s" placeholder="%s">'
                         % (e(it["id"]), "已填 · 输入新值覆盖" if it["filled"] else "未填 · 输入后保存"))
            else:
                ans = it.get("answer") or ""
                rows = 1 if len(ans) < 40 else 3
                field = '<textarea rows="%d" data-id="%s">%s</textarea>' % (rows, e(it["id"]), e(ans))
            meta = " · ".join(x for x in (e(it.get("updated")), e(it.get("source"))) if x)
            parts.append(
                '<div class="q %s" data-required="%s" data-filled="%s"><div class="head"><span class="title">%s</span>%s'
                '<span class="id">%s</span></div>%s%s%s</div>'
                % ("filled" if it["filled"] else "unfilled", "1" if it.get("required") else "0",
                   "1" if it["filled"] else "0", e(it["question"]), tags, e(it["id"]),
                   '<div class="hint">%s</div>' % e(it["hint"]) if it.get("hint") else "",
                   field, '<div class="meta">%s</div>' % meta if meta else ""))
        parts.append("</section>")

    if view["rules"]:
        parts.append('<h2>通用默认规则 <span class="hint">只读，改规则用 qb.py 或直接编辑 yaml</span></h2><ul class="rules">')
        parts += ['<li>%s</li>' % e(r["text"].replace("**", "")) for r in view["rules"]]
        parts.append("</ul>")
    parts.append('<footer>保存时带版本号；worker 或 qb.py 同时改过会提示冲突，你的输入不会丢。</footer>')
    parts.append('<div class="savebar hidden" id="savebar"><span class="msg" id="msg"></span>'
                 '<button class="btn primary" id="save">保存</button><button class="btn" id="discard">撤销修改</button></div>')

    base = {i["id"]: (i.get("answer") if i["kind"] != "secret" else None) for i in items}
    script = "<script>window.BANK=%s;</script><script>%s</script>" % (
        json.dumps({"version": version, "base": base}, ensure_ascii=False).replace("</", "<\\/"), BANK_JS)
    return shell("问题库", "/bank", "".join(parts), instance, BANK_CSS, script)


BANK_JS = r"""
(function(){
'use strict';
const B = window.BANK;
const $ = s => document.querySelector(s);
const fields = [...document.querySelectorAll('.q [data-id]')];
const bar = $('#savebar'), msg = $('#msg'), save = $('#save');
function dirty(){
  return fields.filter(f => f.type === 'password' ? f.value !== '' : f.value !== (B.base[f.dataset.id] || ''));
}
function refresh(){
  fields.forEach(f => f.closest('.q').classList.toggle('dirty', dirtySet.has(f)));
  const n = dirtySet.size;
  bar.classList.toggle('hidden', n === 0 && !msg.classList.contains('err'));
  if (!msg.classList.contains('err')) msg.textContent = n ? `${n} 处修改未保存` : '';
  save.disabled = n === 0;
}
let dirtySet = new Set();
fields.forEach(f => f.addEventListener('input', () => { dirtySet = new Set(dirty()); msg.classList.remove('err'); refresh(); }));
function filter(){
  const om = $('#only-missing').checked, or = $('#only-required').checked;
  document.querySelectorAll('.q').forEach(q => {
    q.classList.toggle('hidden', (om && q.dataset.filled === '1') || (or && q.dataset.required === '0'));
  });
}
$('#only-missing').addEventListener('change', filter);
$('#only-required').addEventListener('change', filter);
$('#discard').addEventListener('click', () => {
  fields.forEach(f => { f.value = f.type === 'password' ? '' : (B.base[f.dataset.id] || ''); });
  dirtySet = new Set(); msg.classList.remove('err'); refresh();
});
async function put(version, updates){
  const r = await fetch('/api/bank', {method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({version, updates})});
  return [r.status, await r.json().catch(() => ({}))];
}
save.addEventListener('click', async () => {
  const updates = [...dirtySet].map(f => ({id: f.dataset.id, answer: f.value}));
  save.disabled = true; msg.classList.remove('err'); msg.textContent = '保存中…';
  let [status, body] = await put(B.version, updates);
  if (status === 409) {
    // 别人刚改过：只要没人动过我改的这几条，就在最新版本上自动重试一次
    const cur = await (await fetch('/api/bank')).json();
    const now = Object.fromEntries(cur.items.map(i => [i.id, i]));
    const clash = updates.filter(u => now[u.id] && now[u.id].answer !== undefined && (now[u.id].answer || '') !== (B.base[u.id] || ''));
    if (clash.length) {
      msg.classList.add('err');
      msg.textContent = `冲突：${clash.map(u => u.id).join('、')} 刚被别人改过。复制好你的输入后刷新页面再改。`;
      save.disabled = false; bar.classList.remove('hidden'); return;
    }
    [status, body] = await put(cur.version, updates);
  }
  if (status === 200) { msg.textContent = '已保存'; location.reload(); return; }
  msg.classList.add('err');
  msg.textContent = (body && body.error) || `保存失败（HTTP ${status}）`;
  save.disabled = false;
});
refresh();
})();
"""


# --------------------------------------------------------------------------
# /setup
# --------------------------------------------------------------------------

def _rows(checks):
    out = []
    for c in checks:
        cls, sym = ("ok", "✓") if c["ok"] else (("bad", "✗") if c.get("required", True) else ("opt", "–"))
        out.append('<div class="row"><span class="mark %s">%s</span><div><b>%s</b>%s<div class="detail">%s</div></div></div>'
                   % (cls, sym, e(c["name"]), "" if c.get("required", True) else ' <span class="tag">可选</span>',
                      e(c["detail"])))
    return '<div class="list">%s</div>' % "".join(out)


def setup_page(instance, host, host_note, inst, mats, mats_dir_ok):
    allc = (host or []) + inst + mats
    req = [c for c in allc if c.get("required", True)]
    bad = [c for c in req if not c["ok"]]
    all_green = host is not None and not bad
    parts = ['<h1>前置条件</h1><p class="sub">%s</p>' % (
        "全部通过" if all_green else "未通过 <span class=\"num\">%d</span> 项%s" % (
            len(bad) + (1 if host is None else 0), "（含宿主机体检未运行）" if host is None else ""))]
    parts.append('<h2>宿主机 <span class="hint">host/host-check.py 写入 state/host-health.json%s</span></h2>'
                 % (" · " + e(host_note) if host and host_note else ""))
    parts.append(_rows(host) if host else '<div class="missing">%s</div>' % e(host_note))
    parts.append('<h2>实例文件 <span class="hint">容器内直接检查 /instance</span></h2>')
    parts.append(_rows(inst))
    parts.append('<h2>材料 <span class="hint">materials/ · 规格见 docs/materials.md</span></h2>')
    parts.append(_rows(mats) if mats_dir_ok else '<div class="missing">实例里还没有 materials/ 目录</div>' + _rows(mats))
    return shell("前置条件", "/setup", "".join(parts), instance)


# --------------------------------------------------------------------------
# /handoffs 与 /handoff/<id>
# --------------------------------------------------------------------------

HANDOFF_CSS = """
  .ho { display: grid; grid-template-columns: 1fr auto; gap: 2px 12px; padding: 11px 0; border-bottom: var(--lw-grid) solid var(--grid); }
  .ho .t { font-weight: 700; }
  .ho .w { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); white-space: nowrap; }
  .ho .d { grid-column: 1 / 3; color: var(--ink-soft); font-size: 13.5px; }
  .ho.done .t, .ho.done .d { color: var(--ink-faint); }
  .action { font-size: 17px; line-height: 1.55; margin: 14px 0; }
  .shot { margin: 16px 0; border: var(--lw-card) solid var(--ink); background: var(--card-bg); padding: 8px; }
  .shot img { display: block; width: 100%; height: auto; -webkit-touch-callout: default; user-select: auto; }
  .facts { display: grid; grid-template-columns: max-content 1fr; gap: 3px 14px; font-size: 13.5px; color: var(--ink-soft); }
  .facts dt { color: var(--ink-faint); }
  .facts dd { margin: 0; }
  .resolved { color: var(--blue); font-weight: 700; }
"""


def _kind(rec):
    return handoffs.KIND_LABEL.get(rec.get("kind") or "", rec.get("kind") or rec.get("event") or "")


def handoffs_page(instance, records):
    active = [r for r in records if handoffs.is_active(r)]
    queued = [r for r in records if r.get("status") in handoffs.QUEUED and not r.get("resolved_at")]
    done = [r for r in records if not handoffs.is_active(r) and r not in queued][:30]

    def item(r):
        return ('<div class="ho%s"><a class="t" href="/handoff/%s">%s</a><span class="w">%s</span>'
                '<div class="d">%s%s<span class="tag">%s</span> <span class="tag">%s</span></div></div>'
                % ("" if handoffs.is_active(r) else " done", e(r["id"]), e(r.get("title") or r["id"]),
                   e((r.get("created_at") or "")[5:16].replace("T", " ")),
                   e(r.get("company") or ""), " · " + e(r.get("worker")) if r.get("worker") else "", e(_kind(r)),
                   e(handoffs.STATUS_LABEL.get(r.get("status") or "", r.get("status") or ""))))

    parts = ['<meta http-equiv="refresh" content="15">',
             '<h1>待我处理</h1><p class="sub">worker 停下来等你的事：扫码、验证码、登录、提交前确认 · 每 15 秒自动刷新</p>']
    parts.append('<h2>等你处理 <span class="hint"><span class="num">%d</span> 条</span></h2>' % len(active))
    parts.append("".join(item(r) for r in active) if active else '<div class="missing">现在没有要你处理的</div>')
    if queued:
        parts.append('<h2>排队中 <span class="hint"><span class="num">%d</span> 条 · 轮到时推送，不用管</span></h2>' % len(queued))
        parts.append("".join(item(r) for r in queued))
    if done:
        parts.append('<h2>最近结束 <span class="hint">已处理 / 过期 / 取消，最近 <span class="num">%d</span> 条</span></h2>' % len(done))
        parts.append("".join(item(r) for r in done))
    return shell("待我处理", "/handoffs", "".join(parts), instance, HANDOFF_CSS)


def handoff_page(instance, rec):
    parts = ['<p class="sub"><a href="/handoffs">← 全部待处理</a></p>']
    parts.append('<h1>%s</h1>' % e(rec.get("title") or rec["id"]))
    parts.append('<p class="sub">%s%s<span class="tag %s">%s</span></p>' % (
        e(rec.get("company") or ""), " · id <span class=\"num\">%s</span>" % e(rec.get("announcement_id"))
        if rec.get("announcement_id") not in (None, "") else "",
        "hot" if not rec.get("resolved_at") else "", e(_kind(rec))))
    if rec.get("action"):
        parts.append('<div class="card action">%s</div>' % e(rec["action"]))
    shot = rec.get("shot")
    if shot:
        parts.append('<div class="shot"><img src="/raw/%s" alt="截图（二维码可长按识别）"></div>' % e(shot.lstrip("/")))
    facts = [("worker", rec.get("worker")), ("事件", rec.get("event")), ("创建", rec.get("created_at")),
             ("处理", rec.get("resolved_at")), ("记录", "state/handoffs/%s.json" % rec["id"])]
    parts.append('<dl class="facts">%s</dl>' % "".join("<dt>%s</dt><dd class=\"num\">%s</dd>" % (e(k), e(v))
                                                       for k, v in facts if v))
    if rec.get("resolved_at"):
        parts.append('<p class="resolved">已处理 · %s</p>' % e(rec["resolved_at"]))
    else:
        parts.append('<p><button class="btn primary" id="resolve">已处理</button> <span class="detail" id="rmsg"></span></p>')
    script = """<script>
(function(){ const b = document.getElementById('resolve'); if(!b) return;
  b.addEventListener('click', async () => { b.disabled = true;
    const r = await fetch('/api/handoff/%s/resolve', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    if (r.ok) location.reload(); else { b.disabled = false; document.getElementById('rmsg').textContent = '失败 HTTP ' + r.status; }
  });
})();
</script>""" % json.dumps(rec["id"])[1:-1]
    return shell(rec.get("title") or "handoff", "/handoffs", "".join(parts), instance, HANDOFF_CSS, script)
