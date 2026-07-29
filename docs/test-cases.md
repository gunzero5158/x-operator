# x-operator 测试用例表（展开 Spec v1.0 §12 测试计划）

> 依据：[design.md](design.md) §12 + [design-v1.1.md](design-v1.1.md)。
> 编号规则：`UT-`单元（pytest，随开发写）/ `IT-`集成 / `E2E-`端到端验收 / `CP-`合规演练 / `MT-`交付前手动清单。
> 单元测试通用前置：内存 SQLite（同 DDL）+ 冻结时钟（`freezegun` 或注入 `now`）；mock 对象指 `pytest-mock`。

---

## 1. 单元测试

### 1.1 ComplianceGuard（`tests/test_compliance.py`）

通用前置：账号 A（official，post 上限 10 / reply 上限 15，间隔 180~600s，活跃 09:00~22:00 Asia/Tokyo，active）；一条 approved reply 条目 Q 指向目标推文 T（作者 U）。

| 用例 ID | 前置条件（叠加通用） | 步骤 | 预期结果 |
|---|---|---|---|
| UT-CG-01 | now=东京 08:59 | check(A,Q) | ok=False, code=OUTSIDE_ACTIVE_HOURS, hard=False |
| UT-CG-02 | 活跃时段 22:00~02:00（跨日）；now=东京 23:30 / 01:30 / 03:00 | 分别 check | 前两者通过该项；03:00 返回 OUTSIDE_ACTIVE_HOURS |
| UT-CG-03 | A.next_allowed_at = now+60s | check | ok=False, code=INTERVAL_NOT_ELAPSED, hard=False |
| UT-CG-04 | interactions 已有 A 当日 reply 15 条 | check | ok=False, code=DAILY_LIMIT_REACHED, hard=False |
| UT-CG-05 | 东京时间昨日 23:50 有 15 条 reply，now=今日 10:00 | check | 通过日上限项（按账号时区自然日清零） |
| UT-CG-06 | interactions 已存在 tweet_id=T（任意账号、action=reply） | check | ok=False, code=ALREADY_REPLIED, hard=True |
| UT-CG-07 | interactions 中 U 的最近互动在 3 天前；cooldown_days=7 | check | ok=False, code=AUTHOR_IN_COOLDOWN, hard=True；互动改为 8 天前则通过 |
| UT-CG-08 | U 在 blacklist | check | ok=False, code=BLACKLISTED, hard=True |
| UT-CG-09 | 账号 B（unofficial）当日 reply 已 3 条；A 当日 0 条 | 分别 check A、B 的条目 | 计数互相独立：A 通过，B 按 B 的上限判断 |
| UT-CG-10 | B 为 unofficial，created_at=5 天前，nurture_days=14，daily_reply_limit=5 | effective_limits(B) | reply 上限=2（减半向下取整）；created_at=20 天前时=5 |
| UT-CG-11 | Q.expires_at = now-1min | check | ok=False, code=TARGET_EXPIRED, hard=True |
| UT-CG-12 | 全部条件满足 | check | ok=True, code=None |

### 1.2 匹配引擎（`tests/test_matcher.py`，mock LLM 与 Translator）

