<!-- 数据回流写入约定（contracts §9）。主持人把本节并进 templates/worker-task.md；
     worker 照此写事件与 outcome，tracker/assessments/看板由脚本生成，不要手写。 -->

## 结构化回写（在留痕之外必须照写；tracker.md / assessments.md 由脚本生成，不要手改）

命令入口：`uv run {{REPO}}/host/events.py`（参数错误会给中文报错；--instance 默认 $CAMPUS_INSTANCE）。

### 事件流 `state/events.jsonl`——到点就写，一行一条

```bash
uv run {{REPO}}/host/events.py event --job {{ANNOUNCEMENT_ID}} --step <step> --msg "一行人话" [--portal <短名>] [--data k=v ...]
```

| step | 什么时候写 | 常带的字段 |
|---|---|---|
| `claim` | 接单开工 | --msg "接单，开 TaskSpace N" |
| `portal_detected` | 认出门户 | --portal beisen/moka/… |
| `account_ready` | 登录/注册完成 | --msg 登录方式 |
| `filling` | 开始填表 | --data page=字段页 |
| `field_blocked` | 遇到缺字段 | --data field=字段名 |
| `gate` | 遇到关卡（先调 notify.py handoff，再写本事件） | --data kind=wechat_qr --data handoff=<id> |
| `submitted` | 门户提示提交成功 | --data candidateId=… |
| `verified` | 门户"我的投递"回读到 | --msg 回读状态 |
| `failed` | 失败或放弃 | --msg 原因，随后写 outcome |
| `note` | 其他想说清的进展 | --msg 自由文本 |

### 终局 outcome——每单只写一次，写完再回执

```bash
uv run {{REPO}}/host/events.py outcome --job {{ANNOUNCEMENT_ID}} --result submitted \
  --company {{COMPANY}} --portal beisen --job-applied "实际岗位名" \
  --portal-status "门户回读状态" --candidate-id "若有" --deadline 2026-10-06 \
  --evidence log/ops/{{BATCH}}-{{ANNOUNCEMENT_ID}}.md --evidence log/shots/{{ANNOUNCEMENT_ID}}-done.png
```

- `--result`：`submitted`（含门户已回读的）| `blocked` | `skipped` | `failed`；后三个**必须** `--reason`
- `submitted` 尽量带 `--portal-status`/`--candidate-id`/`--submitted-at`（缺省取当前时间）
- 不合适/已投过 → `skipped --reason "…"`；待材料 → `blocked --reason "待材料：需XX"`
- 额外字段用 `--set k=v`（如 `--set note=第1志愿`）

### 测评/笔试/面试——门户页面看到就写 `--next-step`（可重复）

```bash
  --next-step "kind=assessment,due_at=2026-10-01T18:00:00+08:00,link=https://…,note=72h 内完成"
```

kind：assessment / written_test / interview / offer。写了会自动并进 `state/next-steps.jsonl`
（与邮件待办去重），进 assessments 表和日历；**不要**只靠留痕文本记期限。

### 与 dispatcher 信号的关系

事件/outcome 是给台账和看板的数据源，**不替代**现有信号文件：handoff 记录、
`log/fields/<id>.md`、回执照写照发。worker 只写 `{{INSTANCE}}/log/` 与 `{{INSTANCE}}/state/` 下本单文件。
