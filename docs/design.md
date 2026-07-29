# x-operator — X（推特）运营工具 完整规格书（Spec v1.0）

---

## 1. 项目概述

### 1.1 背景
用户目前手动做 X（推特）运营获客：定期发内容推文、盯目标推主的新推文并回复、搜索特定话题（如「抱怨多模型 API 成本」）的推文并在下面回复推广内容。全手动效率低、时机容易错过、多账号无法兼顾。

### 1.2 目标
构建一个**通用型** X 运营半自动化工具：AI 负责找目标、匹配素材、写文案，人负责最终把关（审核队列）。第一个使用场景是 apimax.io 的日本市场获客，但工具本身不绑定任何业务，可用于任何产品/语言/市场的 X 运营。

### 1.3 范围（In Scope）
- 多账号管理（官方 API + 非官方登录态两种接入）
- 定时发布预设推文（文本+图片/视频）
- 监控指定推主新推文 → 匹配预设素材 → 回复
- 语义搜索目标推文 → 匹配预设素材 → 回复
- LLM 最佳素材匹配 + 文案变体生成
- AI 辅助素材撰写、多语言、自动翻译
- 人工审核队列、合规护栏、成本/用量仪表盘

### 1.4 非目标（Out of Scope，本期不做）
- DM 私信自动化（X 对 DM 自动化管控最严，留待后续评估）
- 点赞/转推/关注自动化（仅日志预留 action 字段）
- 粉丝增长分析、竞品分析等数据产品功能
- 多用户/团队协作（单用户工具）
- 服务器常驻部署（架构不排斥，但本期按本机手动启动设计）

### 1.5 已确定的关键决策
| 决策点 | 结论 |
|---|---|
| X 接入 | 混合：主号官方 API，小号非官方库（登录态） |
| 发送模式 | 人工审核队列为默认；自己账号的定时推文可设全自动 |
| 规模 | 先 1 账号跑通，数据结构/调度预留多账号 |
| 部署 | 用户自己电脑手动启动（OS 待确认，启动脚本 Win/Mac 都做） |
| LLM | 任何 OpenAI 兼容端点可配置（用户可填自家 apimax 网关） |
| 界面语言 | 中文；内容语言不限（多语言+自动翻译） |

---

## 2. 用户与使用场景

**用户画像**：非技术背景的运营者，一人操作，懂中文，运营内容可能是日语/英语等外语（需要翻译辅助审核）。

**核心日常流程（预期每天 10~20 分钟）**：
1. 双击启动 → 浏览器自动打开中文管理界面
2. 仪表盘看昨日/今日概况（发送数、队列积压、额度消耗、异常）
3. 进审核队列：逐条看「目标推文（原文+中文翻译）+ AI 选的回复 + AI 理由」，批准/编辑后批准/跳过
4. 偶尔：补充素材（手写或 AI 生成/翻译）、调整监控推主和搜索规则

---

## 3. 功能需求（FR）

### FR-1 账号管理
- FR-1.1 添加/停用 X 账号；每账号属性：handle、接入类型（official/unofficial）、每日发推上限、每日回复上限、动作随机间隔范围（min/max 秒）、活跃时段（起止时间+时区）、备注
- FR-1.2 official 账号：填 API Key/Secret + Access Token/Secret（引导页说明去哪申请）；「测试连接」按钮验证并显示账号名
- FR-1.3 unofficial 账号：引导登录获取 cookies 并持久化；**校验规则：标记为「主号」的账号禁止使用 unofficial 接入**（界面+代码双重限制）
- FR-1.4 账号状态机：active / paused / auth_error（凭据失效自动进入，UI 醒目提示，绝不自动重试登录）
- FR-1.5 非官方账号的默认限速档硬性更保守（见 NFR-1）

