/**
 * campus-tg-relay · 共享 Telegram bot 中转（契约 docs/contracts.md §10）
 *
 * 一个 bot，几位同学各自私聊；本 Worker 持有 BOT_TOKEN，按 instance_id 分队列，
 * Mac 端 tgbot.py 签名轮询 /v1/pull 取回属于自己实例的更新。
 *
 * 存储选型：单个 Durable Object（idFromName("relay")）。
 *   理由：nonce 防重放、6 位配对码一次性核销、每实例 FIFO 队列的 seq/qid 都要
 *   原子 check-and-set；Workers KV 是最终一致，并发下会放重放、会双花配对码。
 *   几人私有部署的规模，单 DO 强一致最简单也最够用。
 *
 * secrets（wrangler secret put）：BOT_TOKEN / WEBHOOK_SECRET / ADMIN_KEY
 * vars：TG_API_BASE（测试覆盖用）；QUEUE_TTL_S / PAIR_TTL_S / NONCE_TTL_S /
 *       TS_WINDOW_S 为测试旋钮，缺省用契约值。
 */

const enc = new TextEncoder();

const QUEUE_MAX = 1000;        // 每实例队列上限，溢出丢最旧
const PULL_LIMIT_MAX = 500;
const PAIR_ATTEMPT_MAX = 5;    // 每 user_id 10 分钟最多试 5 次配对码
const RATE_WEBHOOK = 60;       // 每 user_id 每分钟 webhook 更新上限
const RATE_PULL = 240;         // 每实例每分钟 pull 上限
const RATE_SEND = 300;         // 每实例每分钟 send 上限
const RATE_PAIR_INIT = 12;     // 每实例每小时 pair/init 上限
const RATE_PROFILE = 60;       // 每实例每小时 /v1/profile 上限
const IID_RE = /^i-[0-9a-f]{8,32}$/;
const CODE_HASH_RE = /^[0-9a-f]{64}$/;
const NICK_MAX = 24;
const SEND_METHODS = new Set([
  "sendMessage", "sendPhoto", "sendDocument",
  "editMessageText", "editMessageCaption", "editMessageMedia",
  "editMessageReplyMarkup", "answerCallbackQuery",
  "pinChatMessage", "setChatMenuButton",      // chat_id 被 §10.2 绑死在本实例 chat
]);
// bot 全局资料（非发消息）：tgbot.py profile 幂等重放用
const PROFILE_METHODS = new Set([
  "setMyName", "setMyShortDescription", "setMyDescription", "setMyCommands",
]);
const MEDALS = ["🥇", "🥈", "🥉"];
const WEB_BASE_RE = /^https?:\/\/[^\s"'<>]+$/;
// 置顶面板按钮：与 web/app/board.py 的 NAV 保持一致
const PANEL_BTNS: [string, string][] = [
  ["看板", "/"], ["榜单", "/board"], ["日程", "/agenda"],
  ["待我处理", "/handoffs"], ["问题库", "/bank"], ["前置条件", "/setup"],
];

// --------------------------------------------------------------------------
// 小工具
// --------------------------------------------------------------------------

function secs(): number {
  return Math.floor(Date.now() / 1000);
}

function hex(buf: ArrayBuffer | Uint8Array): string {
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < b.length; i++) s += b[i].toString(16).padStart(2, "0");
  return s;
}

function unhex(s: string): Uint8Array {
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}

async function sha256hex(data: string | Uint8Array): Promise<string> {
  const buf = typeof data === "string" ? enc.encode(data) : data;
  return hex(await crypto.subtle.digest("SHA-256", buf));
}

async function hmacHex(keyBytes: Uint8Array, msg: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}