通用前置：目标推文 T(lang='ja')；active 日语 reply 素材 M1(tags='cost')、M2(tags='news')；阈值 0.7。

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| UT-ME-01 | mock LLM 返回 choice=M1, confidence=0.85, adapted_text 合法 | run(T) | 状态 queued；review_queue 新增 pending 条目：material_id=M1、final_text=adapted_text、expires_at=now+48h；T.process_status='queued'、text_zh 已填 |
| UT-ME-02 | mock 返回 choice=null | run(T) | no_match；T.process_status='no_match'；队列无新条目 |
| UT-ME-03 | mock 返回 confidence=0.5（低于阈值） | run(T) | 同 UT-ME-02，reason 含置信度信息 |
| UT-ME-04 | mock 首次返回坏 JSON、纠错重试仍坏（抛 LLMFormatError） | run(T) | 按 no_match 处理，不抛出，action_log 有失败记录 |
| UT-ME-05 | 无 ja 素材；M3(lang='en') 与 M1 同翻译组；mock Translator 返回译文 | run(T) | 候选为现场翻译文案；入队条目 is_auto_translated=1、material_id=M3 |
| UT-ME-06 | 无 ja 素材且翻译组无他语言版本 | run(T) | no_match（reason='无可用素材'类） |
| UT-ME-07 | 12 条 ja 素材：3 条标签有交集（usage_count 3/1/2），9 条无交集 | pick_candidates('ja', T 标签) | 返回 10 条；有交集者排前且按 usage_count 升序（1,2,3 号序）；截断到 10 |
| UT-ME-08 | mock 返回 choice=999（不在候选中） | run(T) | 走一次纠错重试；仍非法则 no_match（v1.1 §9.4 D2） |
| UT-ME-09 | 素材含 URL，mock adapted_text 无 URL 且不超长 | run(T) | final_text 结尾被自动补「 → URL」（v1.1 §9.4 D4） |
| UT-ME-10 | T.lang=None；mock detect_lang 返回 'und' | run(T) | no_match（语言无法判定，D1） |

### 1.3 启动补扫（`tests/test_startup.py`）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| UT-SU-01 | daily 计划 next_run_at=now-1h（宽限 2h 内），auto_approve=0 | run_startup_recovery | 生成 pending 队列条目 1 条；next_run_at 推进到下一天；报告含补发消息 |
| UT-SU-02 | 计划 next_run_at=now-3h（超宽限） | 同上 | 不生成条目；计划 status='missed'；next_run_at 顺延；报告含 missed 提示 |
| UT-SU-03 | 计划 next_run_at=now-3天（daily，积压多次） | 同上 | 只处理一次（missed+顺延到未来最近时刻），绝不逐日补多条 |
| UT-SU-04 | pending 条目 expires_at=now-1min；另一条 expires_at=now+1h | 同上 | 前者→expired；后者不变 |
| UT-SU-05 | 残留 status='sending' 条目 | 同上 | 回置 approved，retry_count 不变 |
| UT-SU-06 | backup 目录已有 14 份备份 | 同上 | 新备份生成后总数仍 ≤14，删除的是最旧一份 |
| UT-SU-07 | once 计划时间在未来 | 同上 | 不动（status 仍 active，不误标 missed） |

### 1.4 Budget（`tests/test_budget.py`）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| UT-BG-01 | quota 模式：月额度 10000，本月已读 4000，当月剩 15 天 | recompute_daily | daily_read_budget=400 |
| UT-BG-02 | payg 模式：月预算 $60，本月已读 2000，剩 20 天 | recompute_daily | (60/0.005-2000)/20=500 |
| UT-BG-03 | 日预算 100，今日 action_log 读 85，保留水位 20 | can_spend(10) | False（85+10 后剩 5 < 20）；can_spend(0) 亦按剩余 15<20 → False（熔断） |
| UT-BG-04 | 日预算 100，今日已读 40 | can_spend(25) | True；today_reads_left()=60 |
| UT-BG-05 | 熔断中（UT-BG-03 状态）；用户把日预算改为 200 | can_spend(10) | True（即时恢复，v1.1 §9.9 I5） |
| UT-BG-06 | x_unofficial 的 action_log 读记录若干 | today_reads_left | 不计入（只统计 api_kind='x_official'） |