### FR-2 素材库
- FR-2.1 素材属性：类型（post 定时推文用 / reply 回复用）、正文、语言（ja/en/zh/…）、场景标签（逗号分隔，供粗筛和 LLM 参考）、媒体（0~4 图或 1 视频）、状态（draft/active/archived）、来源（human/ai）、使用次数、最后使用时间
- FR-2.2 翻译组：同一素材的不同语言版本归入同一 translation_group，界面并排展示
- FR-2.3 一键翻译：任一素材翻译成指定语言生成新版本（可编辑后启用）；翻译为本地化风格而非直译，保留 @/#/URL/占位符
- FR-2.4 AI 撰写：输入主题/要点/风格 → 生成 N 条候选（可选一稿多语）→ 编辑 → 入库
- FR-2.5 媒体管理：本地上传图片（jpg/png/webp≤5MB）视频（mp4≤512MB，X 限制以 API 实际为准）；可填 alt 文本

### FR-3 定时发布
- FR-3.1 计划属性：账号、素材、计划类型（once/daily/weekly/cron 表达式）、下次执行时间、是否自动批准（auto_approve）
- FR-3.2 auto_approve=true：到点直接进队列并置 approved，由分发器按限速发出；false：进队列 pending 等人审
- FR-3.3 错过处理：程序未开导致错过的，启动时在宽限期（默认 2h）内的补发，超过的标 missed 并顺延，UI 提示

### FR-4 监控推主
- FR-4.1 监控清单：handle、是否含回复推文、启用开关、备注；每账号可关联不同监控清单（预留，MVP 全局一份）
- FR-4.2 轮询：程序运行期间按间隔（默认 45~60 分钟，可配）增量拉取新推文（since_id 游标）；首次添加不回溯旧推
- FR-4.3 对每条新推：预检（黑名单/RT/已回复过该推文/该推主冷却期内/超过时效 48h）→ 通过则进入匹配引擎
- FR-4.4 关机补扫：游标机制天然支持；超时效的旧推只入库存档不进队列

### FR-5 语义搜索
- FR-5.1 搜索规则属性：名称、X 关键词查询串（粗筛，如 `lang:ja -is:retweet ("API料金" OR "APIコスト")`）、自然语言语义条件（精筛，如「在抱怨多模型 API 调用成本」）、语言、每次最大拉取数（默认 15）、LLM 相关性分阈值（默认 7/10）、启用开关
- FR-5.2 两级漏斗：X recent search 粗筛（花读额度）→ LLM 批量打分精筛（0-10 分+一句话理由）→ 达阈值进匹配引擎
- FR-5.3 频率默认每天 2~3 次；受读预算熔断控制（见 FR-8.3）

### FR-6 匹配引擎与审核队列
- FR-6.1 匹配：目标推文语言判定（API lang 字段+LLM 兜底）→ 同语言 active 回复素材按标签粗筛（无标签交集则全量，≤10 条送 LLM）→ LLM 输出 {选中素材 id 或 null, 置信度, 理由, 变体文案}
- FR-6.2 同语言无素材但翻译组有其他语言版本时，现场翻译作候选，队列条目标注「自动翻译，请重点检查」
- FR-6.3 LLM 判 null 或置信度低于阈值 → 标记 no_match 存档，不进队列
- FR-6.4 队列条目内容：账号、动作类型、目标推文（原文+中文翻译+作者+链接）、选中素材、最终文案（可编辑）、媒体预览、LLM 理由与置信度、过期时间（回复类默认 48h）
- FR-6.5 用户操作：批准 / 编辑后批准 / 跳过；批量跳过；过期自动 expired
- FR-6.6 状态机：pending → approved → sending → sent / failed（重试≤2 次指数退避后）；pending → skipped / expired
- FR-6.7 手动模式兜底：任一队列条目可「复制文案+打开推文链接」由用户手发，点「标记已发」后同样写入去重账本

