// adapters/sources/jd-fetch.mjs · 公告 JD 浏览器兜底抓取（ego-browser 脚本）
//
// 由 host/fetch-jd.py 调用：它在本文件前面拼一行
//   globalThis.__JDFETCH_CFG__ = {"jobs":[{"id":10001,"url":"https://…"}],
//                                 "out_dir":"/tmp/jdfetch-xxx","interval_ms":1500};
// 再整体通过 stdin 交给 `ego-browser nodejs` 执行。也可手工调试：
//   (echo 'globalThis.__JDFETCH_CFG__={"jobs":[{"id":1,"url":"https://example.com"}],"out_dir":"/tmp/jd"};';
//    cat adapters/sources/jd-fetch.mjs) | ego-browser nodejs
//
// 约束（见 adapters/sources/jd-fetch.md）：
// - 自建 TaskSpace，结束 finish({keep:[]})，不碰 worker 的 TaskSpace，不导出 cookie
// - 同一 Page 顺序 goto，请求间隔 >= 1000ms
// - 每条把正文文本写到 <out_dir>/<id>.txt，汇总写 <out_dir>/result.json
// - 判断状态：ok / login_required（登录墙、环境异常、验证码）/ gone（已删除）/
//   image_only（长图公告）/ missing（其余失败）

const DEFAULTS = { jobs: [], out_dir: null, interval_ms: 1500, timeout_ms: 25000 };
const cfg = { ...DEFAULTS, ...(globalThis.__JDFETCH_CFG__ || {}) };
cfg.interval_ms = Math.max(1000, Number(cfg.interval_ms) || 0);
if (!cfg.out_dir) throw new Error("__JDFETCH_CFG__.out_dir 未设置");

const fs = await import("node:fs/promises");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log("[jd-fetch]", ...a);

// 页面内提取：优先微信正文容器，其次常见正文容器，否则取 innerText 最大的块。
// 只能访问参数，不能访问外层变量。
function extractInPage() {
  const GONE = ["该内容已被发布者删除", "此内容已被发布者删除", "该内容已被删除",
    "链接已过期", "参数错误", "文章已被删除"];
  const AUTH = ["环境异常", "完成验证", "操作频繁", "访问过于频繁", "登录后查看",
    "请先登录", "验证中心"];
  const CSS = ["#js_content", "article", ".article-content", ".article_content",
    "#content", ".content", ".detail-content", "main"];
  const clean = (t) => (t || "").replace(/[ \t ]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  let node = null;
  for (const css of CSS) {
    const el = document.querySelector(css);
    if (el && (el.innerText || "").trim().length > 200) { node = el; break; }
  }
  if (!node) {
    let best = document.body, bestLen = 0;
    for (const el of document.querySelectorAll("div,section,article")) {
      const ln = (el.innerText || "").trim().length;
      if (ln > bestLen) { best = el; bestLen = ln; }
    }
    node = best;
  }
  const text = clean(node ? node.innerText : "");
  const imgs = node ? node.querySelectorAll("img").length : 0;
  const img_urls = node
    ? [...node.querySelectorAll("img")]
        .map((im) => im.getAttribute("data-src") || im.getAttribute("src") || "")
        .map((u) => (u.startsWith("//") ? "https:" + u : u))
        .filter((u) => u.startsWith("http"))
        .slice(0, 30)
    : [];
  const head = text.slice(0, 2000);
  let status = "ok";
  if (GONE.some((m) => head.includes(m))) status = "gone";
  else if (AUTH.some((m) => head.includes(m))) status = "login_required";
  else if (text.length < 300) status = imgs >= 3 ? "image_only" : "missing";
  return { status, text, imgs, img_urls, url: location.href, title: document.title || "" };
}

const result = { ok: true, started_at: new Date().toISOString(), results: [] };
const task = await taskSpace("campus-apply jd fetch");
log("taskSpace", task.spaceId, "jobs", cfg.jobs.length);
try {
  const page = task.page("p1");
  let lastAt = 0;
  for (const job of cfg.jobs) {
    const wait = lastAt + cfg.interval_ms - Date.now();
    if (wait > 0) await sleep(wait);
    lastAt = Date.now();
    const ent = { id: job.id, status: "missing", imgs: 0, chars: 0 };
    try {
      if (!/^https?:\/\//.test(job.url || "")) {
        ent.status = "missing";
      } else {
        await page.goto(job.url, { timeout: cfg.timeout_ms });
        await page.waitForLoadState();
        const r = await page.evaluate(extractInPage);
        ent.status = r.status;
        ent.imgs = r.imgs;
        ent.img_urls = r.img_urls;
        ent.url = r.url;
        ent.title = r.title;
        if (r.text) {
          await fs.writeFile(`${cfg.out_dir}/${job.id}.txt`, r.text);
          ent.chars = r.text.length;
        }
      }
    } catch (e) {
      ent.status = "missing";
      ent.error = String(e).slice(0, 200);
    }
    result.results.push(ent);
    log(job.id, ent.status, ent.chars);
  }
} catch (e) {
  result.ok = false;
  result.error = String((e && e.stack) || e).slice(0, 500);
} finally {
  result.finished_at = new Date().toISOString();
  await fs.mkdir(cfg.out_dir, { recursive: true });
  await fs.writeFile(`${cfg.out_dir}/result.json`, JSON.stringify(result));
  await task.finish({ keep: [] });
  log("done", { ok: result.ok, n: result.results.length });
}