### 1.5 适配器异常映射（`tests/test_adapters.py`，mock tweepy / twikit）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| UT-AD-01 | mock tweepy 抛 TooManyRequests（headers 带 x-rate-limit-reset） | official.search_recent | 抛 RateLimited 且 reset_at 解析正确 |
| UT-AD-02 | mock 抛 Unauthorized | official.get_me | 抛 AuthExpired |
| UT-AD-03 | mock 抛 Forbidden（消息含 "duplicate content"） | official.post | 抛 DuplicateContent |
| UT-AD-04 | mock 抛 Forbidden（其他消息） | official.reply | 抛 PermissionDenied |
| UT-AD-05 | mock 抛 NotFound | official.reply | 抛 TargetNotFound |
| UT-AD-06 | mock 抛 requests.ConnectionError / TwitterServerError | official.post | 抛 NetworkError |
| UT-AD-07 | mock v2 返回含 includes.users | official.get_user_tweets | TweetData.author_handle 正确回填；tweets 旧→新排序；reads_consumed=条数 |
| UT-AD-U1 | cookies 文件不存在 | unofficial.get_me | 抛 AuthExpired（不尝试登录） |
| UT-AD-U2 | mock twikit 抛 AccountLocked / Unauthorized | unofficial.post | 抛 AuthExpired |
| UT-AD-U3 | mock 返回含 RT 与旧 id 的推文列表，since_id 传入 | unofficial.get_user_tweets | RT 与 id≤since_id 者被过滤；reads_consumed=0 |
| UT-AD-U4 | 查询串 `lang:ja -is:retweet ("A" OR "B")` | unofficial.search_recent（mock 传输层，断言实际 query） | 发出的 query 为 `lang:ja -filter:retweets ("A" OR "B")` |
| UT-AD-08 | is_primary=1 且 access_type='unofficial' 的账号 | factory.get_client | 抛 ValueError（代码级禁止） |

### 1.6 其他单元（textcount / schedule_calc / monitor / search / LLM 解析）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| UT-TC-01 | — | weighted_len("hello")／280 个英文字符 | 5／280（临界通过） |
| UT-TC-02 | — | weighted_len("こんにちは")（5 字） | 10（CJK×2） |
| UT-TC-03 | — | weighted_len("見て https://example.com/very/long/path?q=1 です") | URL 记 23：4+1+23+1+4=33 |
| UT-SC-01 | tz=Asia/Tokyo | compute_next_run('daily','21:00', after=东京 20:00/22:00) | 当天 21:00 UTC 值／次日 21:00 |
| UT-SC-02 | — | compute_next_run('once','2026-08-01T21:00', after=之后时刻) | None |
| UT-SC-03 | — | compute_next_run('weekly','mon,thu 21:00', after=周二) | 周四 21:00（账号时区） |
| UT-SC-04 | — | compute_next_run('cron','0 21 * * 1-5', after=周五 22:00) | 下周一 21:00；非法表达式抛 ValueError（中文消息） |
| UT-MN-01 | 新推为 RT | precheck | 返回 'retweet'；target_tweets 存档 filtered |
| UT-MN-02 | 新推作者在黑名单／已回复过／作者冷却中／发布于 50h 前 | precheck ×4 | 分别返回对应原因码，均存档 filtered 不进匹配 |
| UT-MN-03 | watched_user 无游标（首次） | poll_user（mock 适配器返回 3 条） | 只设 last_seen_tweet_id=最新 id，0 条进匹配 |
| UT-MN-04 | mock 适配器对推主 2 抛 TargetNotFound（共 3 个推主） | run_once | 推主 2 enabled=0；推主 1/3 正常处理；stats.errors=1 |
| UT-MN-05 | budget.can_spend=False | run_once | 立即返回，无任何拉取 |
| UT-MN-06 | mock 返回 5 条新推，其中 2 条过预检 | poll_user | 2 条进 MatchEngine；游标=5 条中最大 id；Budget.record(5) |
| UT-SR-01 | mock 打分返回 [8,6,9]，阈值 7 | run_rule | 2 条进 MatchEngine；3 条都写 target_tweets（6 分者 process_status='filtered'，score/reason 已存） |
| UT-SR-02 | 粗筛返回 0 条 | run_rule | 不调 LLM；游标不变；last_run_at 更新 |
| UT-SR-03 | 打分结果缺 1 个 tweet_id | run_rule | 缺失者按 0 分处理并记 warning（C3） |
| UT-SR-04 | dry_run=True | run_rule | 返回 ScoredCandidate 列表；target_tweets 无写入、游标不变、MatchEngine 未调用；action_log 有读记账 |
| UT-SR-05 | 候选含黑名单作者与已回复推文 | run_rule | 在 LLM 调用前被本地过滤（mock LLM 断言收到的批量不含它们） |
| UT-SR-06 | mock 适配器抛 RateLimited | run_rule / run_once | 本轮终止不抛出；action_log 记 rate_limited；下轮可再运行 |
| UT-LLM-01 | mock 响应 "好的```json\n{...}\n```" | chat_json | 容错解析成功（提取大括号段） |
| UT-LLM-02 | mock 首次坏 JSON、二次合法 | chat_json | 返回合法结果；messages 含纠错追加消息；action_log 记 2 次 |
| UT-LLM-03 | mock 两次都坏 | chat_json | 抛 LLMFormatError |
| UT-LLM-04 | mock 返回缺 required_keys 的 JSON | chat_json | 视同格式错误参与重试 |

