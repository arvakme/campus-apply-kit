# 数据源适配器：公告从哪来

框架本身不生产公告数据，也**不附带任何第三方招聘看板的数据或取数脚本**——那些内容属于各自的平台，怎么获取、能不能自动化，取决于你和平台之间的使用条款。

框架只认一个文件契约（`docs/contracts.md` §4）：

```
data/paperball/announcements.jsonl   # 每行一条公告，按 announcement_id 升序
data/paperball/meta.json             # {"synced_at": ISO时间, "count": N, ...}
```

所有读取方都写死了这个路径，别改。这个目录在 `.gitignore` 里，数据只留在你本机。

## 先跑通

```bash
cp -r data/sample data/paperball     # 12 条虚构公告
uv run host/scan.py                  # 出候选清单
```

## 接你自己的数据源

写一个脚本，把你能合法拿到的公告（自己整理的表格、学校就业网导出、你有权限使用的接口……）转成上面的 jsonl。每条的字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `announcement_id` | int | 在你的数据源内唯一、稳定；台账和防重投都靠它 |
| `title` / `company` | str | 公告标题、公司名（`company` 要和 `data/tiers.yaml` 的写法一致才能算层级分） |
| `company_tags` | [str] | 行业/性质/规模标签，如 `["银行/金融","国企"]`；含「国企」「事业单位」会被判为国企；含 `一线大厂` / `冷门大厂` / `腰部名企` 的公司，投递池会停在提交前等你确认 |
| `class_types` | [int] | 届数枚举，和 `intent.yaml` 的 `class_types` 对应（自己定义，保持一致即可） |
| `published_at` / `expired_at` | "YYYY-MM-DD" / null | 发布日、截止日（截止日影响紧迫度加分） |
| `degrees` | [str] | 如 `["本科","硕士"]` |
| `original_jobs` | str | 岗位原文，顿号或逗号分隔；筛选的关键词命中靠它 |
| `link` | str | 公告原文链接 |
| `from_url` | str | 投递入口。门户链接，或 `邮箱投递：hr@example.com（主题格式…）` 这样的文本（会被识别为邮件投递） |
| `written_test` | str / null | 笔试说明 |
| `cities` | [str] | 城市名数组，`全国` 原样保留；`intent.yaml` 的城市偏好靠它 |

把脚本放在 `host/sync-source.py`（现在是个占位文件），`host/daily-sync.sh` 每天会调用它：退出码 0 = 成功；非 0 = 失败并发通知，已有数据不动。请控制抓取频率，别拿去再分发。
