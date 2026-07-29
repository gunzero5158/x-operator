# x-operator 开发任务清单（对应 Spec v1.0 §13 六个 Phase）

> 依据：[design.md](design.md)（v1.0）+ [design-v1.1.md](design-v1.1.md)（实施级细化，下称 v1.1）。
> 用法：按序开发，每完成一项勾掉；「验收点」全部满足才算完成该任务；每个 Phase 末尾的门槛 = v1.0 §13 验收标准。
> 任务编号 `P<Phase>-<序号>`；依赖只列直接前置。

---

## Phase 0 骨架（~2 天）

- [ ] **P0-1 工程初始化**
  - 文件：`pyproject.toml`、`.gitignore`、`start.bat`、`start.sh`、目录骨架（`src/x_operator/` 各包 `__init__.py`、`config/settings.toml`、`data/.gitkeep`）
  - 做什么：uv 工程；依赖 `nicegui`、`sqlalchemy>=2.0`、`apscheduler<4`、`tweepy>=4.14,<5`、`twifork>=2.3.5`、`openai`、`loguru`、`croniter`；dev 依赖 `pytest`、`pytest-mock`。start 脚本按 v1.1 §10.2 行为编写（bat 优先调试）
  - 依赖：无
  - 验收点：Windows 双击 start.bat 从全新环境完成 uv sync 并进入 main（可先只打印占位）；`.gitignore` 覆盖 v1.1 §10.3

- [ ] **P0-2 配置加载**
  - 文件：`src/x_operator/config.py`
  - 做什么：`Settings` 类：读 settings.toml + secrets.toml（缺失容错，字段级默认）；`get_setting(key)/set_setting(key,value)` 走 app_settings 表；首启动把 `[defaults]` 种子写入表（v1.1 §10.1）
  - 依赖：P0-1、P0-3
  - 验收点：secrets.toml 不存在时程序可启动（LLM/X 功能提示未配置而非崩溃）；改表值后 get_setting 生效

- [ ] **P0-3 数据库层**
  - 文件：`src/x_operator/db/models.py`、`db/session.py`、`db/migrations.py`
  - 做什么：v1.1 §4 DDL 全量迁移脚本（版本 1）+ SQLAlchemy ORM 1:1 映射 + StrEnum；session.py 设 WAL/foreign_keys/busy_timeout；migrations.py 按 schema_version 顺序执行
  - 依赖：P0-1
  - 验收点：空目录启动自动建 12 张表；`PRAGMA journal_mode` 返回 wal；插入违反 CHECK（如主号+unofficial）报错

- [ ] **P0-4 适配器基础与异常**
  - 文件：`src/x_operator/adapters/base.py`、`adapters/factory.py`
  - 做什么：v1.1 §3.1 异常体系 + §3.2 数据类与 XClient ABC；factory.get_client（含缓存、主号禁 unofficial 校验、CredentialMissing）
  - 依赖：P0-2
  - 验收点：单元测试：factory 对 is_primary+unofficial 抛 ValueError；缺凭据抛 CredentialMissing

- [ ] **P0-5 官方适配器（get_me 部分）**
  - 文件：`src/x_operator/adapters/official.py`
  - 做什么：OfficialXClient 骨架 + `_map_error` 全表（v1.1 §5.3）+ `get_me`/`get_user_by_handle`（其余方法 NotImplementedError 占位）
  - 依赖：P0-4
  - 验收点：mock tweepy 异常逐类映射正确（见 test-cases.md UT-AD-*）；真实凭据下 get_me 返回本账号 handle

- [ ] **P0-6 LLM 客户端**
  - 文件：`src/x_operator/llm/client.py`
  - 做什么：v1.1 §7.7 LLMClient：chat_json（JSON 容错解析、纠错重试 1 次、response_format 降级、action_log 记账）+ ping
  - 依赖：P0-2、P0-3
  - 验收点：mock 响应测试合法/非法/重试后合法三分支；对 apimax 网关真实 ping 成功

- [ ] **P0-7 UI 壳与设置页**
  - 文件：`src/x_operator/ui/layout.py`、`ui/settings_page.py`、`ui/dashboard.py`（占位卡片）、`main.py`
  - 做什么：v1.1 §8.0 外壳 + §8.7 设置页（本 Phase 先做：账号 CRUD+测试连接、LLM tab+测试连接；其余 tab 出框架）；main.py 串起 db→scheduler(空)→ui.run
  - 依赖：P0-3、P0-5、P0-6
  - 验收点：**Phase 0 门槛**——双击启动浏览器自动打开中文界面；设置页 X「测试连接」显示账号名；LLM「测试连接」通过；单实例锁生效（二次启动提示已运行，v1.1 §9.10 J3）