---

## 2. 集成测试

### 2.1 LLM 全链路（真实 apimax 端点，`tests/integration/test_llm_live.py`，标记 `@pytest.mark.live`）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| IT-LLM-01 | 端点已配置 | 用 v1.1 §6.1 few-shot 的 4 条日语推文真实调用 relevance | 返回合法 JSON、覆盖全部 4 个 tweet_id；1001 得分 ≥7、1004 得分 ≤2 |
| IT-LLM-02 | 同上 | 用 §6.2 示例 1/示例 2 输入真实调用 match | 示例 1 choice=3、confidence≥0.6、adapted_text 为日语且含 https://apimax.io、weighted_len≤280；示例 2 choice=null、adapted_text=null |
| IT-LLM-03 | 同上 | §6.3 示例输入调用 write（ja×2） | 返回 2 条 ja；均含链接；weighted_len≤280；两条文案角度可辨差异（人工抽检） |
| IT-LLM-04 | 同上 | §6.4 zh→ja 翻译 + §6.5 日语中译 | translate：URL 原样保留、输出为日语；tweet_zh：输出中文、含「草」的括号注释 |

### 2.2 SQLite 状态机与约束（`tests/integration/test_db_flow.py`）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| IT-DB-01 | 一条 pending 条目 | 依次 pending→approved→sending→sent（走 Dispatcher/UI 同款 UPDATE） | 每步乐观锁 UPDATE 影响行数=1；decided_at/sent_at 落值 |
| IT-DB-02 | 一条 pending 条目 | 两个会话并发执行「pending→approved」 | 仅一个成功（影响行数 1/0） |
| IT-DB-03 | interactions 已有 reply tweet_id=T | 再插一条 action='reply', tweet_id=T | IntegrityError（部分唯一索引生效）；action='post' 同 tweet_id 可插入 |
| IT-DB-04 | — | 插入 review_queue(action_type='reply', target_tweet_id=NULL) | CHECK 约束拒绝 |
| IT-DB-05 | — | 插入 accounts(is_primary=1, access_type='unofficial') | CHECK 约束拒绝 |
| IT-DB-06 | sent 条目 | 尝试 sent→approved | 乐观锁 UPDATE ... WHERE status IN 合法前态 → 影响 0 行（终态不可逆） |

### 2.3 X API 沙盒（测试小号，人工触发，`@pytest.mark.live_x`）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| IT-X-01 | 测试小号 official 凭据 | upload_media(图片)→post→（人工确认）→删除该推文 | 推文带图发布成功；action_log 有记录；get_me 显示正确账号 |
| IT-X-02 | 同上（Phase 4） | upload_media(短视频 mp4)→post→删除 | 视频处理等待后发布成功 |
| IT-X-03 | 同上 | 60 秒内连续 post 两条相同文本 | 第二条抛 DuplicateContent，条目 failed 且 error_msg 中文 |

---