function eqConst(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

function jresp(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status, headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function clampInt(v: unknown, lo: number, hi: number, dflt: number): number {
  const n = Math.floor(Number(v));
  if (!Number.isFinite(n)) return dflt;
  return Math.min(hi, Math.max(lo, n));
}

function bjDate(offsetDays = 0): string {
  return new Date(Date.now() + 8 * 3600e3 + offsetDays * 86400e3)
    .toISOString().slice(0, 10);
}

function sanitizeNick(s: unknown): string {
  return String(s || "").replace(/[\x00-\x1f@<>\\/]/g, "").trim().slice(0, NICK_MAX);
}

function sanitizeWebBase(v: unknown): string {
  const s = String(v || "").trim().replace(/\/+$/, "");
  return s.length <= 200 && WEB_BASE_RE.test(s) ? s : "";
}

function panelText(): string {
  return [
    "📌 秋招面板",
    "",
    "看板：今天的战报、在跑批次",
    "榜单：27 届全量公司 + 你的投递状态",
    "日程：测评、笔试、面试倒计时",
    "待我处理：扫码、验证码、提交确认",
    "",
    "<i>需要连着 tailnet（sing-box 或 Tailscale）才能打开。</i>",
  ].join("\n");
}

function panelMarkup(webBase: string): unknown {
  const btn = (t: string, p: string) => ({ text: t, url: webBase + p });
  return {
    inline_keyboard: [
      PANEL_BTNS.slice(0, 3).map(([t, p]) => btn(t, p)),
      PANEL_BTNS.slice(3).map(([t, p]) => btn(t, p)),
    ],
  };
}

// --------------------------------------------------------------------------
// 入口
// --------------------------------------------------------------------------

export default {
  async fetch(req: Request, env: any): Promise<Response> {
    const stub = env.RELAY.get(env.RELAY.idFromName("relay"));
    return stub.fetch(req);
  },
  async scheduled(_event: any, env: any, ctx: any): Promise<void> {
    const stub = env.RELAY.get(env.RELAY.idFromName("relay"));
    // 内部 /cron 路由，admin key 门禁（外部 HTTP 摸不到）
    ctx.waitUntil(stub.fetch(new Request("https://relay.internal/cron", {
      headers: { "X-Admin-Key": env.ADMIN_KEY || "" },
    })));
  },
};

// --------------------------------------------------------------------------
// Durable Object：全部状态与逻辑
// --------------------------------------------------------------------------

export class RelayDO {
  state: any;
  env: any;

  constructor(state: any, env: any) {
    this.state = state;
    this.env = env;
  }

  n(name: string, dflt: number): number {
    const v = Number(this.env[name]);
    return Number.isFinite(v) && v > 0 ? v : dflt;
  }

  get store() {
    return this.state.storage;
  }

  async fetch(req: Request): Promise<Response> {
    // 统一排空请求体：任何提前 return（401/429/静默）若留着未读流，
    // workerd dev 代理会异步崩溃、连累下一个请求。读克隆体排干源流，
    // 处理器的 req.arrayBuffer()/json()/formData() 走缓冲副本不受影响。
    try { await req.clone().arrayBuffer(); } catch (_e) { /* 无体可读 */ }
    const url = new URL(req.url);
    const path = url.pathname;
    try {
      if (path === "/tg/webhook" && req.method === "POST") return await this.onWebhook(req);
      if (path === "/v1/pair/init" && req.method === "POST") return await this.onPairInit(req);
      if (path === "/v1/pair/web_base" && req.method === "POST") return await this.onPairWebBase(req);
      if (path === "/v1/pull" && req.method === "GET") return await this.onPull(req, url);
      if (path === "/v1/ack" && req.method === "POST") return await this.onAck(req);
      if (path === "/v1/send" && req.method === "POST") return await this.onSend(req);
      if (path === "/v1/file" && req.method === "GET") return await this.onFile(req, url);
      if (path === "/v1/profile" && req.method === "POST") return await this.onProfile(req);
      if (path === "/v1/rank/report" && req.method === "POST") return await this.onRankReport(req);
      if (path === "/v1/unpair" && req.method === "POST") return await this.onUnpair(req);
      if (path === "/v1/admin/revoke" && req.method === "POST") return await this.onAdminRevoke(req);
      if (path === "/v1/admin/list" && req.method === "GET") return await this.onAdminList(req);
      if (path === "/cron") {
        if (!this.isAdmin(req)) return jresp({ ok: false, error: "unauthorized" }, 401);
        await this.runCron();
        return jresp({ ok: true });
      }
      return jresp({ ok: false, error: "not found" }, 404);
    } catch (e) {
      return jresp({ ok: false, error: "internal error" }, 500);
    }
  }

  // ---------------- 签名与限流 ----------------

  async verifySig(req: Request, iid: string, keyHex: string, body: Uint8Array): Promise<string | null> {
    const ts = req.headers.get("X-Ts") || "";
    const nonce = req.headers.get("X-Nonce") || "";
    const sig = req.headers.get("X-Sig") || "";
    const now = secs();
    if (!/^\d{1,12}$/.test(ts) || Math.abs(now - Number(ts)) > this.n("TS_WINDOW_S", 60)) {
      return "bad_ts";
    }
    if (!nonce || nonce.length > 128 || !/^[A-Za-z0-9._-]+$/.test(nonce)) return "bad_nonce";
    const u = new URL(req.url);
    const path = u.pathname + u.search;      // 签名串含 query（与 Mac 端约定一致）
    const bodyHash = await sha256hex(body);
    const expect = await hmacHex(
      unhex(keyHex), `${req.method}|${path}|${ts}|${nonce}|${bodyHash}`);
    if (!eqConst(expect, sig)) return "bad_sig";
    // nonce 核销放签名通过之后：签错的不消耗 nonce（避免 DoS 占位）
    const nkey = "nonces:" + iid;
    const ttl = this.n("NONCE_TTL_S", 600);
    return await this.store.transaction(async () => {
      const nonces = (await this.store.get(nkey)) || {};
      for (const k of Object.keys(nonces)) {
        if (now - nonces[k] > ttl) delete nonces[k];
      }
      if (nonces[nonce]) return "nonce_replay";
      nonces[nonce] = now;
      await this.store.put(nkey, nonces);
      return null;
    });
  }

  async authed(req: Request): Promise<{ iid: string; inst: any; body: Uint8Array } | Response> {
    const iid = req.headers.get("X-Instance") || "";
    const inst = await this.store.get("inst:" + iid);
    if (!inst) return jresp({ ok: false, error: "unknown_instance" }, 401);
    const body = new Uint8Array(await req.arrayBuffer());
    const err = await this.verifySig(req, iid, inst.key, body);
    if (err) return jresp({ ok: false, error: err }, 401);
    return { iid, inst, body };
  }

  async rateLimit(key: string, max: number, windowS: number): Promise<boolean> {
    const now = secs();
    return await this.store.transaction(async () => {
      const k = "rl:" + key;
      let r = (await this.store.get(k)) || { n: 0, reset: now + windowS };
      if (r.reset <= now) r = { n: 0, reset: now + windowS };
      r.n += 1;
      await this.store.put(k, r);
      return r.n <= max;
    });
  }

  // ---------------- 队列 ----------------
  // 设计：qidx:<iid> 存活的 qid 数组（小），q:<iid>:<qid> 存消息体。
  // 注意：同请求内 transaction() 之后再 list()，workerd dev 下迭代会被截断，
  // 所以队列遍历一律走 qidx 点读 get，不用 list。

  qkey(iid: string, qid: number): string {
    return `q:${iid}:${String(qid).padStart(12, "0")}`;
  }

  async enqueue(iid: string, item: any): Promise<void> {
    await this.store.transaction(async () => {
      const seqKey = "seq:" + iid;
      const qid = ((await this.store.get(seqKey)) || 0) + 1;
      await this.store.put(seqKey, qid);
      await this.store.put(this.qkey(iid, qid), { qid, at: secs(), ...item });
      const idx = (await this.store.get("qidx:" + iid)) || [];
      idx.push(qid);
      while (idx.length > QUEUE_MAX) {
        const old = idx.shift();
        await this.store.delete(this.qkey(iid, old));
      }
      await this.store.put("qidx:" + iid, idx);
    });
  }

  async removeInstance(iid: string): Promise<boolean> {
    const inst = await this.store.get("inst:" + iid);
    if (!inst) return false;
    if (inst.userId) {
      await this.store.delete("uid:" + inst.userId);
      await this.store.delete("pend:" + inst.userId);
    }
    if (inst.codeHash) await this.store.delete("code:" + inst.codeHash);
    const idx = (await this.store.get("qidx:" + iid)) || [];
    for (const q of idx) await this.store.delete(this.qkey(iid, q));
    for (const k of ["seq:", "nonces:", "rank:", "qidx:"]) {
      await this.store.delete(k + iid);
    }
    const members = ((await this.store.get("members")) || [])
      .filter((x: string) => x !== iid);
    await this.store.put("members", members);
    await this.store.delete("inst:" + iid);
    return true;
  }

  // ---------------- Telegram 出站 ----------------

  tgBase(): string {
    return (this.env.TG_API_BASE || "https://api.telegram.org").replace(/\/+$/, "");
  }

  async tgCall(method: string, params: unknown): Promise<any> {
    try {
      const resp = await fetch(`${this.tgBase()}/bot${this.env.BOT_TOKEN}/${method}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(params),
      });
      return await resp.json().catch(() => null);
    } catch (_e) {
      return null;
    }
  }

  async tgSend(chatId: number, text: string): Promise<void> {
    // 推送失败不阻塞入队
    await this.tgCall("sendMessage", { chat_id: chatId, text: text.slice(0, 3800) });
  }

  // 📌 秋招面板：有 webBase 才发。已有 panelMid 就原地编辑（web_base 变更不重发），
  // 编辑失败（消息被删等）才新发+重新置顶。返回 edited/sent/skipped/failed。
  async publishPanel(inst: any): Promise<string> {
    const wb = String(inst.webBase || "").replace(/\/+$/, "");
    if (!wb || !inst.chatId) return "skipped";
    const msg: any = {
      chat_id: inst.chatId, text: panelText(), parse_mode: "HTML",
      disable_web_page_preview: true, reply_markup: panelMarkup(wb),
    };
    const menu = {
      chat_id: inst.chatId,
      menu_button: { type: "web_app", text: "看板", web_app: { url: wb + "/" } },
    };
    if (inst.panelMid) {
      const r = await this.tgCall("editMessageText", { ...msg, message_id: inst.panelMid });
      if (r && r.ok) {
        await this.tgCall("setChatMenuButton", menu);
        return "edited";
      }
    }
    const r = await this.tgCall("sendMessage", msg);
    const mid = Number(r && r.ok && r.result && r.result.message_id) || 0;
    if (mid) {
      inst.panelMid = mid;
      await this.tgCall("pinChatMessage", {
        chat_id: inst.chatId, message_id: mid, disable_notification: true,
      });
    }
    await this.tgCall("setChatMenuButton", menu);
    return mid ? "sent" : "failed";
  }

  // ---------------- /tg/webhook ----------------

  async onWebhook(req: Request): Promise<Response> {
    // 先消费请求体再校验：提前 401 不读流会把 dev 代理弄崩（下个请求 500）
    const update = await req.json().catch(() => null);
    const secret = req.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
    if (!eqConst(secret, this.env.WEBHOOK_SECRET || "")) {
      return jresp({ ok: false }, 401);
    }
    if (!update) return jresp({ ok: true });
    const ent = this.extract(update);
    // §10.1：只接受私聊且 chat.id == from.id
    if (!ent || ent.chatType !== "private" || ent.chatId !== ent.userId) {
      return jresp({ ok: true });
    }
    if (!await this.rateLimit("u:" + ent.userId, RATE_WEBHOOK, 60)) {
      return jresp({ ok: true });
    }
    // 配对审批按钮（ap:ok:<uid> / ap:no:<uid>）：先进独立通道，不进队列
    const cbd = String(
      (update.callback_query && update.callback_query.data) || "");
    if (cbd.startsWith("ap:")) {
      return await this.onApprovalCb(update.callback_query);
    }
    const iid = await this.store.get("uid:" + ent.userId);
    if (!iid) return await this.unboundWebhook(ent);

    const inst = await this.store.get("inst:" + iid);
    if (!inst) {
      await this.store.delete("uid:" + ent.userId);
      return jresp({ ok: true });
    }
    const cmd = (ent.text || "").split(/\s+/)[0].split("@")[0];
    if (cmd === "/rank") {
      await this.tgSend(ent.chatId, await this.rankText());
      return jresp({ ok: true });
    }
    if (cmd === "/pair") {
      await this.tgSend(ent.chatId, "已绑定实例 " + iid + "；要换绑先在本机 unpair。");
      return jresp({ ok: true });
    }
    // 用户发来的文件（材料上传）：记下 file_id 归属本实例，Mac 端凭签名经 /v1/file 取回，token 不出 Worker
    const fm = update.message || update.edited_message;
    if (fm) {
      const ids: string[] = [];
      if (fm.document && fm.document.file_id) ids.push(String(fm.document.file_id));
      if (Array.isArray(fm.photo) && fm.photo.length) ids.push(String(fm.photo[fm.photo.length - 1].file_id));
      for (const fid of ids) await this.store.put("fid:" + iid + ":" + fid, { at: secs() });
    }
    await this.enqueue(iid, { upd: update });
    return jresp({ ok: true });
  }

  // GET /v1/file?file_id=… ：只允许取本实例用户发来的文件（2 天内），流式返回，不落存储
  async onFile(req: Request, url: URL): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    if (a.inst.status !== "bound") return jresp({ ok: false, error: "not_bound" }, 403);
    const fid = url.searchParams.get("file_id") || "";
    const rec = fid ? await this.store.get("fid:" + a.iid + ":" + fid) : null;
    if (!rec || secs() - Number(rec.at || 0) > 2 * 86400) {
      return jresp({ ok: false, error: "file_not_found" }, 404);
    }
    const base = (this.env.TG_API_BASE || "https://api.telegram.org").replace(/\/$/, "");
    const meta = await fetch(base + "/bot" + this.env.BOT_TOKEN + "/getFile?file_id=" + encodeURIComponent(fid));
    const mj: any = await meta.json().catch(() => ({}));
    const fpath = mj && mj.ok && mj.result && mj.result.file_path;
    if (!fpath) return jresp({ ok: false, error: "tg_getfile_failed" }, 502);
    const file = await fetch(base + "/file/bot" + this.env.BOT_TOKEN + "/" + fpath);
    if (!file.ok || !file.body) return jresp({ ok: false, error: "tg_download_failed" }, 502);
    return new Response(file.body, {
      status: 200,
      headers: { "content-type": "application/octet-stream", "x-file-path": String(fpath) },
    });
  }

  extract(update: any): { userId: number; chatId: number; chatType: string; text: string; username: string } | null {
    const m = update.message || update.edited_message;
    if (m && m.from && m.chat) {
      return {
        userId: Number(m.from.id), chatId: Number(m.chat.id),
        chatType: String(m.chat.type || ""), text: String(m.text || ""),
        username: String(m.from.username || ""),
      };
    }
    const cb = update.callback_query;
    if (cb && cb.from && cb.message && cb.message.chat) {
      return {
        userId: Number(cb.from.id), chatId: Number(cb.message.chat.id),
        chatType: String(cb.message.chat.type || ""), text: "",
        username: String(cb.from.username || ""),
      };
    }
    return null;
  }

  maintainerId(): number {
    return Math.floor(Number(this.env.MAINTAINER_USER_ID)) || 0;
  }

  // 正式绑定：写 uid 映射 + members（入排行榜/播报），推 bound 事件，发 ✅ 与面板
  async bindUser(iid: string, inst: any): Promise<void> {
    inst.status = "bound";
    inst.boundAt = secs();
    await this.store.transaction(async () => {
      await this.store.put("inst:" + iid, inst);
      await this.store.put("uid:" + inst.userId, iid);
      const members = (await this.store.get("members")) || [];
      if (!members.includes(iid)) {
        members.push(iid);
        await this.store.put("members", members);
      }
    });
    await this.enqueue(iid, {
      sys: { type: "bound", chat_id: inst.chatId, user_id: inst.userId },
    });
    await this.tgSend(inst.chatId,
      `✅ 配对成功：本 chat 已绑定「${inst.nickname}」。\n` +
      "以后的投递提醒、扫码请求都发这里。/rank 看今日投递榜。");
    // 📌 秋招面板：pair/init 上报过 web_base 才发；panelMid 变化要落库
    await this.publishPanel(inst);
    await this.store.put("inst:" + iid, inst);
  }

  async unboundWebhook(ent: { userId: number; chatId: number; text: string; username: string }): Promise<Response> {
    const parts = (ent.text || "").split(/\s+/);
    if (parts[0].split("@")[0] === "/pair" && parts[1]) {
      if (await this.rateLimit("pair:" + ent.userId, PAIR_ATTEMPT_MAX, 600)) {
        const codeHash = await sha256hex(parts[1].trim());
        const iid = await this.store.get("code:" + codeHash);
        const inst = iid ? await this.store.get("inst:" + iid) : null;
        if (inst && !inst.userId && inst.codeHash && inst.codeExp > secs()) {
          const maint = this.maintainerId();
          if (!maint || ent.userId === maint) {
            // 维护者本人（或 dev 未配维护者）：直接绑定，不走审批
            inst.userId = ent.userId;
            inst.chatId = ent.chatId;
            inst.username = ent.username;
            inst.codeHash = null;
            inst.codeExp = 0;
            await this.store.transaction(async () => {
              await this.store.put("inst:" + iid, inst);
              await this.store.delete("code:" + codeHash);
            });
            await this.bindUser(iid, inst);
          } else {
            // 其他人：pending 等维护者审批——不入 members、uid 不映射、消息全静默
            if (await this.store.get("pend:" + ent.userId)) {
              await this.tgSend(ent.chatId,
                "你的加入申请已在等维护者确认，不用重发配对码。");
              return jresp({ ok: true });
            }
            inst.userId = ent.userId;
            inst.chatId = ent.chatId;
            inst.username = ent.username;
            inst.codeHash = null;
            inst.codeExp = 0;
            inst.status = "pending";
            inst.pendAt = secs();
            await this.store.transaction(async () => {
              await this.store.put("inst:" + iid, inst);
              await this.store.put("pend:" + ent.userId, iid);
              await this.store.delete("code:" + codeHash);
            });
            await this.tgSend(ent.chatId, "已提交，等待维护者确认。");
            await this.tgCall("sendMessage", {
              chat_id: maint,
              text: `🔔 配对请求\n昵称：${inst.nickname}\n` +
                `Telegram：@${inst.username || "-"}（user_id ${ent.userId}）\n` +
                `实例：${iid}`,
              reply_markup: { inline_keyboard: [[
                { text: "✅ 批准", callback_data: "ap:ok:" + ent.userId },
                { text: "❌ 拒绝", callback_data: "ap:no:" + ent.userId },
              ]] },
            });
          }
          return jresp({ ok: true });
        }
      }
    }
    // 未绑定一律静默，只计数（§10.1 防探测）
    const pkey = "probe:" + ent.userId;
    const p = (await this.store.get(pkey)) || { n: 0 };
    p.n += 1;
    p.last = secs();
    await this.store.put(pkey, p);
    return jresp({ ok: true });
  }

  // 维护者审批回调：ap:ok:<uid> 批准转 bound；ap:no:<uid> 拒绝删实例。
  // 非维护者点击无效（answerCallbackQuery 提示，状态不变）。
  async onApprovalCb(cb: any): Promise<Response> {
    const maint = this.maintainerId();
    const answer = (text: string) => this.tgCall("answerCallbackQuery", {
      callback_query_id: cb.id, text,
    });
    if (!maint || Number(cb.from && cb.from.id) !== maint) {
      await answer("只有维护者可以审批");
      return jresp({ ok: true });
    }
    const parts = String(cb.data || "").split(":");
    const act = parts[1], uid = Math.floor(Number(parts[2]));
    const iid = await this.store.get("pend:" + uid);
    const inst = iid ? await this.store.get("inst:" + iid) : null;
    const mid = cb.message && cb.message.message_id;
    const origText = String(cb.message && cb.message.text || "");
    const mark = async (tag: string) => {
      if (mid && origText) {
        await this.tgCall("editMessageText", {
          chat_id: maint, message_id: mid, text: origText + "\n\n" + tag,
        });
      }
    };
    if (!inst || inst.status !== "pending") {
      await answer("该请求已处理过");
      return jresp({ ok: true });
    }
    if (act === "ok") {
      await this.store.delete("pend:" + uid);
      await this.bindUser(iid, inst);
      await answer("已批准");
      await mark("✅ 已批准");
    } else {
      await this.removeInstance(iid);      // 顺带清 pend 映射（removeInstance 内）
      await answer("已拒绝，实例已删除");
      await this.tgSend(inst.chatId, "你的配对请求未获通过。");
      await mark("❌ 已拒绝");
    }
    return jresp({ ok: true });
  }

  // ---------------- /v1/pair/init ----------------

  async onPairInit(req: Request): Promise<Response> {
    // 全局限流：每来源 IP 每小时 N 次，防陌生人刷实例占存储（CF-Connecting-IP 生产不可伪造）
    const ip = req.headers.get("CF-Connecting-IP") || "local";
    if (!await this.rateLimit("piip:" + ip, this.n("PAIR_IP_RATE", 5), 3600)) {
      return jresp({ ok: false, error: "rate_limited" }, 429);
    }
    const body = new Uint8Array(await req.arrayBuffer());
    let payload: any;
    try {
      payload = JSON.parse(new TextDecoder().decode(body));
    } catch (_e) {
      return jresp({ ok: false, error: "bad_json" }, 400);
    }
    const iid = String(payload.instance_id || "");
    const key = String(payload.key || "");
    const codeHash = String(payload.code_hash || "");
    if (req.headers.get("X-Instance") !== iid || !IID_RE.test(iid)) {
      return jresp({ ok: false, error: "bad_instance_id" }, 400);
    }
    if (!CODE_HASH_RE.test(key) || !CODE_HASH_RE.test(codeHash)) {
      return jresp({ ok: false, error: "bad_key_or_code_hash" }, 400);
    }
    const existing = await this.store.get("inst:" + iid);
    if (existing && existing.userId) {
      return jresp({ ok: false, error: "already_bound" }, 409);
    }
    if (existing && existing.key !== key) {
      return jresp({ ok: false, error: "key_mismatch" }, 403);
    }
    // 自举签名：用 body 里声明的新密钥校验本次请求
    const err = await this.verifySig(req, iid, key, body);
    if (err) return jresp({ ok: false, error: err }, 401);
    if (!await this.rateLimit("pi:" + iid, RATE_PAIR_INIT, 3600)) {
      return jresp({ ok: false, error: "rate_limited" }, 429);
    }
    if (payload.web_base !== undefined && !sanitizeWebBase(payload.web_base)) {
      return jresp({ ok: false, error: "bad_web_base" }, 400);
    }
    const now = secs();
    const ttl = clampInt(payload.ttl, 60, 900, this.n("PAIR_TTL_S", 600));
    const inst = {
      key,
      codeHash,
      codeExp: now + ttl,
      nickname: sanitizeNick(payload.nickname) || iid,
      rank: payload.rank !== false,
      status: existing ? existing.status : "unbound",
      userId: existing ? existing.userId : null,
      chatId: existing ? existing.chatId : null,
      username: existing ? existing.username || "" : "",
      createdAt: existing ? existing.createdAt : now,
      boundAt: existing ? existing.boundAt : null,
      pendAt: existing ? existing.pendAt || 0 : 0,
      webBase: payload.web_base !== undefined
        ? sanitizeWebBase(payload.web_base)
        : (existing ? existing.webBase || "" : ""),
      panelMid: existing ? existing.panelMid || 0 : 0,
    };
    await this.store.transaction(async () => {
      await this.store.put("inst:" + iid, inst);
      await this.store.put("code:" + codeHash, iid);
    });
    return jresp({
      ok: true, instance_id: iid,
      expires_at: new Date(inst.codeExp * 1000).toISOString(),
      rank: inst.rank,
    });
  }

  // ---------------- /v1/pair/web_base ----------------
  // web_base 变更：已绑定就原地编辑置顶面板（不发新消息），未绑定只存值。

  async onPairWebBase(req: Request): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    let p: any;
    try {
      p = JSON.parse(new TextDecoder().decode(a.body));
    } catch (_e) {
      return jresp({ ok: false, error: "bad_json" }, 400);
    }
    const webBase = sanitizeWebBase(p.web_base);
    if (!webBase) return jresp({ ok: false, error: "bad_web_base" }, 400);
    const inst = a.inst;
    inst.webBase = webBase;
    const panel = (inst.status === "bound" && inst.chatId)
      ? await this.publishPanel(inst) : "skipped";
    await this.store.put("inst:" + a.iid, inst);
    return jresp({ ok: true, panel });
  }

  // ---------------- /v1/pull /ack ----------------

  async onPull(req: Request, url: URL): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    if (!await this.rateLimit("pull:" + a.iid, RATE_PULL, 60)) {
      return jresp({ ok: false, error: "rate_limited" }, 429);
    }
    const after = Math.max(0, Math.floor(Number(url.searchParams.get("after")) || 0));
    // searchParams.get 缺失时返回 null，Number(null)=0 会被 clampInt 夹到 1——必须显式判空
    const lp = url.searchParams.get("limit");
    const limit = lp === null ? 100 : clampInt(lp, 1, PULL_LIMIT_MAX, 100);
    const cutoff = secs() - this.n("QUEUE_TTL_S", 24 * 3600);
    const idx = (await this.store.get("qidx:" + a.iid)) || [];
    const updates: any[] = [];
    const dead: number[] = [];
    for (const qid of idx) {
      const it = await this.store.get(this.qkey(a.iid, qid));
      if (!it || (it as any).at < cutoff) { dead.push(qid); continue; }
      if (qid > after && updates.length < limit) updates.push(it);
    }
    if (dead.length) {
      // 顺手清过期/丢失的条目（索引在事务内重写，防并发丢新入队项）
      await this.store.transaction(async () => {
        const gone = new Set(dead);
        const cur = (await this.store.get("qidx:" + a.iid)) || [];
        await this.store.put("qidx:" + a.iid,
          cur.filter((q: number) => !gone.has(q)));
        await this.store.delete(dead.map((q) => this.qkey(a.iid, q)));
      });
    }
    return jresp({
      ok: true, bound: a.inst.status === "bound", now: secs(), updates,
    });
  }

  async onAck(req: Request): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    let qids: any[];
    try {
      qids = JSON.parse(new TextDecoder().decode(a.body)).qids || [];
    } catch (_e) {
      return jresp({ ok: false, error: "bad_json" }, 400);
    }
    const gone = new Set<number>();
    for (const q of qids.slice(0, PULL_LIMIT_MAX)) {
      const qid = Math.floor(Number(q));
      if (qid > 0) gone.add(qid);
    }
    let n = 0;
    await this.store.transaction(async () => {
      const cur = (await this.store.get("qidx:" + a.iid)) || [];
      await this.store.put("qidx:" + a.iid,
        cur.filter((q: number) => !gone.has(q)));
      for (const q of gone) {
        await this.store.delete(this.qkey(a.iid, q));
        n++;
      }
    });
    return jresp({ ok: true, acked: n });
  }

  // ---------------- /v1/send ----------------

  async onSend(req: Request): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    if (a.inst.status !== "bound" || !a.inst.chatId) {
      return jresp({ ok: false, error: "not_bound" }, 403);
    }
    if (!await this.rateLimit("send:" + a.iid, RATE_SEND, 60)) {
      return jresp({ ok: false, error: "rate_limited" }, 429);
    }
    const ctype = req.headers.get("content-type") || "";
    let method = "", params: any = {}, form: FormData | null = null;
    if (ctype.includes("multipart/form-data")) {
      // authed() 已把 body 读成 bytes 做签名哈希；req.formData() 会报 body 已用，
      // 用缓存的 a.body 重建 Request 再解析（图片字节直接透传，不落存储）
      const fd = await new Request(req.url, {
        method: "POST", headers: req.headers, body: a.body,
      }).formData();
      method = String(fd.get("method") || "");
      try {
        params = JSON.parse(String(fd.get("params") || "{}"));
      } catch (_e) {
        return jresp({ ok: false, error: "bad_params" }, 400);
      }
      form = new FormData();
      for (const [k, v] of Object.entries(params)) {
        form.append(k, typeof v === "string" ? v : JSON.stringify(v));
      }
      for (const [k, v] of fd.entries()) {
        if (k !== "method" && k !== "params") form.append(k, v);
      }
    } else {
      try {
        const payload = JSON.parse(new TextDecoder().decode(a.body));
        method = String(payload.method || "");
        params = payload.params || {};
      } catch (_e) {
        return jresp({ ok: false, error: "bad_json" }, 400);
      }
    }
    if (!SEND_METHODS.has(method)) {
      return jresp({ ok: false, error: "method_not_allowed" }, 400);
    }
    // §10.2：只能发给本 instance 绑定的 chat
    if (params.chat_id !== undefined && Number(params.chat_id) !== Number(a.inst.chatId)) {
      return jresp({ ok: false, error: "chat_forbidden" }, 403);
    }
    const tgUrl = `${this.tgBase()}/bot${this.env.BOT_TOKEN}/${method}`;
    let resp: Response;
    try {
      resp = form
        ? await fetch(tgUrl, { method: "POST", body: form })
        : await fetch(tgUrl, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(params),
        });
    } catch (_e) {
      return jresp({ ok: false, error: "tg_upstream_unreachable" }, 502);
    }
    const text = await resp.text();
    return new Response(text, {
      status: resp.status,
      headers: { "content-type": resp.headers.get("content-type") || "application/json" },
    });
  }

  // ---------------- /v1/profile ----------------
  // bot 全局资料代发：实例签名 + 白名单 4 方法；params 原样转发（无 chat_id 概念）。

  async onProfile(req: Request): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    if (!await this.rateLimit("prof:" + a.iid, RATE_PROFILE, 3600)) {
      return jresp({ ok: false, error: "rate_limited" }, 429);
    }
    let p: any;
    try {
      p = JSON.parse(new TextDecoder().decode(a.body));
    } catch (_e) {
      return jresp({ ok: false, error: "bad_json" }, 400);
    }
    const method = String(p.method || "");
    if (!PROFILE_METHODS.has(method)) {
      return jresp({ ok: false, error: "method_not_allowed" }, 400);
    }
    let resp: Response;
    try {
      resp = await fetch(`${this.tgBase()}/bot${this.env.BOT_TOKEN}/${method}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(p.params || {}),
      });
    } catch (_e) {
      return jresp({ ok: false, error: "tg_upstream_unreachable" }, 502);
    }
    const text = await resp.text();
    return new Response(text, {
      status: resp.status,
      headers: { "content-type": resp.headers.get("content-type") || "application/json" },
    });
  }

  // ---------------- /v1/rank/report + 排行榜 ----------------

  async onRankReport(req: Request): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    let p: any;
    try {
      p = JSON.parse(new TextDecoder().decode(a.body));
    } catch (_e) {
      return jresp({ ok: false, error: "bad_json" }, 400);
    }
    const date = String(p.date || "");
    const today = Math.floor(Number(p.applied_today));
    const total = Math.floor(Number(p.applied_total));
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !(today >= 0) || !(total >= 0)) {
      return jresp({ ok: false, error: "bad_fields" }, 400);
    }
    // §10.3：只存数字，不存公司/岗位/任何文本
    await this.store.put("rank:" + a.iid, {
      date, applied_today: today, applied_total: total, at: secs(),
    });
    // 是否上榜以本人 notify.yaml 的 relay.rank 为准：配对时选错了，改本地配置即可，不必解绑重配
    let rank: boolean | undefined;
    if (typeof p.rank === "boolean") {
      const inst = await this.store.get("inst:" + a.iid);
      if (inst && (inst.rank !== false) !== p.rank) {
        inst.rank = p.rank;
        await this.store.put("inst:" + a.iid, inst);
      }
      rank = p.rank;
    }
    return jresp(rank === undefined ? { ok: true } : { ok: true, rank });
  }

  async rankEntries(): Promise<any[]> {
    // 参与者索引 members（绑定时写入，吊销时移除）；全部点读，不用 list
    const members = (await this.store.get("members")) || [];
    const out: any[] = [];
    for (const iid of members) {
      const inst = await this.store.get("inst:" + iid);
      if (!inst || inst.status !== "bound" || inst.rank === false) continue;
      const r = (await this.store.get("rank:" + iid)) || null;
      out.push({
        nickname: inst.nickname || iid,
        chatId: inst.chatId,
        today: r && r.date === bjDate() ? r.applied_today : 0,
        total: r ? r.applied_total : 0,
      });
    }
    out.sort((x, y) => (y.today - x.today) || (y.total - x.total)
      || String(x.nickname).localeCompare(String(y.nickname)));
    return out;
  }

  async rankText(): Promise<string> {
    const rows = await this.rankEntries();
    const lines = [`🏁 校招投递日榜 · ${bjDate()}`, ""];
    if (!rows.length) {
      lines.push("还没有人上榜。先配对、再投递，明天 21:00 见。");
      return lines.join("\n");
    }
    rows.forEach((r, i) => {
      const medal = MEDALS[i] || `${i + 1}.`;
      lines.push(`${medal} ${r.nickname} — 今日 ${r.today} · 累计 ${r.total}`);
    });
    const top = rows[0];
    lines.push("", top.today > 0
      ? `${top.nickname} 今天领跑。其他人，明天 21:00 榜单见。`
      : "今天全员挂零。明天 21:00 再报，别躺。");
    return lines.join("\n");
  }

  async runCron(): Promise<void> {
    const rows = await this.rankEntries();
    if (!rows.length) return;
    const text = await this.rankText();
    for (const r of rows) {
      if (r.chatId) await this.tgSend(r.chatId, text);
    }
  }

  // ---------------- /v1/unpair + admin ----------------

  async onUnpair(req: Request): Promise<Response> {
    const a = await this.authed(req);
    if (a instanceof Response) return a;
    await this.removeInstance(a.iid);
    return jresp({ ok: true });
  }

  async onAdminRevoke(req: Request): Promise<Response> {
    if (!this.isAdmin(req)) return jresp({ ok: false, error: "unauthorized" }, 401);
    let iid = "";
    try {
      iid = String((await req.json()).instance_id || "");
    } catch (_e) {
      return jresp({ ok: false, error: "bad_json" }, 400);
    }
    const ok = await this.removeInstance(iid);
    return jresp({ ok: true, revoked: ok ? iid : null });
  }

  async onAdminList(req: Request): Promise<Response> {
    if (!this.isAdmin(req)) return jresp({ ok: false, error: "unauthorized" }, 401);
    // 单次 list 全量扫，内存里按前缀分组（同请求二次 list 会被截断）
    const all = await this.store.list();
    const insts = new Map<string, any>();
    const queued = new Set<string>();
    for (const [k, v] of all) {
      if (k.startsWith("inst:")) insts.set(k.slice(5), v);
      else if (k.startsWith("q:")) queued.add(k.split(":")[1]);
    }
    const out: any[] = [];
    for (const [iid, inst] of insts) {
      out.push({
        instance_id: iid, nickname: inst.nickname, user_id: inst.userId,
        chat_id: inst.chatId, bound_at: inst.boundAt || null,
        status: inst.status || (inst.userId ? "bound" : "unbound"),
        rank: inst.rank !== false, pair_pending: !!inst.codeHash,
        queued: queued.has(iid) ? "nonempty" : "empty",
      });
    }
    return jresp({ ok: true, instances: out });
  }

  isAdmin(req: Request): boolean {
    const k = req.headers.get("X-Admin-Key") || "";
    return !!this.env.ADMIN_KEY && eqConst(k, this.env.ADMIN_KEY);
  }
}