---

## Phase 1 定时发推 + 队列（~3 天）

- [ ] **P1-1 官方适配器补全（发推/媒体）**
  - 文件：`adapters/official.py`
  - 做什么：post / reply / upload_media（v1.1 §5.1，图片先行；视频留 Phase 4）；确认 v1.1 media 上传通路（§5.1 联调确认点），不通则实现 v2 备选
  - 依赖：P0-5
  - 验收点：脚本手动调用真发 1 条带图推文并删除（test-cases.md IT-X-01）

- [ ] **P1-2 字符计数**
  - 文件：`core/textcount.py`
  - 做什么：weighted_len（URL 23、CJK×2、总 280），URL 识别用简化正则（http/https）
  - 依赖：无
  - 验收点：纯英文/纯日文/混合含 URL 用例通过（UT-TC-*）

- [ ] **P1-3 素材库（文本+图片）**
  - 文件：`ui/materials.py`、`ui/components.py`（素材卡片）
  - 做什么：v1.1 §8.3 中除 AI 撰写/翻译外全部：CRUD、筛选、图片上传校验（≤5MB、格式）、alt 文本、状态流转、字数提示
  - 依赖：P0-3、P0-7、P1-2
  - 验收点：新建→启用→归档全流程可用；超 5MB 图片被拒且提示中文

- [ ] **P1-4 计划计算与定时计划页**
  - 文件：`core/schedule_calc.py`、`ui/schedule.py`
  - 做什么：compute_next_run 四类型（v1.1 §7.6）+ §8.6 页面（含表达式校验、auto_approve 风险说明）
  - 依赖：P1-3
  - 验收点：once/daily/weekly/cron 计算用例通过（UT-SC-*）；非法表达式保存被拒

- [ ] **P1-5 ComplianceGuard**
  - 文件：`core/compliance.py`
  - 做什么：v1.1 §7.1 全部（软硬违规区分、跨日活跃时段、账号时区自然日计数、养号期减半）
  - 依赖：P0-3
  - 验收点：UT-CG-01~10 全过

- [ ] **P1-6 审核队列页**
  - 文件：`ui/queue.py`、`ui/components.py`（queue_card）
  - 做什么：v1.1 §8.2（本 Phase 先覆盖 post 类条目：批准/编辑/跳过/字数校验/乐观锁；reply 卡片结构一并做好，数据 Phase 2 才有）
  - 依赖：P1-3、P1-2
  - 验收点：pending 条目 5s 内出现在页面；批准后状态变化即时反映；并发批准冲突提示（E2E 手测）

- [ ] **P1-7 调度器 + scheduled_check + Dispatcher**
  - 文件：`core/scheduler.py`、`core/dispatcher.py`
  - 做什么：v1.1 §7.5 / §7.6：4 job 注册（monitor/search 本 Phase 空实现）；scheduled_check 生成队列条目；dispatcher 全流程（乐观锁、软硬违规分流、重试退避、interactions+action_log、next_allowed_at）
  - 依赖：P1-1、P1-4、P1-5、P1-6
  - 验收点：auto_approve 计划到点自动真发；人审计划走队列批准后发出；发送后 interactions/action_log 有记录

- [ ] **P1-8 启动补扫 + 备份**
  - 文件：`core/startup.py`
  - 做什么：v1.1 §7.6 run_startup_recovery（宽限期补发/missed 顺延/过期扫描/sending 回置/日预算重算占位/backup API 备份 14 份滚动/app.lock）
  - 依赖：P1-7
  - 验收点：**Phase 1 门槛**——真发带图推文成功；关机重开：宽限内计划补发、超宽限标 missed 且 UI 提示；日上限打满后 approved 条目滞留次日发出（UT-SU-*、E2E-02）

---

## Phase 2 监控 + 匹配回复（~4 天）

- [ ] **P2-1 官方适配器补全（读侧）**
  - 文件：`adapters/official.py`
  - 做什么：get_user_tweets / search_recent（v1.1 §5.1：字段参数、clamp、since_id 语义、handle 回填）
  - 依赖：P1-1
  - 验收点：真实拉取某公开账号新推返回 TweetData 字段完整；reads_consumed=返回条数