### FR-7 发送与合规护栏（ComplianceGuard）
- FR-7.1 分发器每 60s 扫一次 approved 条目，按账号串行发送
- FR-7.2 发送前最终校验（缺一不可）：在活跃时段内 / 距该账号上次动作 ≥ 本次随机间隔 / 当日该类动作未达上限 / 目标推文从未被任何自有账号回复过 / 目标推主不在冷却期（默认 7 天）/ 不在黑名单
- FR-7.3 文案变体：LLM 生成的 adapted_text 保证同一素材多次发送时措辞不完全相同
- FR-7.4 全部发送写入 interactions（去重账本）与 action_log

### FR-8 仪表盘与成本
- FR-8.1 概况：今日/本周发送数（按账号/类型）、队列积压、no_match 率、失败与异常
- FR-8.2 用量：X API 读消耗（本日/本月，进度条对预算）、LLM 调用次数与估算费用
- FR-8.3 读预算熔断：日读预算=月度额度/天数（可手动调），用尽自动暂停监控与搜索 job（发送不受影响），UI 提示

### FR-9 黑名单
- 手动添加/移除；被拉黑者的推文在预检阶段直接丢弃；可从队列条目一键「跳过并拉黑作者」

---

## 4. 非功能需求（NFR）

- **NFR-1 平台合规（防封号）**：非官方账号默认每日回复上限 5、间隔 10~30 分钟随机、仅活跃时段、养号期（前 14 天）上限减半；官方账号默认每日回复 15、发推 10、间隔 3~10 分钟。所有默认值可配但界面显示风险提示。
- **NFR-2 安全**：凭据存本地 secrets.toml 与 cookies 文件，权限 600，gitignore；UI 不回显完整密钥；数据库不存明文凭据。
- **NFR-3 可靠性**：SQLite WAL；每日启动时自动备份 db 到 data/backup/（保留 14 份）；任何 job 崩溃不影响其他 job（隔离 try/except + 日志）。
- **NFR-4 性能**：单机单用户，无性能压力；LLM 调用批量化（搜索精筛一批一次调用）控制延迟与成本。
- **NFR-5 成本**：X API 读额度是最紧资源，两级漏斗+预算熔断保护；LLM 侧用可配置的轻量模型做打分/翻译、强模型做匹配/撰写（模型名分场景可配）。
- **NFR-6 可用性**：双击启动；全中文界面；所有 AI 决策展示理由；错误信息人话化。
- **NFR-7 可扩展**：适配器接口化（未来加平台）；账号维度贯穿所有表（多账号无迁移成本）；LLM 端点可换。

---

## 5. 系统架构

单 Python 进程 = NiceGUI（UI）+ APScheduler（4 类 job）+ SQLite。

```
┌────────────────── 浏览器（中文 UI，NiceGUI）──────────────────┐
│ 仪表盘 │ 审核队列 │ 素材库 │ 监控推主 │ 搜索规则 │ 定时计划 │ 设置 │
└──────────────────────────┬──────────────────────────────────┘
                           │ SQLite (WAL)
┌──────────────────────────┴──────────────────────────────────┐
│ Scheduler(APScheduler)                                      │
│  ├ job:定时发推检查 ──────────────→ review_queue             │
│  ├ job:监控推主轮询 ─→ target_tweets ─→ MatchEngine ─→ queue │
│  ├ job:语义搜索轮询 ─→ target_tweets ─→ MatchEngine ─→ queue │
│  └ job:发送分发器 ─→ approved条目 ─→ ComplianceGuard ─→ 适配器│
│                                                             │
│ MatchEngine(粗筛+LLM匹配+变体)   LLMClient(OpenAI兼容端点)    │
│ Translator(素材翻译/推文中译)     Writer(AI撰写)              │
│ ComplianceGuard(发送唯一闸口)     Budget(读额度熔断)          │
│                                                             │
│ XClient 适配器接口: post/reply/search_recent/get_user_tweets │
│                    /upload_media/get_me + 统一异常           │
│  ├ OfficialXClient(tweepy)    ├ UnofficialXClient(twifork)  │
└─────────────────────────────────────────────────────────────┘
```

**铁律**：一切要发出的内容必经 review_queue 表 + ComplianceGuard，只有一条发送路径。

---

