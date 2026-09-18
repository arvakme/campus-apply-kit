# 数据回流：事件流 → 台账/看板自动生成

> 原则：**人不手写台账**。worker 每步写结构化数据，dispatcher 只认标记文件，
> tracker.md / assessments.md / 看板区块全部由脚本生成。契约见 `docs/contracts.md` §9（规格真源），
> 本文是链路网图 + 迁移步骤 + 排障。

## 1. 全链路

```
worker（每单一 TaskSpace）
  │  每到一个节点调 host/events.py event --step …        ──┐
  │  终局调 host/events.py outcome --result …            │ 写（mkdir+flock 锁 / 原子替换）
  ▼                                                      ▼
$INSTANCE/state/events.jsonl        $INSTANCE/state/outcomes/<id>.json
  │                                      │ next_steps 同步追加 → state/next-steps.jsonl
  │                                      ▼                  （与 mail.py 邮件待办同口径去重）
  │  dispatcher 每轮读 outcome/fields/handoff 推进作业 → state/jobs/<id>.json
  ▼                                      │
host/render-tracker.py 读 jobs+outcomes+registry+events+next-steps
  │  生成 tracker.generated.md / assessments.generated.md（迁移期）→ --write-live 后写 tracker.md/assessments.md
  ▼
web 看板：dashboard.py 读 tracker.md；web/app/live.py 读 jobs+events+handoffs 出「在跑批次」区块
```

| 文件 | 谁写 | 谁读 |
|---|---|---|
| `state/events.jsonl` | worker（events.py event） | live.py「在跑批次/事件流」、render-tracker（记录列）、`events.py tail/stats` |
| `state/outcomes/<id>.json` | worker（events.py outcome） | dispatcher 定终局、render-tracker |
| `state/next-steps.jsonl` | mail.py、events.py（outcome 的 next_steps 镜像） | render-tracker → assessments、schedule.py → 日历 |
| `state/jobs/<id>.json` | dispatcher | live.py、render-tracker |
| `tracker.md` / `assessments.md` | render-tracker（--write-live） | dashboard.py、registry.parse_tracker、人 |

## 2. worker 侧命令速查

```bash
uv run host/events.py event --job <id> --step claim|portal_detected|account_ready|filling|\
  field_blocked|gate|submitted|verified|failed|note [--portal X] [--msg ..] [--data k=v ..]
uv run host/events.py outcome --job <id> --result submitted|blocked|skipped|failed \
  [--company X --portal X --job-applied X --portal-status X --candidate-id X --deadline YYYY-MM-DD] \
  [--next-step "kind=assessment,due_at=ISO,link=URL,note=.."] [--evidence path] [--reason ..] [--set k=v]
uv run host/events.py show --job <id>     # 单作业 outcome + 全事件
uv run host/events.py tail [--n 50]       # 最近事件
uv run host/events.py stats [--since 1d]  # 按步骤/结果/天汇总
```

worker 写法的完整约定在 `templates/worker-outcome.md`（主持人并进 `worker-task.md`）。

## 3. 生成器

```bash
uv run host/render-tracker.py [--instance DIR]          # 写 tracker.generated.md / assessments.generated.md
uv run host/render-tracker.py --check                   # 与手写版逐行比对，差异退出码 1，只读不写
uv run host/render-tracker.py --write-live              # 覆盖 tracker.md / assessments.md（切换开关）
uv run host/render-tracker.py --stdout                  # 只打印不落盘（调试用）
```

生成口径（§9.3）：
- **tracker**：行 = jobs ∪ outcomes ∪ registry 的公告集合；状态列由 outcome.result 优先、
  其次 job.status 映射（gate_wait→「填表中（等用户·x）」、blocked→「待投递·阻塞」、
  skipped 带"不合适"字样→「不合适」否则「已停止」）；记录列 = evidence + 门户状态 + 测评提示。
- **assessments**：next-steps.jsonl 按 id 折叠（后写覆盖先写）∪ outcomes.next_steps
  （按确定性 id 去重，再按公告+kind+链接/期限去重）；superseded/cancelled 不上表。
- 表头与手写版一致，`dashboard.py` / `registry.parse_tracker` 可直接读生成文件。

## 4. 迁移步骤（手写 → 生成）

1. 先跑一段双写期：worker 工单并入 `templates/worker-outcome.md`，新单开始写 events/outcomes；
   历史已投行一次性回填 `uv run host/registry.py import-tracker <实例>/tracker.md`（先 --dry-run 看数）。
2. `uv run host/render-tracker.py` 生成 `*.generated.md`，肉眼对几行。
3. `uv run host/render-tracker.py --check` 对账。差异分三类处理：
   - **只在手写版**：state/ 里没有对应作业/outcome/registry——历史行回填 registry 后进生成版；
     仍在跑但没写结构化数据的单让 worker 补事件/outcome。
   - **只在生成版**：手写漏行，直接认生成版。
   - **共有行字段不一致**：多为手写自由文本（「关键日期」「记录」列），逐条看状态桶是否一致；
     状态不一致以门户回读为准改结构化数据，不改生成文本。
4. 差异收敛到可解释后 `--write-live` 切换，手写版从此只读；看板继续读 tracker.md 无感。
5. 切换后 `--check` 进 CI/每日巡检：差异再出现说明有人手写了 tracker——找到人，停手。

## 5. web 接线

`web/app/live.py` 只读实例 state/，容器可用。加独立路由 3 行（`do_GET` 里）：

```python
import live                                          # server.py 顶部
if path == "/live":                                  # do_GET 路由段
    return self._send(200, live.page(INSTANCE))
```

要嵌进现有看板页而不是单开路由，则渲染处调 `live.render_fragment(live.build(INSTANCE))`
拿到 `<section>` 片段拼进页面。片段自带 `<style>`，不依赖外部 CSS。

## 6. 排障

| 现象 | 查哪里 | 处理 |
|---|---|---|
| 事件没进 events.jsonl | worker 侧 events.py 输出 | 命令报中文错按提示改；`events.py tail` 验证 |
| events.jsonl 追加卡住 | `state/events.jsonl.lockdir` 残留（锁超时 60s 自动清理） | 确认无进程在写后删 `.lockdir` 目录 |
| jsonl 里有坏行 | 读取端自动跳过坏行 | 手工修或留空，不阻断 |
| outcome 写错 | 重跑 `events.py outcome` 同 id 覆盖（最后一版为准） |  dispatcher 每轮重读，无缓存 |
| next-steps 重复/过期 | `state/next-steps.jsonl` 同 id 后写覆盖；同公司同 kind 旧 open 自动 superseded | 手工把该 id 追加一条 status=done/cancelled |
| tracker.generated 与手写差异 | `--check` 输出三类清单 | 按 §4.3 分类处理，不逐字对齐自由文本列 |
| 看板「在跑批次」空 | state/jobs 是否有非终态作业；events.jsonl 是否存在 | live.py 只显示在跑；终态作业看 tracker |
| --write-live 覆盖后想回滚 | 手写版属个人实例，建议切换前 `cp tracker.md tracker.md.bak` | 生成器可反复跑，随时重生成 |

红线不变：worker 不改 tracker/assessments 本体；提交权限、handoff、防重投照旧
（docs/redlines.md、contracts §8）。本链路只替代"写字"，不替代"决定"。