- [ ] **P2-2 Budget（基础记账）**
  - 文件：`core/budget.py`
  - 做什么：v1.1 §7.2（本 Phase：today_reads_left / can_spend / recompute_daily 双模式；usage 聚合 Phase 3 完善）
  - 依赖：P0-3
  - 验收点：UT-BG-01~05 过；startup 接入 recompute_daily
  - 依赖补充：P1-8（startup 挂接）

- [ ] **P2-3 Prompt 模板（匹配相关 4 场景）**
  - 文件：`llm/prompts.py`、`llm/translator.py`
  - 做什么：v1.1 §6.2/6.4/6.5/6.6 模板与构造函数（match / translate / tweet_zh / detect_lang，含 few-shot）；Translator 三方法
  - 依赖：P0-6
  - 验收点：IT-LLM-02/04（真实端点契约测试）通过

- [ ] **P2-4 匹配引擎**
  - 文件：`core/matcher.py`
  - 做什么：v1.1 §7.3 全部（pick_candidates 排序规则、fallback_translate、D1~D7 边界处理、入队含 text_zh/expires_at）
  - 依赖：P2-3、P1-5
  - 验收点：UT-ME-01~08 过（mock LLM 四分支 + 边界）

- [ ] **P2-5 监控轮询 job**
  - 文件：`core/monitor.py`
  - 做什么：v1.1 §7.4 MonitorJob（预检全分支、游标推进、首次只设游标、B1~B8 边界、budget 前置检查、逐推主隔离）
  - 依赖：P2-1、P2-2、P2-4
  - 验收点：UT-MN-01~06 过；真实监控一个测试号，新推 10 分钟内进队列

- [ ] **P2-6 监控推主页 + 黑名单**
  - 文件：`ui/watched.py`、`ui/settings_page.py`（黑名单 tab）、`ui/queue.py`（跳过并拉黑按钮接通）
  - 做什么：v1.1 §8.4 + FR-9（添加时 get_user_by_handle 解析；队列一键拉黑）
  - 依赖：P2-5、P1-6
  - 验收点：添加不存在的 handle 有中文错误；拉黑者后续推文被预检过滤（存档 filtered）

- [ ] **P2-7 队列 reply 卡片完整化**
  - 文件：`ui/queue.py`、`ui/components.py`
  - 做什么：目标推文区（原文+中文翻译+作者+链接）、AI 理由/置信度、自动翻译黄标、含链接计费灰标、过期倒计时、手动模式（复制+打开链接+标记已发）
  - 依赖：P2-4、P1-6
  - 验收点：**Phase 2 门槛**——监控推主发新推后 10min 内进队列且匹配贴切、卡片信息完整；no_match 正确存档；同一推文二次触发被拦（E2E-03/合规演练 CP-01）

---

## Phase 3 语义搜索（~3 天）

- [ ] **P3-1 相关性打分 Prompt**
  - 文件：`llm/prompts.py`
  - 做什么：v1.1 §6.1（relevance，批量一次调用，few-shot）
  - 依赖：P2-3
  - 验收点：IT-LLM-01 通过（含遗漏 id 容错 C3）

- [ ] **P3-2 搜索 job（两级漏斗）**
  - 文件：`core/search.py`
  - 做什么：v1.1 §7.4 SearchJob（本地过滤在 LLM 前、批量打分、阈值入队、游标、dry_run、C1~C7 边界）；scheduler 挂接每日 N 次均分时刻
  - 依赖：P3-1、P2-4、P2-2
  - 验收点：UT-SR-01~06 过

- [ ] **P3-3 搜索规则页 + 试运行**
  - 文件：`ui/search_rules.py`
  - 做什么：v1.1 §8.5（规则 CRUD、语法说明链接、试运行弹窗表格）
  - 依赖：P3-2
  - 验收点：试运行显示逐条 score/reason；非法查询串错误可读

- [ ] **P3-4 仪表盘 + 用量 + 熔断闭环**
  - 文件：`ui/dashboard.py`、`core/budget.py`（usage 完善）、`ui/layout.py`（熔断徽标）
  - 做什么：v1.1 §8.1 全部卡片与图表；熔断后 monitor/search 跳过 + UI 提示 + 手动调预算即时恢复（I1/I5）
  - 依赖：P3-2、P2-2
  - 验收点：**Phase 3 门槛**——示例规则跑一天误报 <50%（人工抽检 dry_run 结果）；读消耗在预算内且与 action_log 一致；熔断触发与恢复可演示（E2E-04）

