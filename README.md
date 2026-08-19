# x-operator

通用型 X（推特）运营半自动化工具：定时发推、监控推主回复、语义搜索回复、LLM 素材匹配与撰写、多语言+自动翻译、人工审核队列。

- 完整规格：[docs/design.md](docs/design.md)（v1.0）
- 旧版窄范围草稿（已被取代，仅存档）：docs/archive/twitter-autopost-spec-v0.1.md

---

## WebUI MVP（当前分支）

这是一个**可立即在浏览器里跑通全流程的最小可用版**，用于边测边改边优化。核心设计：

- **零凭据、零封号风险即可测**：默认 Mock 演示模式，所有 X 抓取/发送走假适配器，
  不碰真实推特。整条流水线（监控 → 打分 → 匹配 → 审核 → 发送）都能点起来看效果。
- **LLM 可选**：设置页填了 OpenAI 兼容网关（apimax）就用真实 LLM 打分/匹配；
  不填则用内置启发式规则兜底，完全离线也能演示。
- **表结构/模块划分严格对齐 design-v1.1**，方便这个 MVP 平滑长成完整版。

### 快速开始

Windows 双击 `start.bat`（需先装 `uv`），或命令行：

```bash
uv sync
uv run python -m x_operator.main
```

浏览器打开 http://localhost:8080。首次启动会自动建库并写入一批演示数据
（一个 Mock 账号、几条中日英回复素材、一个监控推主、一条搜索规则）。

### 建议的测试路径

1. **仪表盘** → 点「运行监控轮询」「运行语义搜索」，看抓取/打分/入队统计。
2. **审核队列** → 逐条看 AI 理由、编辑文案、点「批准」，再点「触发发送」，
   观察状态流转到 sent。
3. **搜索规则** → 点某条规则的「试运行」，看候选推文的相关性打分与达标情况。
4. **素材库 / 监控推主 / 定时计划** → 增删改，验证数据落库。
5. **设置** → 关掉「Mock 演示模式」需要真实凭据（当前为占位）；填 LLM 网关后
   打分/匹配会切成真实 LLM；可打开「后台自动轮询」让 job 按间隔自跑。

### 目录结构

```
x_operator/
  db/         schema.py(完整 DDL) database.py(连接/迁移) seed.py(默认设置+演示数据)
  adapters/   base.py(异常/数据类/抽象基类) mock.py(Mock 适配器) real.py(tweepy/twifork 占位) factory.py
  llm/        prompts.py(相关性/匹配 prompt) client.py(网关调用+启发式兜底)
  core/       compliance.py matcher.py monitor.py search.py dispatcher.py scheduler.py schedule_calc.py
  ui/         layout.py + 7 个页面(dashboard/queue/materials/watched/rules/schedule/settings)
  config.py   main.py
config/settings.toml   start.bat / start.sh
```

### MVP 与完整 spec 的差距（后续迭代）

- 真实 X 适配器（official=tweepy / unofficial=twifork）当前是占位，接凭据后补
  `adapters/real.py` 即可，上层无需改动。
- 媒体上传、翻译组并排视图、AI 撰写弹窗、预算熔断/月度重算、启动补扫的定时补发
  宽限逻辑等为简化版或待补。
- cron 表达式暂只支持 `M H * * *`（每日固定时分）。

> 请只在你有权操作的账号上使用，遵守 X 服务条款与所在地法律；自动化回复存在封号风险，
> 非官方通道尤甚，务必保守限速。
