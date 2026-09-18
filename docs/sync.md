# 同步更新

仓库只放框架和公共数据，个人实例在仓库外，所以 `git pull` 永远不会覆盖或冲突你的 profile、问题库、台账。

## 1. 什么会更新

| 内容 | 谁改 | 频率 | 你要做什么 |
|---|---|---|---|
| `data/paperball/`、`data/tiers.yaml` | 维护者 Mac 上的 `host/sync-source.py` 自动提交 | 每天 1 次 | `git pull` 后重跑 `host/scan.py` |
| `docs/ portals/ templates/ host/ web/ scripts/` | 维护者 | 攒一批发一次，写进 `CHANGELOG.md` | 按 CHANGELOG 执行 |
| 实例目录 | 你自己 | 随时 | 不同步 |

## 2. 每天：拿新公告

装了定时同步（§3）的同学不用手动跑下面三条，脚本会自动 pull + scan 并推送“新增 N 家”。

```bash
git -C $CAMPUS_REPO pull --ff-only
cat $CAMPUS_REPO/data/paperball/meta.json        # 看 synced_at 和 count
uv run $CAMPUS_REPO/host/scan.py                 # 参数见 --help
```

`meta.json` 的 `synced_at` 超过两天没变，说明维护者那边同步停了，在群里说一声。

## 3. 定时自动同步（launchd）

两个人都装，但装的角色不同：

| 角色 | 安装命令 | 触发时机 | 做什么 |
|---|---|---|---|
| 维护者 | `uv run $CAMPUS_REPO/host/install-schedule.py install --role maintainer` | 每天 01:00 | `host/daily-sync.sh`：`git pull --ff-only` → `sync-source.py` → 抓增量 JD（`host/fetch-jd.py` 存在才跑）→ 只提交 `data/` 并 push |
| 同学 | `uv run $CAMPUS_REPO/host/install-schedule.py install --role member` | 每天 08:00 / 11:00 / 14:00 / 18:00 / 22:00 + 开机登录补一次 | `host/pull-update.sh`：有新提交才 `pull --ff-only`；`data/` 变了自动跑 `scan.py`（+ `judge.py` 若存在）并推送“新增 N 家”；`CHANGELOG.md` 变了推送“框架有更新，打开 Codex 说 同步更新” |

- 安装前先 `export CAMPUS_REPO=... CAMPUS_INSTANCE=...`：launchd 下没有 shell 环境，两者会写死进 plist。
- 手动跑一次：`bash $CAMPUS_REPO/host/daily-sync.sh`（维护者）或 `pull-update.sh`（同学）。
- 看状态：`uv run $CAMPUS_REPO/host/install-schedule.py status`（可加 `--role`），exit≠0 表示没装或没加载。
- 卸载：`uv run $CAMPUS_REPO/host/install-schedule.py uninstall --role <角色>`。
- 日志：`$CAMPUS_INSTANCE/state/daily-sync.log` / `pull-update.log`（脚本日志，保留 30 天）；`*.launchd.log` 是 launchd 的 stdout/stderr。
- 两个脚本幂等、带锁防重入：没有新数据/新提交就静默退出。同学侧工作区脏或分支分叉时不动仓库，同一问题 24 小时内只通知一次。
- 维护者注意：MacBook 合盖睡眠会错过 01:00，launchd 会在唤醒后补跑；想准点可 `sudo pmset repeat wake MTWRFSU 00:55:00`。公开版的 `host/sync-source.py` 是占位文件，先按 `adapters/sources/README.md` 写好你自己的适配器再装这个角色；`data/paperball/` 不进 git，所以「提交并 push」这一步在公开版里不会发生。
- 不想装定时任务也可以，§2 的手动流程依然有效。

## 4. 框架更新：“同步更新”流程

在仓库根目录对 Codex 说“同步更新”，它按下面执行；手动也一样：

1. **确认工作区干净**：`git -C $CAMPUS_REPO status --porcelain`。有本地改动先问用户，不 stash、不 reset。
2. **记下当前版本**：`git -C $CAMPUS_REPO rev-parse HEAD`。
3. **拉取**：`git -C $CAMPUS_REPO pull --ff-only`。失败（分叉）就停，报给用户。
4. **读 CHANGELOG 新增部分**：`git -C $CAMPUS_REPO diff <旧HEAD> HEAD -- CHANGELOG.md`。逐条列出“你需要做”的事项。
5. **检查模板新增必填项**：

   ```bash
   git -C $CAMPUS_REPO diff <旧HEAD> HEAD --stat -- templates/
   git -C $CAMPUS_REPO diff <旧HEAD> HEAD -- templates/intent.dev.yaml templates/intent.pm.yaml templates/question-bank.example.yaml templates/profile.template.md templates/notify.example.yaml
   ```

   - intent 模板新增字段：对照实例 `intent.yaml`，缺的字段逐个问用户，写入实例。
   - 问题库模板新增 `required: true` 条目：经 `host/qb.py` 加进实例问题库，answer 问用户。
   - 新增规则（`rules`）：展示给用户，用户同意再加。
6. **依赖与容器**：`host/` 或 `web/` 有变化时，按 CHANGELOG 说明重建 web 容器；uv 脚本的依赖会在下次 `uv run` 时自动安装。
7. **自检**：`host/host-check.py`（落地后）全绿；web `/setup` 全绿。
8. **汇报**：本次更新了什么、你已经做了什么、还需要用户做什么。

## 5. 不会发生的事

- 实例文件被覆盖：实例目录不在仓库里。
- 你的问题库答案被同步给别人：问题库只存在实例里，仓库只有无答案的模板。
- 正在跑的投递被打断：框架更新不影响已派出的 worker；但重建 web 容器期间看板页会短暂不可用。

## 6. 同学反馈改进

发现门户新坑或规则问题：整理成去掉个人信息的描述发给维护者，或者提 PR 改 `portals/`。提交前跑：

```bash
python3 $CAMPUS_REPO/scripts/check_secrets.py --instance $CAMPUS_INSTANCE
```

0 命中才能提交。