## 6. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+，uv | 生态齐全；uv 让 start 脚本一条命令自动装环境 |
| UI | NiceGUI | 纯 Python、WebSocket 实时刷新队列、启动自动开浏览器；不用 Streamlit（整页重跑做队列交互别扭） |
| DB | SQLite(WAL)+SQLAlchemy 2.0 | 零运维单文件；ORM 保留未来迁移可能 |
| 调度 | APScheduler | 进程内，匹配手动启动模式 |
| 官方 API | tweepy(v2) | 成熟，含媒体 chunked upload |
| 非官方 | twifork（twikit 维护 fork；备选 twscrape） | 安装时核实可用性 |
| LLM | openai SDK，base_url 可配 | OpenAI 兼容，一行切端点/模型 |

⚠️ **X API 计费情报（需在开发者后台核实）**：$200/月 Basic 档 2026-02 起停止新订阅；新用户按量付费约 读 $0.005/条、发 $0.015/条、**含链接推文 $0.20/条**（带外链的推广文成本单独核算）。预估本工具用量 $50-80/月。若持有 legacy Basic：读 10,000/月是最紧约束（≈330/天）。

---

## 7. 数据模型（核心表）

```
accounts        id, handle, display_name, access_type('official'|'unofficial'),
                is_primary BOOL(主号禁unofficial), credential_ref,
                daily_post_limit, daily_reply_limit,
                min_interval_sec, max_interval_sec,
                active_hours_start, active_hours_end, timezone,
                status('active'|'paused'|'auth_error'), created_at

materials       id, kind('post'|'reply'), text, lang,
                translation_group_id, scenario_tags, media_ids,
                created_by('human'|'ai'), status('draft'|'active'|'archived'),
                usage_count, last_used_at, created_at

media_assets    id, file_path, media_type('image'|'video'), alt_text, created_at

scheduled_posts id, account_id, material_id,
                schedule_type('once'|'daily'|'weekly'|'cron'), schedule_expr,
                next_run_at, auto_approve BOOL,
                status('active'|'paused'|'done'), last_run_at

watched_users   id, handle, x_user_id, last_seen_tweet_id,
                include_replies BOOL, enabled BOOL, note

search_rules    id, name, keyword_query, semantic_criteria, lang,
                newest_id_cursor, max_results_per_run, min_llm_score,
                enabled, last_run_at

target_tweets   id, tweet_id UNIQUE, author_id, author_handle, text, lang,
                text_zh(中文翻译缓存), tweet_created_at,
                source('monitor'|'search'), source_rule_id,
                llm_relevance_score, llm_relevance_reason,
                process_status('new'|'queued'|'no_match'|'filtered'|'expired'),
                fetched_at

review_queue    id, account_id, action_type('post'|'reply'),
                target_tweet_id, material_id, final_text, final_media_ids,
                llm_reason, llm_confidence, is_auto_translated BOOL,
                status('pending'|'approved'|'sending'|'sent'|'failed'
                       |'skipped'|'expired'),
                auto_approve BOOL, sent_tweet_id, error_msg,
                created_at, decided_at, sent_at, expires_at

interactions    id, account_id, action('reply'|'post'), tweet_id, author_id,
                sent_at
                索引:(tweet_id) 同推文永不重复；(author_id,sent_at) 冷却查询

blacklist       id, x_user_id, handle, reason, created_at
action_log      id, account_id, api_kind, endpoint, reads_consumed,
                success BOOL, error, created_at
app_settings    key, value   (冷却天数N、宽限期、日读预算、LLM分场景模型名等)
```

---

## 8. 核心流程规格

### 8.1 监控轮询（伪代码）
```
job monitor_poll(每45~60min, 仅程序运行时):
  if Budget.today_reads_left() < 保留水位: return
  for u in watched_users(enabled):
    tweets = adapter.get_user_tweets(u.x_user_id, since_id=u.last_seen_tweet_id,
                                     max_results=5)
    for t in tweets(旧→新):
      if 预检失败(RT/黑名单/interactions中已有/作者冷却/超48h): 存档跳过
      else: insert target_tweets(source='monitor'); MatchEngine.run(t)
    u.last_seen_tweet_id = newest_id
    Budget.record(reads=len(tweets))
  # 首次添加推主: 只取最新1条设游标不处理(不回溯轰炸)
```

