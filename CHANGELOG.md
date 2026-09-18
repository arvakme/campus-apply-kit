# CHANGELOG

`git pull` 前先看这里。每条写清"改了什么"和"你需要做什么"。实例目录不受任何更新影响。

## 1.0.0 · 2026-09-18

第一个公开版本。此前在几个人的小范围里跑过一整轮秋招（数百家公司的门户投递 + 邮件投递），这里是去掉个人信息和公告数据之后的框架本体。

### 包含

- 候选筛选（`host/scan.py`）、看板 web、事件与防重投登记
- 投递池（`host/slot-pool.py`）：固定 N 个浏览器槽位，一个 agent 一家公司，限流自适应
- 人工关卡：扫码/验证码/登录/终审经 Telegram（或 Bark）推到手机，排队、两段式推送、浏览器空间校验
- 门户 playbook（Moka、飞书、北森等）与批量填表 skill（`skills/fill-moka`）
- 邮件投递：起草 → 校验 → 限速发送
- 自动热更新（`host/pull-update.sh`）、实例本地备份

### 不包含

- **公告数据和取数脚本**：仓库里只有 `data/sample/` 的虚构示例；真实公告按 `adapters/sources/README.md` 的字段契约接你自己的数据源，数据只留本机
- **共享 Telegram bot**：默认走你自己的 bot（`mode: direct`）；几个人共用一个 bot 才需要自己部署 `relay/`

### 你需要做什么

照 README 的 L1 走一遍。