## 3. 端到端验收（E2E，对应 Phase 门槛）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| E2E-01（P0） | 全新 Windows 环境 | 双击 start.bat；设置页填 X/LLM 凭据并各点「测试连接」 | 自动装环境并打开中文界面；两个测试连接均显示成功（含账号名） |
| E2E-02（P1） | 1 账号、1 图片素材、1 once 计划（auto_approve=0） | 到点→队列出现→编辑文案→批准→自动发出；再建到点计划后关程序，宽限内重开；再把日上限设为 0 批准一条 | 真发带图推文；重开后补发正确；上限 0 时条目滞留 approved（软违规），次日（或调回上限后）发出 |
| E2E-03（P2） | 1 监控推主（测试号）、2 条日语 reply 素材、1 条英语同组素材 | 测试号发一条日语新推→等待轮询→审核队列检查卡片→批准发出→测试号再发语义无关推文 | 10 分钟内进队列；卡片含原文/中文翻译/理由/置信度/链接；发出后 interactions 记录；无关推文 no_match 存档 |
| E2E-04（P3） | 1 搜索规则（日语示例：`lang:ja -is:retweet ("API料金" OR "APIコスト")` + 语义条件） | 试运行看打分→正式启用跑一天→检查仪表盘 | 试运行表格可读；一天内误报率 <50%（人工抽检达标条目）；读消耗≤日预算；仪表盘读数与 action_log 合计一致 |
| E2E-05（P4） | 主号 official + 小号 unofficial 各 1，同一监控源 | 触发两条不同目标推文，分别由两账号批准发送；再让两账号的条目指向同一目标推文 | 两账号各自发出且计数独立；同一目标推文第二个账号被 ALREADY_REPLIED 拦截置 skipped |
| E2E-06（最终） | 1 监控推主+1 搜索规则+素材若干，完整配置 | 抓取→打分→匹配→翻译→人审→发送→去重全链路跑通后，取 action_log 读写合计与 X 开发者后台用量页对比 | 全链路无人工干预代码；用量数字与后台一致（允许后台统计延迟误差） |

## 4. 合规演练（CP，v1.0 §12.3）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| CP-01 | 已对推文 T 回复过（interactions 有记录） | 让监控/搜索再次抓到 T（清 target_tweets 缓存后模拟） | 预检 'already_replied' 过滤，不进队列；若手工造 approved 条目指向 T，发送前被 guard 拦截置 skipped |
| CP-02 | 推主 U 于 3 天前被回复过（冷却 7 天） | U 发新推被抓到 | 预检 'author_cooldown' 过滤存档；8 天后同场景可正常进队列 |
| CP-03 | 日回复上限 2，队列有 3 条 approved | 观察一天发送 | 只发 2 条；第 3 条保持 approved 无 skip；次日自动发出 |
| CP-04 | 同素材 M 连续匹配 3 条不同目标推文 | 批准并发出 3 条 | 3 条 final_text 措辞互不相同（FR-7.3，人工比对）；无 DuplicateContent 报错 |

## 5. 交付前手动测试清单（MT，v1.0 §12.4）

| 用例 ID | 前置条件 | 步骤 | 预期结果 |
|---|---|---|---|
| MT-01 | 全新 Windows 机器（无 Python/uv） | 双击 start.bat | 引导安装 uv 或自动完成环境装配，最终打开界面 |
| MT-02 | 全新 Mac | ./start.sh | 同 MT-01 |
| MT-03 | 三种故障各造一次：断网启动 / LLM base_url 填错 / X 凭据改坏 | 启动或触发相应操作 | 三种场景 UI 提示均为中文人话（非 traceback）；X 凭据坏→账号 auth_error 红色提示且不自动重试登录 |
| MT-04 | — | 浏览全部 7 个页面 | 中文界面无乱码、无英文残留（API 名/代码标识符除外） |
| MT-05 | 队列中有日语+表情符号（含 𝕏 类特殊字符）目标推文 | 查看卡片并批准发送 | 显示与发送均正常，无编码错误 |
| MT-06 | 熔断触发（把日预算调到低于已消耗） | 看仪表盘与监控 job | 熔断徽标显示；监控/搜索停止；发送不受影响；调回预算后自动恢复 |
| MT-07 | 队列条目用「手动模式」 | 复制文案→浏览器手发→点「标记已发」 | 剪贴板内容正确；interactions 记录写入；该推文后续被去重拦截 |
| MT-08 | 程序运行中直接杀进程后重启 | 观察启动报告 | 备份生成；sending 残留回置；无重复发送；启动摘要中文可读 |