### 8.2 语义搜索（伪代码）
```
job semantic_search(每天2~3次):
  for r in search_rules(enabled):
    resp = adapter.search_recent(r.keyword_query, since_id=r.newest_id_cursor,
             start_time=max(r.last_run_at, now-7d), max_results=r.max_results_per_run)
    cands = 本地过滤(resp)          # 去重/黑名单/冷却/自有账号/太老
    scores = LLM.judge_relevance(r.semantic_criteria, cands)   # 批量一次调用
    for t in cands where score >= r.min_llm_score:
      insert target_tweets(source='search'); MatchEngine.run(t)
    r.newest_id_cursor = resp.newest_id
```

### 8.3 匹配引擎（伪代码）
```
MatchEngine.run(tweet):
  lang = tweet.lang or LLM判定
  cands = materials(kind='reply', status='active', lang=lang, 标签粗筛, 限10条)
  if empty and 翻译组有他语言版本: cands = 现场翻译(标记is_auto_translated)
  if empty: mark no_match; return
  r = LLM.match(tweet, cands)      # → {choice|null, confidence, reason, adapted_text}
  if r.choice is null or r.confidence < 阈值: mark no_match; return
  tweet.text_zh = Translator.to_zh(tweet.text)     # 供审核展示
  insert review_queue(pending, final_text=r.adapted_text, expires_at=now+48h)
  UI实时提醒
```

### 8.4 发送分发器（伪代码）
```
job dispatcher(每60s):
  for acc in accounts(active):
    if not 活跃时段 or now < acc.next_allowed_at: continue
    item = review_queue.oldest(approved, acc)
    if not item: continue
    if not ComplianceGuard.check(acc, item): item→skipped(记原因); continue
    media = [adapter.upload_media(m) for m in item.final_media_ids]
    try:  resp = adapter.reply/post(...); item→sent; 写interactions+action_log
    except Retryable: 重试≤2(指数退避) else item→failed(UI醒目提示)
    acc.next_allowed_at = now + random(acc.min_interval, acc.max_interval)
```

### 8.5 启动补扫
```
on_startup:
  1 定时推文: next_run_at<now → 宽限期(2h)内补队列, 否则标missed并顺延+UI提示
  2 监控: since_id天然补扫; >48h旧推只入库
  3 搜索: start_time=max(上次运行, now-7d)
  4 队列: 过expires_at的pending → expired
  5 Budget: 今日读预算 = 月剩余额度/月剩余天数
  6 备份: 复制app.db → data/backup/(保留14份)
```

---

## 9. LLM Prompt 规格（llm/prompts.py 集中管理）

| 场景 | 模型档 | 输入 | 输出(JSON) | 关键要求 |
|---|---|---|---|---|
| 相关性打分 | 轻量 | 语义条件+候选推文批量 | [{tweet_id, score0-10, reason}] | 批量一次调用；宁低勿高 |
| 最佳匹配 | 强 | 目标推文+作者信息+候选素材(含标签) | {choice\|null, confidence, reason, adapted_text} | 不贴切必须null；adapted_text用目标语言、呼应原文具体内容、措辞每次自然变化、保留核心信息与链接 |
| 素材撰写 | 强 | 主题/要点/风格/目标语言(可多) | [{lang, text}] | 符合该语言圈推特习惯；≤280字符(CJK按X计数规则) |
| 翻译 | 轻量 | 原文+目标语言 | {text} | 本地化非直译；@/#/URL/占位符不译 |
| 推文中译(审核辅助) | 轻量 | 目标推文 | {text_zh} | 直译+俚语注释 |

