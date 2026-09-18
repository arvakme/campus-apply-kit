"""egospace.py · 关卡交给用户前，宿主机对 ego 空间做硬校验并交出（notify.py / tgbot.py 共用）

规则（项目约定：推过来的空间必须就是要处理的那一页）：
  1. 空间必须存在；名字是固定槽位 campus-slot-N，或含该关卡的 announcement_id/公司名（其他通用空间一律拒绝）
  2. 空间里只保留关卡页：--page 指定的标签（如 p2），没指定就用当前激活的标签；其余标签全部关掉
  3. 以上通过才 handOff（ego lite 显示 Return to agent）
任一步失败返回 (False, 原因)，调用方不推送。
"""
from __future__ import annotations

import json
import subprocess

NEED_SPACE_KINDS = {"captcha", "wechat_qr", "qr", "submit_confirm", "login", "face"}

_SCRIPT = r"""
const sid = %(sid)d, aid = %(aid)s, company = %(company)s, want = %(page)s, doHandOff = %(handoff)s;
const spaces = await listTaskSpaces();
const sp = spaces.find(s => s.id === sid);
if (!sp) { console.log(JSON.stringify({ok:false, reason:"ego 空间 #" + sid + " 不存在（可能被清理）"})); }
else if (!/^campus-slot-\d+$/.test(String(sp.name || "")) && aid && !String(sp.name || "").includes(String(aid)) && !(company && String(sp.name || "").includes(company))) {
  console.log(JSON.stringify({ok:false, reason:"空间 #" + sid + "（" + sp.name + "）名字里没有公告编号 " + aid + "：必须一家公司一个空间，名如 campus-" + aid + "-公司"}));
} else if (sp.ownership !== "agent") {
  console.log(JSON.stringify({ok:true, name: sp.name, already: true}));   // 已经交给用户了，不再动它
} else {
  const t = await taskSpace(sid);
  const tabs = await t.tabs();
  let keep = want ? tabs.find(x => x.label === want) : tabs.find(x => x.active);
  if (!keep && tabs.length === 1) keep = tabs[0];
  if (!keep) { console.log(JSON.stringify({ok:false, reason:"空间 #" + sid + " 里找不到关卡页（" + (want || "激活标签") + "）"})); }
  else {
    const closed = [];
    for (const x of tabs) {
      if (x.targetId === keep.targetId) continue;
      try { await t.page(x.label).close(); closed.push(x.label); } catch (e) {}
    }
    if (doHandOff) await t.handOff();
    console.log(JSON.stringify({ok:true, name: sp.name, kept: keep.label, url: keep.url, closed}));
  }
}
"""


def prepare_and_hand_off(space, announcement_id=None, page=None, hand_off=True, timeout=60, company=None):
    """返回 (ok, info_or_reason)。"""
    if not str(space or "").isdigit():
        return False, "没有 --space（关卡所在 ego 空间编号）"
    script = _SCRIPT % {"sid": int(space), "aid": json.dumps(str(announcement_id or "")), "company": json.dumps(str(company or "")[:8]),
                        "page": json.dumps(page or ""), "handoff": "true" if hand_off else "false"}
    try:
        r = subprocess.run(["ego-browser", "nodejs"], input=script, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "ego-browser 调用失败：%s" % exc
    for line in reversed(((r.stdout or "") + "\n" + (r.stderr or "")).splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            return (True, d) if d.get("ok") else (False, d.get("reason") or "校验失败")
    err = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
    first = next((l.strip() for l in err if l.strip() and not l.strip().startswith("at ")), "")
    return False, "ego 浏览器没响应（%s）" % (first[:80] or "无输出")
