# 中途接手：还原进度

适用场景：主持人会话断了、换了 agent、隔天继续、worker pane 挂了。目标是在不重复提交的前提下，搞清楚每一单现在在哪。

接手的不必是原来的 agent——同学用 Codex / Claude Code / Devin 哪个都行，入口都是 `AGENTS.md`，本节流程与具体 agent 无关。

## 1. 证据从哪来

| 证据 | 路径 | 可信度 | 用途 |
|---|---|---|---|
| 门户“我的投递/应聘记录” | 各门户，入口见 `portals/*.md` 的“提交后核验” | 最高 | 判断是否已投递的唯一硬证据 |
| 台账 | `$CAMPUS_INSTANCE/tracker.md` | 高 | 每家公司的当前状态、关键日期、记录路径 |
| 批次文件 | `log/B-xx.md` | 中 | 这批派了哪些单、哪个 task、哪个 pane |
| 操作留痕 | `log/ops/B-xx-<id>.md` | 中 | 门户 URL、账号方式、逐步操作、停在哪一步、TaskSpace 编号 |
| 查岗/执行记录 | `log/B-xx-<id>.md`、`log/B-xx-<id>-apply.md` | 中 | 岗位选择理由、投递方式 |
| seedmux 任务目录 | `~/.seedmux/team/tasks/T-*/` | 中 | `prompt.md` 工单原文、`reply.md` 回执、`meta.json` 状态 |
| handoff 记录 | `state/handoffs/<id>.json` | 中 | 等用户做什么、是否已处理 |
| 看板 | 你自己的看板（若有） | 低 | 可能滞后或写失败，只作辅证 |
| 截图 | `log/shots/<id>-*.png` | 辅证 | 停手时页面长什么样 |

## 2. 还原步骤

### 2.1 列出未闭环的单

1. `tracker.md` 里状态不是 已投递 / 不合适 / 已停止 的行，全部列出来。
2. 找编号最大的 `log/B-xx.md`，再往前翻一批，把“已派单”“查岗中”“填表中”“待用户…”的行合并进来。批次文件末尾常有“同时在跑”一行，记录跨批遗留，别漏。
3. 用 announcement_id 去重，得到待核对清单。

### 2.2 每单查三处

对清单每一行：

```bash
T=T-xxxxxx   # 从 B-xx.md 的“工单 / pane”列取
python3 -c 'import json,sys;m=json.load(open(sys.argv[1]));print(m["status"],m["to_pane"])' ~/.seedmux/team/tasks/$T/meta.json
test -f ~/.seedmux/team/tasks/$T/reply.md && head -5 ~/.seedmux/team/tasks/$T/reply.md
~/.seedmux/bin/smx-team panes          # to_pane 还在不在，是否 idle
```

然后读 `log/ops/B-xx-<id>.md` 的最后一节（通常叫“停在哪一步”或“阶段 N”），记下：TaskSpace 编号、停在哪个页面、提交按钮点没点、在等谁。

`meta.json` 的 `status` 常见值：`dispatched`（已派未回执）、`replied:done`、`replied:blocked`、`replied:failed`。注意 `replied:done` 只说明 worker 认为完成，不代表已投递。一单查岗完成停在注册墙，也可能回执 done。

### 2.3 需要看现场时

```bash
~/.seedmux/bin/smx-team capture <pane> -n 200
```

看 worker 最后在做什么。浏览器空间用 ego-browser 查看 TaskSpace 列表，不要 `takeOver` 用户正在操作的空间。

## 3. 归类

| 类别 | 判定条件 | 下一步 |
|---|---|---|
| 在跑 | meta 为 `dispatched`，pane 存在且非 idle，capture 有近期输出 | 不动，等回执 |
| 空转 | meta 为 `dispatched`，pane idle，capture 停在输出截断或等待输入 | nudge：`smx-team send --to <pane> --text '[team-msg task=T-xx] 继续按工单执行，先读 log/ops/B-xx-<id>.md 最后一节'` |
| 等用户 | 留痕停在注册墙、扫码、验证码、终审；或 `state/handoffs/` 有未处理记录 | 确认已调用 `notify.py handoff`，没有就补发；不重复发 |
| 卡住待决 | `replied:blocked`，或留痕写“已问主持人” | 从 reply.md 提取问题，汇总给用户；答案进问题库后再放行 |
| 待材料 | tracker 标“待投递·待材料” | 查实例 `materials/` 是否已补齐，齐了才重派 |
| 已闭环未回写 | 门户回读有投递记录，但 tracker 或看板没改 | 只补回写和留痕，禁止再点提交 |
| 需要重派 | pane 已不存在或 `replied:failed`，且门户回读确认没有投递记录 | 新开 worker，工单写明“接手 T-xx，先读 ops 留痕”，复用原 TaskSpace 编号（若还在） |

## 4. 不得重投的判断

以下任一成立，这一单就不能再进入提交步骤：

1. 门户“我的投递/应聘记录/投递记录”里有该岗位或同雇主主体的同类岗位。
2. 岗位详情页按钮已变为“已投递”“已申请”“取消投递”或置灰。
3. 留痕或回执里出现“投递成功”“已成功提交”“恭喜你完成投递”“thanks 页”等提交后页面描述。
4. 你自己的看板（若有）里这条公告已标「已投递」，备注写了提交时间。
5. 门户点投递返回“您已申请过此职位”。

判断时注意：

- **按雇主主体查**，不是按公告查。集团公告与子公司公告、集团门户里的子公司岗与子公司自有门户岗，都算同一雇主。
- **“确定”不等于投递**。有的门户先“提交简历”再“投递岗位”两段式，简历保存成功不代表岗位已投。用第 1、2 条核实。
- **即时回显不可信**。提交后页面没刷新就判断成功，会误判。刷新后再看记录。
- 核验不了（掉登录、门户挂了）时，状态记“待核验”，不重投，交给用户。

## 5. 接手后第一份汇报

汇报给用户的表格，至少包含：

| 公司 | id | 类别 | 证据 | 下一步 | 需要你做 |
|---|---|---|---|---|---|

写清证据来自哪里（门户回读 / 留痕 / 回执）。用户确认前不派新单，不点提交。

## 6. 写回

- 状态有变化，先改 `tracker.md`，再改批次文件，再回写看板。
- 在对应 `log/ops/` 留痕末尾追加一节“接手核验（日期）”，写查了什么、结论是什么。
- 新发现的门户经验，整理后写进 `portals/`，去掉个人信息。