---

## Phase 4 AI 撰写 + 翻译 + 视频 + 小号（~5 天）

- [ ] **P4-1 撰写 Prompt + Writer + 撰写页**
  - 文件：`llm/prompts.py`（§6.3）、`llm/writer.py`、`ui/materials.py`（AI 撰写弹窗）
  - 做什么：一稿多语生成、over_length 标红、入库草稿、多语言归同翻译组
  - 依赖：P2-3
  - 验收点：IT-LLM-03 过；生成→编辑→入库→组内并排展示

- [ ] **P4-2 一键翻译 + 翻译组视图**
  - 文件：`ui/materials.py`、`ui/components.py`
  - 做什么：翻译菜单→Translator→新建并列草稿（G1~G6 边界）；翻译组并排 UI（FR-2.2）
  - 依赖：P2-3、P1-3
  - 验收点：ja↔zh 互译入库；@/#/URL 未被翻译（抽检）

- [ ] **P4-3 视频上传**
  - 文件：`adapters/official.py`、`ui/materials.py`
  - 做什么：mp4≤512MB 校验、chunked upload、processing 等待、MediaError 分支
  - 依赖：P1-1
  - 验收点：真发 1 条带视频推文成功（IT-X-02）

- [ ] **P4-4 非官方适配器**
  - 文件：`adapters/unofficial.py`、`scripts/login_helper.py`
  - 做什么：v1.1 §3.4 + §5.2 全部方法、async 封装 `_run`、异常映射核对（twifork 2.3.5 实际异常名逐一确认并回填 v1.1 §5.3 表）、cookies 引导脚本
  - 依赖：P0-4
  - 验收点：UT-AD-U* 过；login_helper 生成 cookies 后 get_me 成功；cookies 删除后调用→账号 auth_error+UI 提示（H3）

- [ ] **P4-5 小号保守限速 + 养号期**
  - 文件：`core/compliance.py`（effective_limits 已有，接默认值）、`ui/settings_page.py`
  - 做什么：unofficial 新账号默认值按 NFR-1（回复 5/日、间隔 10~30min）；养号期减半展示；低于风险阈值时黄色警示文案
  - 依赖：P4-4、P1-5
  - 验收点：新建 unofficial 账号默认值正确；两账号日计数互相独立（UT-CG-09）

- [ ] **P4-6 1 主 1 小实测**
  - 文件：无新增（联调）
  - 做什么：主号 official + 小号 unofficial 同时启用，跑监控→匹配→审核→双账号发送
  - 依赖：P4-4、P4-5、P2-7
  - 验收点：**Phase 4 门槛**——AI 生成/翻译素材入库并组内并排；视频发布成功；小号限速更保守、两账号计数独立；同一目标推文只被一个账号回复（E2E-05）

---

## Phase 5 打磨（持续）

- [ ] **P5-1 统计完善**：`ui/dashboard.py` 7 天图按账号分色、no_match 率口径核对；验收：与 SQL 手查一致
- [ ] **P5-2 异常通知**：`ui/layout.py` 全局红色徽标 + 仪表盘异常卡片联动（auth_error/连续失败/熔断）；验收：三类异常演练均在 UI 显眼可见（MT-03）
- [ ] **P5-3 备份与恢复文档化**：`docs/user-guide.md` 恢复步骤章节 + J6 提示语接通；验收：手动损坏 db 后按文档恢复成功
- [ ] **P5-4 中文使用说明（截图版）**：`docs/user-guide.md` 全流程（依赖全部 Phase 完成）；验收：**Phase 5 门槛**——用户不看代码按文档独立操作全流程（MT-01~08 全过）
- [ ] **P5-5 交付前手动测试**：执行 test-cases.md「手动测试清单」全部条目并留记录

---

## 任务依赖图（关键路径）

```
P0-1 → P0-3 → P0-2 → P0-4 → P0-5 → P0-7(壳)
                          ↘ P0-6 ↗
P0-7 → P1-3 → P1-4 → P1-7 → P1-8 ─→ P2-2
P0-5 → P1-1 → P2-1 → P2-5 → P2-7 ═ Phase2 门槛
P0-6 → P2-3 → P2-4 ↗
P2-4 → P3-1 → P3-2 → P3-3/P3-4 ═ Phase3 门槛
P2-3 → P4-1/P4-2；P1-1 → P4-3；P0-4 → P4-4 → P4-5 → P4-6 ═ Phase4 门槛
```