系统提示共通约束：「宁可跳过也不发像广告骚扰的回复」；输出严格 JSON。

---

## 10. UI 页面规格

| 页面 | 核心要素 |
|---|---|
| 仪表盘 | 今日发送数/队列积压/异常卡片；读额度与 LLM 费用进度条；最近 7 天发送图 |
| 审核队列 | 卡片列表：目标推文原文+中文翻译+作者+跳转链接、AI 选的素材与理由/置信度、可编辑文案框、媒体预览、［批准/跳过/跳过并拉黑/手动模式］按钮；顶部筛选（账号/类型/状态）；新条目实时出现 |
| 素材库 | 列表（筛选：类型/语言/标签/状态）；翻译组并排视图；新建/编辑；［AI 撰写］［一键翻译］入口 |
| 监控推主 | 清单 CRUD、启用开关、最近命中数 |
| 搜索规则 | 规则 CRUD、关键词查询串+语义条件+阈值、试运行按钮（只拉取打分不进队列，用于调试规则） |
| 定时计划 | 计划 CRUD、下次执行时间、auto_approve 开关、missed 提示 |
| 设置 | 账号管理（含测试连接/凭据引导）、LLM 端点与分场景模型名、合规参数（上限/间隔/冷却/活跃时段）、读预算、黑名单管理 |

---

## 11. 错误处理与日志

- 统一异常体系：RateLimited（退避至窗口重置）、AuthExpired（账号置 auth_error+UI 提示）、Duplicate（标 failed 记原因）、NetworkError（重试）
- 所有 X API/LLM 调用写 action_log（含 reads_consumed、耗时、错误）
- 应用日志 data/logs/app.log（滚动 7 天）；UI「异常」页显示最近错误的人话说明
- LLM 输出 JSON 解析失败：重试 1 次（附格式错误提示），仍失败按 no_match 处理并记日志

---

## 12. 测试计划

### 12.1 单元测试（pytest，随开发写）
- ComplianceGuard：日上限/间隔/冷却/黑名单/重复推文各拦截分支
- 匹配引擎：LLM 返回 null/低置信度/正常/JSON 坏格式（mock LLM）
- 补扫逻辑：宽限期内外、游标增量、7 天窗口边界
- Budget：预算计算与熔断触发
- 适配器：统一异常映射（mock tweepy/twifork）

### 12.2 集成测试
- LLM 全链路（真实端点）：打分→匹配→变体→翻译，校验 JSON 契约
- SQLite 状态机迁移：队列全状态流转、interactions 唯一约束
- X API 沙盒测试：用测试小号真实发一条带图推文并删除

### 12.3 端到端验收（每 Phase 门槛，见 §13）
- 最终 E2E：配 1 监控推主+1 搜索规则，从抓取→打分→匹配→翻译→人审→发送→去重完整跑通；action_log 用量与 X 开发者后台数字比对一致
- 合规演练：同一推文二次触发被拦、冷却期推主被拦、日上限达到后 approved 条目滞留次日发出

### 12.4 手动测试清单（交付用户前）
- Windows/Mac 双击启动全新环境安装成功
- 断网启动、LLM 端点错误、X 凭据失效三种故障的 UI 提示可理解
- 中文界面无乱码，日语/表情符号推文显示与发送正常

---

## 13. 分阶段实施计划

| Phase | 内容 | 验收标准 |
|---|---|---|
| 0 骨架(~2天) | 工程+建表+NiceGUI中文壳+start脚本+设置页 | 双击启动→中文界面；X API与LLM端点「测试连接」通过 |
| 1 定时发推+队列(~3天) | 素材CRUD(图片/语言)、定时计划、队列操作、官方适配器、护栏基础、补扫 | 真发带图推文；关机重开补扫正确；日上限拦截生效 |
| 2 监控+匹配回复(~4天) | 监控轮询、匹配引擎(语言过滤)、去重账本、黑名单、队列中文翻译 | 新推10min内进队列且匹配贴切；no_match正确；重复触发被拦 |
| 3 语义搜索(~3天) | 搜索规则、两级漏斗、读预算仪表盘+熔断、试运行 | 示例规则跑一天误报<50%；消耗在预算内 |
| 4 AI撰写+翻译+视频+小号(~5天) | 撰写页(一稿多语)、一键翻译+翻译组、视频上传、twifork+保守限速+手动模式、1主1小实测 | AI生成/翻译入库并组内并排；视频发布成功；小号限速更保守、两账号计数独立 |
| 5 打磨(持续) | 统计、自动备份、异常通知、中文使用说明(截图版) | 用户不看代码能独立操作全流程 |

---

## 14. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 非官方接入封号 | 高 | 只用可弃小号；主号代码级禁止 unofficial；保守限速+养号期；家庭 IP 不套机房代理；手动模式兜底 |
| 自动回复被判 spam | 高 | 人工审核默认开；变体文案；冷却与上限；「宁跳过不硬发」写进 prompt |
| X API 计费/额度变动 | 中 | 适配器与 Budget 解耦计费模式；action_log 全量记账可对账 |
| twifork 失效 | 中 | 适配器可整体换（twscrape 备选）；手动模式保底 |
| LLM 匹配错素材 | 中 | 人审把关；置信度阈值；理由展示辅助判断 |
| 用户电脑不常开导致漏推 | 低 | 补扫+missed 提示；未来可迁服务器（架构已兼容） |

---

## 15. 运维与交付

- **备份**：启动时自动备份 db（14 份滚动）；素材媒体在 data/media/ 随目录整体备份即可
- **升级**：git pull + uv sync（start 脚本内置）；db 迁移用简单版本号+迁移脚本
- **凭据轮换**：设置页可直接更新；旧 cookies 文件自动归档
- **交付物**：可运行工程 + 中文使用说明（截图版）+ 本 Spec 入库 docs/design.md

---

## 16. 项目位置与目录结构

**新建独立项目 `/home/user2/projects/x-operator/`**（与 zenith-quant 并列）。本 Spec 定稿后存入项目 `docs/design.md`。

```
x-operator/
├── start.bat / start.sh
├── pyproject.toml
├── docs/design.md
├── config/{settings.toml, secrets.toml(gitignore)}
├── data/{app.db, media/, cookies/, logs/, backup/}
├── src/x_operator/
│   ├── main.py
│   ├── db/{models.py, session.py, migrations.py}
│   ├── adapters/{base.py, official.py, unofficial.py}
│   ├── core/{scheduler.py, monitor.py, search.py, matcher.py,
│   │        dispatcher.py, compliance.py, budget.py}
│   ├── llm/{client.py, prompts.py, writer.py, translator.py}
│   └── ui/{dashboard.py, queue.py, materials.py, watched.py,
│            search_rules.py, schedule.py, settings.py}
└── tests/
```

---

## 17. 立即执行事项（用户已确认）

1. 在 `/home/user2/projects/x-operator/` 新建 git 仓库（git init + .gitignore，排除 secrets.toml/cookies/data）
2. 本 Spec 存入 `docs/design.md` 并完成首次 commit
3. 用户随后重新发起 Ultraplan 云端交接（需在该仓库目录下运行）

## 18. 实施前确认事项（2026-07-29 用户已答复）

1. 用户电脑 OS：**Windows** —— start.bat 优先调试
2. X API 计费档位：用户确认没有问题，凭据到 Phase 1 联调时提供
3. LLM 端点：**用 apimax 网关**（OpenAI 兼容），base_url/key 到 Phase 0 设置页联调时提供
4. 首批素材/监控推主/搜索条件：**等开发完成后再定**

## 19. 补充说明

- 发现另一会话产出的旧版窄范围草稿 `~/apimax-ja-optimization/twitter-autopost-spec.md`
  （v0.1：仅自动发推+线索队列，不做自动回复）。本 Spec 功能超集覆盖之，
  建仓库时将其归档至 `docs/archive/` 作参考，避免双版本混淆。
