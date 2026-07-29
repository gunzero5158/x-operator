# x-operator — 实施级规格书（Spec v1.1）

> 本文档在 [Spec v1.0](design.md) 基础上细化到「可直接照着写代码」的粒度。
> **v1.0 的所有已定决策（§1.5：混合接入 / 审核队列 / Windows 本机跑 / apimax 网关 LLM / 多语言+翻译）在本文档中全部维持不变。**
> v1.0 原文保持不动，便于对照；两文档冲突时以本文档为准（本文档只做细化，不做推翻）。
>
> 配套文档：[docs/tasks.md](tasks.md)（文件级开发任务清单）、[docs/test-cases.md](test-cases.md)（测试用例表）。

---

## 0. v1.0 存疑点核实结论（2026-07-29 核实）

### 0.1 X API 计费（v1.0 §6 标注「需核实」）

**结论：v1.0 中的计费情报基本准确，予以确认。** 要点：

| 项目 | 核实结论 |
|---|---|
| 分档订阅 | 2026-02-06 起 X 把 **pay-per-use（按量付费）设为新开发者默认**，$200/月 Basic 与 $5,000/月 Pro **停止新订阅**；已订阅者（legacy）可继续保留 |
| Free 档 | 对新开发者已停止；存量 free 用户获一次性 $10 代金券并迁移到按量付费 |
| 按量单价 | 读推文 **$0.005/条**；发推 **$0.015/条**；**含链接的推文 $0.20/条**（链接加价在 2026-04 的价格调整中引入，不同信源对引入月份表述略有出入，单价本身一致）；用户资料查询 $0.01/次 |
| 读上限 | 按量付费封顶 **200 万读/月**，超过只能上 Enterprise（约 $42,000+/月）——对本工具无实际影响 |
| Legacy Basic 额度 | 读约 10,000~15,000/月、写约 50,000/月、仅 7 天窗口搜索——与 v1.0「读 10,000/月是最紧约束（≈330/天）」一致 |

信源（第三方汇总，2026-07-29 检索；**开发前仍应在 X 开发者后台以账号实际显示为准**）：
- [Postproxy: X (Twitter) API Pricing in 2026](https://postproxy.dev/blog/x-api-pricing-2026/)
- [twitterapi.io: X API Cost Breakdown 2026](https://twitterapi.io/blog/x-api-cost-breakdown-2026)
- [SocialCrawl: X API in 2026: Credit Pricing](https://www.socialcrawl.dev/blog/x-twitter-api-2026)

**对设计的影响**：
1. Budget 模块必须同时支持两种记账模式：`quota`（legacy Basic：月读额度池）与 `payg`（按量：月预算金额，读按 $0.005/条折算）。`app_settings.billing_mode` 二选一，默认 `payg`。
2. **含链接推文 $0.20/条**：ComplianceGuard 在发送前检测 `final_text` 是否含 URL，含链接时在队列条目上显示「本条含链接，按量计费约 $0.20」提示（仅提示，不拦截）；action_log 记 `has_link` 便于对账。
3. 用户已在 v1.0 §18 确认计费档位没有问题，故不因计费改变任何功能范围。

### 0.2 twifork 可用性（v1.0 §6 标注「安装时核实」）

**结论：可用，维持 v1.0 选型（twifork 主选、twscrape 备选）。**

| 项目 | 核实结论 |
|---|---|
| PyPI 包名 | `twifork`，最新版 **2.3.5**（2026-07-05 发布），Python ≥3.8 |
| 性质 | d60/twikit 的**在维护 fork**（维护者 PawiX25），修复了 2026 年 X 前端改版导致上游 twikit 失效的问题（`ondemand.s.js` 解析、`x-client-transaction-id` 计算等） |
| 兼容性 | **drop-in 替换：安装 `twifork` 后仍以 `twikit` 名 import**（`from twikit import Client`），上游文档/示例可直接参考 |
| 能力覆盖 | 登录（含 cookies 持久化）、发推、回复、搜索、取用户推文、上传媒体——覆盖本工具全部所需 |
| 接口形态 | **全 async**（`await client.create_tweet(...)`），适配器需做同步封装（见 §3.4） |

信源（2026-07-29 检索）：
- [PyPI: twifork 2.3.5](https://pypi.org/project/twifork/)
- [GitHub: PawiX25/twifork](https://github.com/PawiX25/twifork)

**对设计的影响**：
1. `pyproject.toml` 依赖写 `twifork>=2.3.5`，代码中 `import twikit`（注释说明实际装的是 twifork）。
2. 因 twifork 全 async 而工程主体（APScheduler 线程 job）为同步，`UnofficialXClient` 内部维护独立事件循环线程做同步封装（§3.4）。
3. 风险对策不变（v1.0 §14）：twifork 失效时整体换 twscrape 适配器，手动模式保底。

---

## 1. 术语与全局约定

| 约定 | 内容 |
|---|---|
| 时间 | 数据库一律存 **UTC ISO-8601 文本**（`YYYY-MM-DDTHH:MM:SSZ`）；账号活跃时段按 `accounts.timezone`（IANA 名，如 `Asia/Tokyo`）换算；UI 展示用本机时区 |
| ID | 内部主键 `INTEGER PRIMARY KEY AUTOINCREMENT`；X 侧 tweet_id/user_id 一律 **TEXT**（snowflake 超 JS 安全整数） |
| 枚举 | 数据库用 TEXT + CHECK 约束；Python 侧用 `enum.StrEnum` 一一对应（定义在 `db/models.py`） |
| JSON 字段 | `media_ids` 等多值字段存 JSON 数组文本（如 `["1","2"]`），SQLAlchemy 用 `JSON` 类型 |
| 字符计数 | X 计数规则：URL 按 23 计、CJK 每字按 2 计、总上限 280；实现统一放 `core/textcount.py: weighted_len(text) -> int` |
| 日志 | `loguru`，`data/logs/app.log` 滚动 7 天；所有对外调用（X/LLM）额外落 `action_log` 表 |
| 配置 | 非敏感配置：`config/settings.toml` + `app_settings` 表（表优先，UI 可改）；敏感凭据：`config/secrets.toml` 与 `data/cookies/*.json`（chmod 600，gitignore） |

`app_settings` 键清单（含默认值，Phase 0 建表时写入）：

| key | 默认 | 说明 |
|---|---|---|
| `cooldown_days` | `7` | 同一推主冷却天数（FR-7.2） |
| `grace_period_hours` | `2` | 定时推文补发宽限期（FR-3.3） |
| `reply_ttl_hours` | `48` | 回复类队列条目过期时长（FR-6.4） |
| `tweet_max_age_hours` | `48` | 目标推文超时效阈值（FR-4.3） |
| `billing_mode` | `payg` | `payg` 或 `quota`（§0.1） |
| `monthly_read_quota` | `10000` | quota 模式：月读额度（条） |
| `monthly_budget_usd` | `60` | payg 模式：月预算（美元） |
| `daily_read_budget` | 启动时算 | 日读预算（条），可手动覆盖（FR-8.3） |
| `budget_reserve_reads` | `20` | 保留水位：低于此值监控/搜索 job 不启动 |
| `monitor_interval_minutes` | `50` | 监控轮询间隔（45~60 可配） |
| `search_runs_per_day` | `2` | 语义搜索每日次数（2~3） |
| `match_confidence_threshold` | `0.7` | 匹配置信度阈值（FR-6.3） |
| `nurture_days` | `14` | unofficial 账号养号期天数（NFR-1） |
| `llm_model_light` | `gpt-4o-mini`（示例） | 轻量档模型名（打分/翻译/中译） |
| `llm_model_strong` | `gpt-4o`（示例） | 强档模型名（匹配/撰写） |
| `llm_price_per_1k_in` / `llm_price_per_1k_out` | `0` | LLM 费用估算单价（可留 0 只计次数） |
| `schema_version` | `1` | 数据库迁移版本号 |

`config/secrets.toml` 结构（示例）：

```toml
[llm]
base_url = "https://api.apimax.io/v1"   # apimax 网关（OpenAI 兼容）
api_key  = "sk-..."

[x.main_official]        # 表名 = accounts.credential_ref
api_key = ""
api_secret = ""
access_token = ""
access_token_secret = ""

# unofficial 账号不进 secrets.toml：cookies 存 data/cookies/<credential_ref>.json
```

---

## 2. 模块与依赖总览

```
src/x_operator/
├── main.py                 # 入口：init db → startup recovery → scheduler → NiceGUI
├── config.py               # Settings 加载（settings.toml + secrets.toml + app_settings）
├── db/
│   ├── models.py           # SQLAlchemy 2.0 ORM + StrEnum（§4 DDL 的 1:1 映射）
│   ├── session.py          # engine/Session 工厂（WAL、busy_timeout）
│   └── migrations.py       # 版本号迁移（schema_version → 顺序执行 SQL）
├── adapters/
│   ├── base.py             # XClient ABC + 数据类 + 统一异常（§3.1~3.2）
│   ├── official.py         # OfficialXClient（tweepy）（§3.3）
│   ├── unofficial.py       # UnofficialXClient（twifork，async 封装）（§3.4）
│   └── factory.py          # get_client(account) → XClient（含实例缓存）
├── core/
│   ├── scheduler.py        # APScheduler 装配（4 类 job）
│   ├── startup.py          # 启动补扫（v1.0 §8.5）
│   ├── monitor.py          # 监控轮询
│   ├── search.py           # 语义搜索（两级漏斗）
│   ├── matcher.py          # 匹配引擎
│   ├── dispatcher.py       # 发送分发器
│   ├── compliance.py       # ComplianceGuard（发送唯一闸口）
│   ├── budget.py           # 读预算/熔断
│   ├── schedule_calc.py    # once/daily/weekly/cron → next_run_at 计算
│   └── textcount.py        # X 字符计数
├── llm/
│   ├── client.py           # LLMClient（OpenAI 兼容、JSON 契约、重试）
│   ├── prompts.py          # 5 场景 prompt 模板与构造函数（§6）
│   ├── writer.py           # AI 撰写
│   └── translator.py       # 素材翻译 + 推文中译
└── ui/
    ├── layout.py           # 公共外壳（header/导航/通知）
    ├── dashboard.py / queue.py / materials.py / watched.py
    ├── search_rules.py / schedule.py / settings_page.py
    └── components.py       # 复用组件（队列卡片、翻译组视图、确认弹窗）
```

依赖方向（只允许向下）：`ui → core → (adapters, llm, db)`；`adapters/llm` 不依赖 `core`；谁都不依赖 `ui`。

---

## 3. adapters 模块接口定义

### 3.1 统一异常（`adapters/base.py`）

```python
class XClientError(Exception):
    """所有适配器异常基类。message 必须是中文人话（NFR-6）。"""
    def __init__(self, message: str, *, raw: Exception | None = None): ...

class RateLimited(XClientError):
    """429/限流。reset_at 为限流窗口重置时间（UTC）；未知则 None（调用方按 15min 退避）。"""
    reset_at: datetime | None

class AuthExpired(XClientError):
    """401/凭据失效/cookies 过期/账号被锁。捕获方必须将账号置 auth_error，绝不自动重试登录（FR-1.4）。"""

class DuplicateContent(XClientError):
    """X 判定重复内容（官方 403 duplicate；twifork DuplicateTweet）。条目置 failed，不重试。"""

class PermissionDenied(XClientError):
    """403 非鉴权类：对方锁推/禁止回复/账号被限写。不重试，条目置 failed 记原因。"""

class TargetNotFound(XClientError):
    """目标推文/用户不存在（已删除/改名）。不重试。"""

class MediaError(XClientError):
    """媒体上传失败（格式/大小/处理超时）。不重试，条目置 failed。"""

class NetworkError(XClientError):
    """网络/超时/5xx。可重试（指数退避 ≤2 次，FR-6.6）。"""
```

异常语义 = 重试策略的唯一依据：**仅 `NetworkError` 与 `RateLimited` 可重试**，其余一律终态。

### 3.2 数据类与 XClient 抽象基类

```python
@dataclass(frozen=True)
class TweetData:
    tweet_id: str
    author_id: str
    author_handle: str          # 无法取得时为 ""（由调用方回填）
    text: str
    lang: str | None            # API lang 字段；unofficial 可能为 None
    created_at: datetime        # UTC
    is_retweet: bool
    in_reply_to_tweet_id: str | None

@dataclass(frozen=True)
class UserData:
    user_id: str
    handle: str
    display_name: str

@dataclass(frozen=True)
class FetchResult:
    tweets: list[TweetData]     # 按 created_at 升序（旧→新）
    newest_id: str | None       # 本批最大 tweet_id；空批为 None（调用方不得覆盖游标）
    reads_consumed: int         # 本次消耗读数（= len(tweets)，官方按返回条数计）

@dataclass(frozen=True)
class PostResult:
    tweet_id: str

class XClient(ABC):
    """所有方法均为同步阻塞；线程安全要求：单实例串行调用（分发器按账号串行，天然满足）。
    所有方法均可能抛出 §3.1 异常；下面只标注方法特有的语义。"""

    @abstractmethod
    def get_me(self) -> UserData:
        """验证凭据并返回本账号信息（FR-1.2 测试连接）。凭据错误抛 AuthExpired。"""

    @abstractmethod
    def get_user_by_handle(self, handle: str) -> UserData:
        """按 @handle 查用户（添加监控推主时解析 x_user_id）。不存在抛 TargetNotFound。"""

    @abstractmethod
    def post(self, text: str, media_ids: list[str] | None = None) -> PostResult:
        """发独立推文。media_ids 为本适配器 upload_media 返回值（不跨适配器复用）。"""

    @abstractmethod
    def reply(self, text: str, in_reply_to_tweet_id: str,
              media_ids: list[str] | None = None) -> PostResult:
        """回复推文。目标已删除抛 TargetNotFound；作者限制回复抛 PermissionDenied。"""

    @abstractmethod
    def get_user_tweets(self, user_id: str, since_id: str | None = None,
                        max_results: int = 5,
                        include_replies: bool = False) -> FetchResult:
        """拉取用户新推文（FR-4.2）。since_id=None 时只取最新 1 条（首次设游标用）。
        返回中已排除转推（RT 在适配器层过滤，is_retweet 仅供防御性复查）。"""

    @abstractmethod
    def search_recent(self, query: str, since_id: str | None = None,
                      start_time: datetime | None = None,
                      max_results: int = 15) -> FetchResult:
        """近 7 天搜索（FR-5.2 粗筛）。query 语法差异由适配器吸收（§5 映射表）。"""

    @abstractmethod
    def upload_media(self, file_path: str, media_type: str,
                     alt_text: str | None = None) -> str:
        """上传媒体返回 media_id。media_type: 'image'|'video'。视频须等处理完成后返回。"""
```

### 3.3 OfficialXClient（tweepy，`adapters/official.py`）

```python
class OfficialXClient(XClient):
    def __init__(self, api_key: str, api_secret: str,
                 access_token: str, access_token_secret: str):
        """内部构造 tweepy.Client(v2, wait_on_rate_limit=False) 与 tweepy.API(v1.1, OAuth1)
        （后者仅用于媒体上传）。wait_on_rate_limit 必须为 False——限流由本工具统一处理，
        不允许 tweepy 内部 sleep 阻塞 job 线程。"""
```

- 私有方法 `_map_error(e: tweepy.TweepyException) -> XClientError` 集中做异常映射（映射表见 §5.3），所有公开方法用 `try/except` 包住 tweepy 调用后 `raise self._map_error(e) from e`。
- `RateLimited.reset_at` 从响应头 `x-rate-limit-reset`（epoch 秒）解析。

### 3.4 UnofficialXClient（twifork，`adapters/unofficial.py`）

```python
class UnofficialXClient(XClient):
    def __init__(self, cookies_path: str):
        """cookies_path: data/cookies/<credential_ref>.json。
        构造时不做网络请求；首次调用任一方法时惰性 client.load_cookies() 并校验。
        cookies 文件不存在/损坏/失效 → AuthExpired（绝不尝试账密登录，FR-1.4）。"""

    def _run(self, coro: Coroutine) -> Any:
        """同步封装：类持有一个后台线程 + 专用事件循环（懒启动、进程内单例复用），
        用 asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=120)。
        不使用 asyncio.run()（twikit Client 内部 aiohttp session 跨调用复用，需常驻 loop）。"""
```

- cookies 获取流程（Phase 4，FR-1.3 引导页配套）：提供 `scripts/login_helper.py`，命令行交互式跑 twifork `Client.login(auth_info_1, auth_info_2, password)` 一次后 `client.save_cookies(path)`；主程序永不调用 login。
- 异常映射 `_map_error(e: twikit.errors.TwitterException) -> XClientError` 见 §5.3。
- `get_user_tweets`/`search_recent` 的 `since_id` 为**客户端过滤**（twifork 无服务端 since_id）：拉取后丢弃 `tweet_id <= since_id` 的条目；`reads_consumed` 记 0（不花官方读额度，Budget 不扣，但 action_log 仍记次数）。

### 3.5 factory（`adapters/factory.py`）

```python
def get_client(account: Account) -> XClient:
    """按 account.access_type 构造并缓存客户端实例（dict[account_id, XClient]）。
    official: 从 secrets.toml [x.<credential_ref>] 取 4 项凭据，缺项抛 CredentialMissing(XClientError)。
    unofficial: cookies_path = data/cookies/<credential_ref>.json。
    account.is_primary and access_type=='unofficial' → 直接抛 ValueError（代码级禁止，FR-1.3）。"""

def invalidate(account_id: int) -> None:
    """凭据更新/账号停用时清缓存。"""
```

---

## 4. 数据库完整 DDL（SQLite 方言）

> ORM（`db/models.py`）与此 DDL 1:1 对应；建表由 `migrations.py` 执行原生 SQL（版本 1）。
> 连接初始化必须执行：`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`

```sql
CREATE TABLE schema_version (
    version     INTEGER NOT NULL
);
INSERT INTO schema_version(version) VALUES (1);

CREATE TABLE accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    handle              TEXT    NOT NULL UNIQUE,            -- 不含 @
    display_name        TEXT    NOT NULL DEFAULT '',
    access_type         TEXT    NOT NULL CHECK (access_type IN ('official','unofficial')),
    is_primary          INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
    credential_ref      TEXT    NOT NULL,                   -- secrets.toml 节名 / cookies 文件名
    daily_post_limit    INTEGER NOT NULL DEFAULT 10  CHECK (daily_post_limit  >= 0),
    daily_reply_limit   INTEGER NOT NULL DEFAULT 15  CHECK (daily_reply_limit >= 0),
    min_interval_sec    INTEGER NOT NULL DEFAULT 180 CHECK (min_interval_sec >= 0),
    max_interval_sec    INTEGER NOT NULL DEFAULT 600 CHECK (max_interval_sec >= min_interval_sec),
    active_hours_start  TEXT    NOT NULL DEFAULT '09:00',   -- 'HH:MM'（账号时区）
    active_hours_end    TEXT    NOT NULL DEFAULT '22:00',
    timezone            TEXT    NOT NULL DEFAULT 'Asia/Tokyo',
    status              TEXT    NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','paused','auth_error')),
    next_allowed_at     TEXT,                               -- 分发器写入（v1.0 §8.4）
    note                TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    -- 主号禁 unofficial：界面+代码之外的第三重保险（FR-1.3）
    CHECK (NOT (is_primary = 1 AND access_type = 'unofficial'))
);

CREATE TABLE media_assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT    NOT NULL,                           -- 相对 data/media/ 的路径
    media_type  TEXT    NOT NULL CHECK (media_type IN ('image','video')),
    alt_text    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE materials (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT    NOT NULL CHECK (kind IN ('post','reply')),
    text                 TEXT    NOT NULL,
    lang                 TEXT    NOT NULL,                  -- BCP-47 小写：'ja','en','zh'…
    translation_group_id INTEGER,                           -- 同组同值；首条创建后回填自身 id
    scenario_tags        TEXT    NOT NULL DEFAULT '',       -- 逗号分隔，无空格，小写
    media_ids            TEXT    NOT NULL DEFAULT '[]',     -- JSON 数组 → media_assets.id
    created_by           TEXT    NOT NULL DEFAULT 'human' CHECK (created_by IN ('human','ai')),
    status               TEXT    NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft','active','archived')),
    usage_count          INTEGER NOT NULL DEFAULT 0,
    last_used_at         TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_materials_pick ON materials(kind, status, lang);        -- 匹配引擎粗筛
CREATE INDEX ix_materials_group ON materials(translation_group_id);    -- 翻译组并排视图

CREATE TABLE scheduled_posts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    material_id    INTEGER NOT NULL REFERENCES materials(id),
    schedule_type  TEXT    NOT NULL CHECK (schedule_type IN ('once','daily','weekly','cron')),
    schedule_expr  TEXT    NOT NULL,   -- once:'2026-08-01T21:00'(本地) daily:'21:00'
                                       -- weekly:'mon,thu 21:00' cron:5 段表达式
    next_run_at    TEXT,               -- UTC；done 后为 NULL
    auto_approve   INTEGER NOT NULL DEFAULT 0 CHECK (auto_approve IN (0,1)),
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','paused','done','missed')),
    last_run_at    TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_sched_due ON scheduled_posts(status, next_run_at);      -- 到点扫描

CREATE TABLE watched_users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    handle             TEXT    NOT NULL UNIQUE,
    x_user_id          TEXT    NOT NULL UNIQUE,             -- 添加时经 get_user_by_handle 解析
    last_seen_tweet_id TEXT,                                -- since_id 游标
    include_replies    INTEGER NOT NULL DEFAULT 0 CHECK (include_replies IN (0,1)),
    enabled            INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    hit_count          INTEGER NOT NULL DEFAULT 0,          -- 最近命中数（UI 展示）
    note               TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE search_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    keyword_query       TEXT    NOT NULL,                   -- X 查询语法（粗筛）
    semantic_criteria   TEXT    NOT NULL,                   -- 自然语言（LLM 精筛）
    lang                TEXT    NOT NULL DEFAULT 'ja',
    newest_id_cursor    TEXT,
    max_results_per_run INTEGER NOT NULL DEFAULT 15 CHECK (max_results_per_run BETWEEN 10 AND 100),
    min_llm_score       INTEGER NOT NULL DEFAULT 7  CHECK (min_llm_score BETWEEN 0 AND 10),
    enabled             INTEGER NOT NULL DEFAULT 1  CHECK (enabled IN (0,1)),
    last_run_at         TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE target_tweets (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id             TEXT    NOT NULL UNIQUE,
    author_id            TEXT    NOT NULL,
    author_handle        TEXT    NOT NULL DEFAULT '',
    text                 TEXT    NOT NULL,
    lang                 TEXT,                              -- API lang / LLM 兜底判定结果
    text_zh              TEXT,                              -- 中文翻译缓存（进队列时填）
    tweet_created_at     TEXT    NOT NULL,
    source               TEXT    NOT NULL CHECK (source IN ('monitor','search')),
    source_rule_id       INTEGER,       -- source='search'→search_rules.id；'monitor'→watched_users.id
    llm_relevance_score  INTEGER CHECK (llm_relevance_score BETWEEN 0 AND 10),
    llm_relevance_reason TEXT,
    process_status       TEXT    NOT NULL DEFAULT 'new'
                         CHECK (process_status IN ('new','queued','no_match','filtered','expired')),
    fetched_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_target_status  ON target_tweets(process_status, fetched_at);
CREATE INDEX ix_target_author  ON target_tweets(author_id);

CREATE TABLE review_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    action_type       TEXT    NOT NULL CHECK (action_type IN ('post','reply')),
    target_tweet_id   INTEGER REFERENCES target_tweets(id),  -- reply 必填；post 为 NULL
    material_id       INTEGER REFERENCES materials(id),
    scheduled_post_id INTEGER REFERENCES scheduled_posts(id),-- 定时发推来源（post 类）
    final_text        TEXT    NOT NULL,
    final_media_ids   TEXT    NOT NULL DEFAULT '[]',
    llm_reason        TEXT    NOT NULL DEFAULT '',
    llm_confidence    REAL    CHECK (llm_confidence BETWEEN 0 AND 1),
    is_auto_translated INTEGER NOT NULL DEFAULT 0 CHECK (is_auto_translated IN (0,1)),
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','sending','sent',
                                        'failed','skipped','expired')),
    skip_reason       TEXT,                                 -- ComplianceGuard 拦截码等
    auto_approve      INTEGER NOT NULL DEFAULT 0 CHECK (auto_approve IN (0,1)),
    retry_count       INTEGER NOT NULL DEFAULT 0,
    sent_tweet_id     TEXT,
    error_msg         TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    decided_at        TEXT,                                 -- 人审时间
    sent_at           TEXT,
    expires_at        TEXT                                  -- reply 类必填；post 类 NULL
    ,
    CHECK (NOT (action_type = 'reply' AND target_tweet_id IS NULL))
);
CREATE INDEX ix_queue_dispatch ON review_queue(status, account_id, created_at); -- 分发器取件
CREATE INDEX ix_queue_pending  ON review_queue(status, expires_at);             -- 过期扫描

CREATE TABLE interactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    action      TEXT    NOT NULL CHECK (action IN ('post','reply')),
    tweet_id    TEXT    NOT NULL,   -- reply: 目标推文 id；post: 发出的推文 id
    author_id   TEXT,               -- reply: 目标作者；post: NULL
    sent_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
-- 去重账本铁律：同一目标推文全体自有账号只回一次（FR-7.2）→ 部分唯一索引
CREATE UNIQUE INDEX ux_interactions_reply ON interactions(tweet_id) WHERE action = 'reply';
CREATE INDEX ix_interactions_cooldown ON interactions(author_id, sent_at);  -- 冷却查询
CREATE INDEX ix_interactions_daily    ON interactions(account_id, action, sent_at); -- 日上限计数

CREATE TABLE blacklist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    x_user_id  TEXT    NOT NULL UNIQUE,
    handle     TEXT    NOT NULL DEFAULT '',
    reason     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE action_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER,                                 -- LLM 调用等可为 NULL
    api_kind       TEXT    NOT NULL CHECK (api_kind IN ('x_official','x_unofficial','llm')),
    endpoint       TEXT    NOT NULL,                        -- 'search_recent'/'llm.match'…
    reads_consumed INTEGER NOT NULL DEFAULT 0,
    tokens_in      INTEGER NOT NULL DEFAULT 0,              -- llm 专用
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    has_link       INTEGER NOT NULL DEFAULT 0,              -- 发送类：文案含 URL（§0.1 对账）
    success        INTEGER NOT NULL CHECK (success IN (0,1)),
    error          TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_actionlog_usage ON action_log(api_kind, created_at);    -- 用量统计

CREATE TABLE app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

---

## 5. X API 端点映射表

### 5.1 OfficialXClient ↔ tweepy（X API v2；媒体走 v1.1）

tweepy 版本锁定 `tweepy>=4.14,<5`。所有 v2 调用共用字段参数：
`TWEET_FIELDS = ["created_at","lang","referenced_tweets","in_reply_to_user_id","author_id"]`、`EXPANSIONS = ["author_id"]`、`USER_FIELDS = ["username","name"]`。

| 适配器方法 | tweepy 调用 | 参数映射与说明 | 读记账（reads_consumed） |
|---|---|---|---|
| `get_me()` | `client.get_me(user_fields=USER_FIELDS)` | — | 0（用户查询不占推文读；payg 下约 $0.01/次，记 endpoint 便于对账） |
| `get_user_by_handle(h)` | `client.get_user(username=h, user_fields=USER_FIELDS)` | handle 去掉开头 `@` | 0 |
| `post(text, media_ids)` | `client.create_tweet(text=text, media_ids=media_ids or None)` | — | 0（写操作按发推计费） |
| `reply(text, id, media_ids)` | `client.create_tweet(text=text, in_reply_to_tweet_id=id, media_ids=media_ids or None)` | — | 0 |
| `get_user_tweets(uid, since_id, n, incl_replies)` | `client.get_users_tweets(id=uid, since_id=since_id, max_results=clamp(n,5,100), exclude=excl, tweet_fields=TWEET_FIELDS, expansions=EXPANSIONS, user_fields=USER_FIELDS)` | `excl=["retweets"]`，`incl_replies=False` 时再加 `"replies"`；API 要求 max_results≥5，调用后在适配器内截断到 n；`since_id=None`（首次设游标）时 n 固定 5、仅返回最新 1 条 | `len(返回推文)`（含被截断丢弃的，按 API 实际返回计） |
| `search_recent(q, since_id, start, n)` | `client.search_recent_tweets(query=q, since_id=since_id, start_time=start, max_results=clamp(n,10,100), tweet_fields=TWEET_FIELDS, expansions=EXPANSIONS, user_fields=USER_FIELDS)` | `start_time` 转 RFC3339，且不早于 `now-7d+60s`（recent 窗口硬限制）；`since_id` 与 `start_time` 同传时 API 以 since_id 为准（tweepy 允许并存） | `len(返回推文)` |
| `upload_media(path, type, alt)` | `api_v1.media_upload(filename=path, chunked=True, media_category=cat)`；随后 `api_v1.create_media_metadata(media_id, alt)`（alt 非空时） | `cat='tweet_image'|'tweet_video'`；视频依赖 tweepy chunked upload 内置的 processing 等待，超时 300s 抛 MediaError | 0 |

> ⚠️ 联调确认点（Phase 1）：按量付费新应用对 **v1.1 media/upload** 的开放情况需实测；若被拒（403），改走 v2 `POST /2/media/upload`（tweepy 新版 `Client.media_upload`，不可用则直接 `requests` 实现，接口签名不变）。

作者 handle 回填：v2 返回的 `includes.users` 按 `author_id` 建映射填 `TweetData.author_handle`；缺失时填 `""`，由上层用 `get_user_by_handle` 补或留空。

### 5.2 UnofficialXClient ↔ twifork（`import twikit`，全 async，经 `_run()` 同步化）

| 适配器方法 | twifork 调用 | 参数映射与说明 |
|---|---|---|
| `get_me()` | `await client.user()` | 返回 `twikit.User` → `UserData(id, screen_name, name)`；首调用前先 `client.load_cookies(path)` |
| `get_user_by_handle(h)` | `await client.get_user_by_screen_name(h)` | — |
| `post(text, media_ids)` | `await client.create_tweet(text=text, media_ids=media_ids or None)` | — |
| `reply(text, id, media_ids)` | `await client.create_tweet(text=text, reply_to=id, media_ids=media_ids or None)` | — |
| `get_user_tweets(uid, since_id, n, incl_replies)` | `await client.get_user_tweets(uid, tweet_type, count=max(n,5))` | `tweet_type='Replies' if incl_replies else 'Tweets'`；**since_id 为客户端过滤**（丢弃 `int(tweet_id) <= int(since_id)`）；RT 按 `tweet.retweeted_tweet is not None` 过滤 |
| `search_recent(q, since_id, start, n)` | `await client.search_tweet(q2, product='Latest', count=n)` | 查询语法转换：`-is:retweet→-filter:retweets`、`is:reply→filter:replies`、`-is:reply→-filter:replies`，其余原样；since_id/start 客户端过滤。（默认搜索走官方主号，此实现仅备用） |
| `upload_media(path, type, alt)` | `mid = await client.upload_media(path, wait_for_completion=True)`；alt 非空再 `await client.create_media_metadata(mid, alt)` | 视频 `wait_for_completion` 内部轮询处理状态 |

非官方调用 `reads_consumed` 一律记 0（不消耗官方读额度），但 `action_log(api_kind='x_unofficial')` 照记次数。twifork 各方法/异常名以安装的 2.3.5 实际为准，Phase 4 联调时逐一核对（本表按 twikit 上游 API 编写，twifork 为 drop-in 兼容）。

### 5.3 异常映射表（两适配器的 `_map_error`）

| 统一异常 | tweepy 来源 | twifork（twikit.errors）来源 | 后续动作（§5.4） |
|---|---|---|---|
| `RateLimited` | `TooManyRequests`（429；`reset_at` ← 响应头 `x-rate-limit-reset`） | `TooManyRequests`（headers 有则取，无则 None） | 可等待重试 |
| `AuthExpired` | `Unauthorized`（401）；`Forbidden` 且消息含 suspended/locked | `Unauthorized` / `AccountLocked` / `AccountSuspended` / cookies 文件缺失或加载失败 | 账号→auth_error，终态 |
| `DuplicateContent` | `Forbidden` 且消息含 "duplicate" | `DuplicateTweet` | 条目 failed，终态 |
| `PermissionDenied` | 其余 `Forbidden`（403）、`BadRequest`（400，含查询语法错误——错误消息带中文说明） | `BadRequest` / `Forbidden` | 条目 failed，终态 |
| `TargetNotFound` | `NotFound`（404） | `TweetNotAvailable` / `UserNotFound` / `UserUnavailable` | 条目 failed / 监控项提示，终态 |
| `MediaError` | media_upload 处理失败/超时/格式拒绝 | `InvalidMedia` / 上传处理失败 | 条目 failed，终态 |
| `NetworkError` | `TwitterServerError`（5xx）、`requests` 连接/超时异常 | `aiohttp` 连接/超时异常、5xx | 重试 ≤2（退避 5s/25s） |
| `XClientError`（兜底） | 其他未识别异常 | 其他未识别异常 | 终态，记原始异常入日志 |

### 5.4 限流与重试统一策略

- **读类 job（monitor/search）**：捕获 `RateLimited` → 本轮 job 立即收尾退出（已入库条目与已推进游标保留），`action_log` 记 `success=0, error='rate_limited'`，等下一轮调度自然重试。**job 内绝不 sleep 等窗口**（避免占住线程）。
- **发送（dispatcher）**：`RateLimited` → 条目保持 `approved` 不动、不计 `retry_count`，`accounts.next_allowed_at = max(reset_at, now+15min)`；`NetworkError` → 同条目重试 ≤2 次（间隔 5s、25s），仍败置 `failed`；其余异常一律直接 `failed` 并记 `error_msg`（中文）。
- **tweepy `wait_on_rate_limit=False`**（§3.3）：限流全部走上述统一策略，禁止库内部阻塞。
- 每次 X 调用（无论成败）写 `action_log`：`api_kind`、`endpoint`（=适配器方法名）、`reads_consumed`、`duration_ms`、`success`、`error`。

---

## 6. LLM Prompt 全文（`llm/prompts.py`）

### 6.0 通用契约

- 全部走 OpenAI 兼容 `POST /chat/completions`（base_url = apimax 网关）。`messages = [system] + few_shot対 + [user]`（few-shot 以 user/assistant 消息对形式内置）。
- 模型档位：相关性打分/翻译/推文中译/语言判定 → `llm_model_light`；最佳匹配/素材撰写 → `llm_model_strong`。temperature：打分/翻译/中译/判定 0.2；匹配/撰写 0.7。
- 请求 `response_format={"type":"json_object"}`；网关报不支持（400）时自动去掉该参数降级重发（LLMClient 记忆该降级，本进程内不再带）。
- 解析容错：取输出中首个 `{` 到最后一个 `}` 的子串做 `json.loads`；失败则追加 user 消息「你上一次的输出不是合法 JSON。请只输出符合要求格式的 JSON，不要包含任何解释、markdown 代码块或多余文字。」重试 1 次；仍失败抛 `LLMFormatError`（匹配/打分场景按 no_match 处理，v1.0 §11）。
- 所有 `reason` 类字段一律**简体中文**（给审核者看）；`adapted_text`/`text` 用目标内容语言。
- 共通铁律（写入匹配与撰写的 system）：**宁可跳过也不发像广告骚扰的回复**。

### 6.1 场景一：相关性打分（scene=`relevance`，轻量档）

**System 模板（全文）**：

```text
你是一个严格的推文筛选助手，为 X（推特）运营工具做候选推文的相关性打分。
你会收到一个「筛选条件」（自然语言描述的目标人群状态）和一批候选推文。
任务：判断每条推文的作者本人是否真的处于筛选条件所描述的状态/需求中，打 0-10 分：
- 9~10：明确符合，作者正在亲身表达该状态
- 6~8：大概率符合，但表达间接或信息不完整
- 3~5：话题相关但意图不符（新闻转述、科普教程、招聘、他人转达、推广营销）
- 0~2：不相关
铁律：
1. 宁低勿高：拿不准就给低分。新闻报道、营销推广、招聘、课程/教程分享一律 ≤3 分。
2. reason 用简体中文一句话（40 字以内）说明打分依据。
3. 只输出 JSON，不得输出任何其他文字。必须覆盖输入中的每一个 tweet_id，不得遗漏或新增。
输出格式：
{"results": [{"tweet_id": "字符串", "score": 整数0-10, "reason": "中文一句话"}]}
```

**User 模板**：

```text
筛选条件：{semantic_criteria}

候选推文（JSON 数组）：
{tweets_json}
```

其中 `tweets_json = [{"tweet_id": "...", "author_handle": "...", "text": "..."}, ...]`（一批 ≤ `max_results_per_run` 条，一次调用，NFR-4）。

**输出 JSON Schema**：

```json
{
  "type": "object", "required": ["results"],
  "properties": {
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["tweet_id", "score", "reason"],
        "properties": {
          "tweet_id": {"type": "string"},
          "score": {"type": "integer", "minimum": 0, "maximum": 10},
          "reason": {"type": "string", "maxLength": 60}
        }
      }
    }
  }
}
```

**Few-shot 示例（内置 user/assistant 消息对，日语场景）**：

示例 user：

```text
筛选条件：作者在抱怨多个 AI 模型的 API 使用成本高，或正在寻找降低 API 成本的办法

候选推文（JSON 数组）：
[{"tweet_id": "1001", "author_handle": "dev_taro", "text": "OpenAIとClaude両方契約してるけど、API代だけで月3万超えた…そろそろ一本化するか、安いゲートウェイ探すかしないと無理だわ"},
 {"tweet_id": "1002", "author_handle": "ai_news_jp", "text": "【解説】主要LLM APIの料金比較 2026年版をブログにまとめました。ご参考までに → https://example.com/blog"},
 {"tweet_id": "1003", "author_handle": "yamada_eng", "text": "GPT-5のAPI、いつの間にか値上げされてない？今月の請求見てびっくりした"},
 {"tweet_id": "1004", "author_handle": "lunch_lover", "text": "今日のランチ1800円もした。物価高やばい"}]
```

示例 assistant：

```json
{"results": [
  {"tweet_id": "1001", "score": 9, "reason": "作者亲身抱怨多模型API月费过高，并在寻找网关等解决方案"},
  {"tweet_id": "1002", "score": 2, "reason": "资讯博主的科普分享，作者本人无成本痛点"},
  {"tweet_id": "1003", "score": 7, "reason": "在抱怨API涨价账单，但仅涉及单一模型，间接符合"},
  {"tweet_id": "1004", "score": 0, "reason": "谈午餐物价，与API成本无关"}
]}
```

### 6.2 场景二：最佳匹配（scene=`match`，强档）

**System 模板（全文）**：

```text
你是一个 X（推特）运营助手，任务是为一条目标推文挑选最合适的回复素材，并把素材改写成贴合语境的回复文案。
你会收到：目标推文（作者、语言、原文）和候选素材列表（各有 id、场景标签 tags、正文 text）。
判断与改写规则：
1. 只有当某条素材对这条推文来说自然、有帮助、不突兀时才选择它；否则 choice 必须为 null。
   铁律：宁可跳过，也绝不发出像广告骚扰的回复。对方在提问求助但素材答非所问、对方情绪不适合插入推广、
   对方是同行/媒体号等情况，都应返回 null。
2. adapted_text 必须使用目标推文的语言撰写，并且呼应原文的具体内容（引用对方提到的具体痛点、数字、
   工具名等），不能是一段可以贴给任何人的通用文案。
3. 保留素材中的核心信息与链接：URL、@提及、#标签必须原样保留；其余措辞每次自然变化，
   不得与素材原文逐字相同（避免重复内容判定）。
4. 语气像一个真实使用者在搭话：不用推销腔，不用夸张营销词，表情符号至多 1 个。
5. 长度合规：X 加权长度 ≤ 280（URL 记 23，中日韩每字记 2）。
6. reason 用简体中文（60 字以内）说明选择或不选的理由；confidence 是 0~1 的小数，
   表示「这条回复发出去是恰当的」的把握；choice 为 null 时 confidence 填 0、adapted_text 填 null。
只输出 JSON，格式：
{"choice": 素材id整数或null, "confidence": 0~1小数, "reason": "中文", "adapted_text": "目标语言文案或null"}
```

**User 模板**：

```text
目标推文：
- 作者：@{author_handle}
- 语言：{lang}
- 原文：{text}

候选素材（JSON 数组）：
{materials_json}
```

其中 `materials_json = [{"id": 3, "tags": "cost,gateway", "text": "..."}, ...]`（≤10 条，FR-6.1）。

**输出 JSON Schema**：

```json
{
  "type": "object",
  "required": ["choice", "confidence", "reason", "adapted_text"],
  "properties": {
    "choice": {"type": ["integer", "null"]},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "reason": {"type": "string", "maxLength": 90},
    "adapted_text": {"type": ["string", "null"]}
  }
}
```

**Few-shot 示例 1（命中，日语）**——示例 user：

```text
目标推文：
- 作者：@dev_taro
- 语言：ja
- 原文：OpenAIとClaude両方契約してるけど、API代だけで月3万超えた…そろそろ一本化するか、安いゲートウェイ探すかしないと無理だわ

候选素材（JSON 数组）：
[{"id": 3, "tags": "cost,gateway", "text": "複数のAIモデルを使うなら、APIMaxのようなゲートウェイで一括管理するとコストが見える化できて便利です。詳しくは → https://apimax.io"},
 {"id": 7, "tags": "release,news", "text": "APIMaxが新モデルに対応しました！ぜひお試しください → https://apimax.io"}]
```

示例 assistant：

```json
{"choice": 3, "confidence": 0.85,
 "reason": "对方明确在找便宜网关做一本化，素材3的网关方案直接回应其痛点；素材7是新闻式推广不贴切",
 "adapted_text": "月3万は痛いですよね…うちもOpenAIとClaude併用で同じ悩みでした。APIMaxみたいなゲートウェイに集約したら請求が一本化されて、モデルごとのコストも見えるようになったので、一本化を検討中なら選択肢に入れてみてください → https://apimax.io"}
```

**Few-shot 示例 2（必须跳过，日语）**——示例 user：

```text
目标推文：
- 作者：@keiri_san
- 言語：ja
- 原文：API利用料の請求書処理、経理としては勘定科目どれに振るのが正解なんだろう。通信費？支払手数料？

候选素材（JSON 数组）：
[{"id": 3, "tags": "cost,gateway", "text": "複数のAIモデルを使うなら、APIMaxのようなゲートウェイで一括管理するとコストが見える化できて便利です。詳しくは → https://apimax.io"}]
```

示例 assistant：

```json
{"choice": null, "confidence": 0,
 "reason": "对方是经理岗在问会计科目归类，不是API成本痛点人群，插入产品推广会显得像骚扰广告",
 "adapted_text": null}
```

### 6.3 场景三：素材撰写（scene=`write`，强档）

**System 模板（全文）**：

```text
你是一个多语言社交媒体文案专家，为 X（推特）撰写运营素材。
你会收到：主题、要点、风格要求、目标语言列表、每种语言需要的候选条数 N。
规则：
1. 每条文案要符合目标语言圈的推特表达习惯（日语可适度使用体言止め、口语化敬体；英语简洁直接；
   中文自然口语），不能是翻译腔。
2. X 加权长度 ≤ 280（URL 记 23，中日韩每字记 2）。要点中给出的 URL、@提及、#标签、占位符（如 {name}）
   必须原样保留。
3. 同一语言的 N 条候选之间，切入角度或语气要有明显差异（如：痛点共鸣型 / 数据事实型 / 提问互动型），
   不是同义改写。
4. 不使用夸张营销词（「衝撃」「神ツール」这类），表情符号每条至多 1 个。
   铁律：写出来的内容宁可平实，也不能像垃圾广告。
只输出 JSON，格式：
{"results": [{"lang": "语言代码", "text": "文案"}]}
共 语言数 × N 条，按语言分组排列。
```

**User 模板**：

```text
主题：{topic}
要点（必须涵盖）：{points}
风格：{style}
目标语言：{langs}    # 如 ["ja", "en"]
每种语言条数：{n}
```

**输出 JSON Schema**：

```json
{
  "type": "object", "required": ["results"],
  "properties": {
    "results": {
      "type": "array",
      "items": {
        "type": "object", "required": ["lang", "text"],
        "properties": {"lang": {"type": "string"}, "text": {"type": "string"}}
      }
    }
  }
}
```

**Few-shot 示例（日语，1 语言 × 2 条）**——示例 user：

```text
主题：APIMax 多模型 API 网关帮开发者省成本
要点（必须涵盖）：一个入口调用多家模型；按用量计费无月费；链接 https://apimax.io
风格：面向个人开发者，务实、有共鸣，不吹嘘
目标语言：["ja"]
每种语言条数：2
```

示例 assistant：

```json
{"results": [
  {"lang": "ja", "text": "複数のAIモデルを使い分けてると、契約もAPIキーも請求もバラバラになりがち。APIMaxなら1つの入口で各社モデルを呼べて、料金は使った分だけ（月額なし）。個人開発の管理コストがだいぶ減りました → https://apimax.io"},
  {"lang": "ja", "text": "AIのAPI代、モデルごとに請求が来て把握しきれてない人いませんか？APIMaxは複数モデルを一括で呼べるゲートウェイ。従量課金で月額固定費ゼロなので、まず小さく試すのに向いてます → https://apimax.io"}
]}
```

### 6.4 场景四：素材翻译（scene=`translate`，轻量档）

**System 模板（全文）**：

```text
你是一个社交媒体本地化译者。把给定的 X（推特）素材翻译成目标语言。
规则：
1. 本地化而非直译：译文要像目标语言圈的母语者发的推文，语气、习惯与原文意图一致。
2. @提及、#话题标签、URL、占位符（{xxx} 形式）一律原样保留，不得翻译或改动。
3. X 加权长度 ≤ 280（URL 记 23，中日韩每字记 2）；必要时压缩措辞，但不得丢失核心信息与链接。
只输出 JSON，格式：{"text": "译文"}
```

**User 模板**：

```text
原文（{source_lang}）：
{text}

目标语言：{target_lang}
```

**输出 JSON Schema**：`{"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}`

**Few-shot 示例（zh→ja）**——示例 user：

```text
原文（zh）：
多个 AI 模型来回切换，API Key 和账单管理太头疼？用 APIMax 一个入口全搞定，按量付费没有月费 → https://apimax.io

目标语言：ja
```

示例 assistant：

```json
{"text": "複数のAIモデルを行き来して、APIキーも請求もバラバラで困っていませんか？APIMaxなら1つの入口でまとめて呼び出せて、料金は使った分だけ（月額なし）→ https://apimax.io"}
```

### 6.5 场景五：推文中译・审核辅助（scene=`tweet_zh`，轻量档）

**System 模板（全文）**：

```text
你是审核辅助翻译。把一条外语推文翻译成简体中文，供中文运营者快速理解原意。
规则：
1. 以忠实直译为主，保留原文语气，不做美化和省略。
2. 原文中的俚语、网络用语、缩写、文化梗，在对应译文后用括号简要注释。
3. @提及、#标签、URL 原样保留。
只输出 JSON，格式：{"text_zh": "中文译文"}
```

**User 模板**：

```text
推文原文：
{text}
```

**输出 JSON Schema**：`{"type": "object", "required": ["text_zh"], "properties": {"text_zh": {"type": "string"}}}`

**Few-shot 示例（日语）**——示例 user：

```text
推文原文：
API代えぐすぎて草。もう自腹きつい、会社持ちにしてほしいわ…
```

示例 assistant：

```json
{"text_zh": "API 费用太离谱了，笑死（草：日语网络用语，相当于「哈哈」）。自掏腰包已经吃不消了，真希望公司能报销…"}
```

### 6.6 附加小场景：语言判定（scene=`detect_lang`，轻量档）

FR-6.1 的 LLM 兜底判定（API lang 字段缺失时用）。System：「判断下面这条推文的主要语言，只输出 JSON：{"lang": "BCP-47 小写语言代码，如 ja/en/zh"}。混合语言取占比最高者。」User：推文原文。无 few-shot。

---

## 7. core 与 llm 模块接口定义

### 7.1 ComplianceGuard（`core/compliance.py`）

```python
class GuardCode(StrEnum):
    ACCOUNT_NOT_ACTIVE   = "account_not_active"    # 软
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"  # 软
    INTERVAL_NOT_ELAPSED = "interval_not_elapsed"  # 软
    DAILY_LIMIT_REACHED  = "daily_limit_reached"   # 软（滞留次日发出，v1.0 §12.3）
    ALREADY_REPLIED      = "already_replied"       # 硬
    AUTHOR_IN_COOLDOWN   = "author_in_cooldown"    # 硬
    BLACKLISTED          = "blacklisted"           # 硬
    TARGET_EXPIRED       = "target_expired"        # 硬（expires_at 已过）

@dataclass(frozen=True)
class GuardResult:
    ok: bool
    code: GuardCode | None      # ok=True 时为 None
    hard: bool                  # 硬违规→条目置 skipped(skip_reason=code)；软→条目保持 approved 本轮跳过
    detail: str                 # 中文人话，直接可展示

class ComplianceGuard:
    def __init__(self, session_factory: Callable[[], Session], settings: Settings): ...

    def check(self, account: Account, item: ReviewQueueItem,
              now: datetime | None = None) -> GuardResult:
        """发送前最终校验（FR-7.2 全项）。按上表顺序逐项检查，返回第一个未通过项。
        只读不写库；不抛业务异常（DB 异常向上抛）。"""

    def is_in_active_hours(self, account: Account, now: datetime) -> bool:
        """按 account.timezone 换算本地时间；支持跨日时段（如 22:00~02:00：
        start > end 时判定 t >= start or t < end）。start == end 视为全天活跃。"""

    def daily_action_count(self, account_id: int, action: str, now: datetime) -> int:
        """当日（按账号时区的自然日）该账号该类动作数，查 interactions 表。"""

    def effective_limits(self, account: Account) -> tuple[int, int]:
        """(daily_post_limit, daily_reply_limit)。unofficial 且处于养号期
        （created_at 距今 < nurture_days）时上限减半（向下取整，至少 1），NFR-1。"""
```

### 7.2 Budget（`core/budget.py`）

```python
@dataclass(frozen=True)
class BudgetUsage:
    reads_today: int; reads_month: int
    daily_budget: int; est_cost_month_usd: float
    llm_calls_month: int; llm_est_cost_usd: float
    circuit_open: bool                      # True=已熔断

class Budget:
    def __init__(self, session_factory, settings): ...
    def today_reads_left(self) -> int       # daily_budget - 今日 action_log 读合计（UTC 日）
    def can_spend(self, estimated_reads: int) -> bool
        # 熔断判定：today_reads_left() - estimated_reads >= budget_reserve_reads
        # False 时 monitor/search job 直接返回（发送不受影响，FR-8.3）
    def recompute_daily(self) -> int
        # 启动时执行：quota 模式 = (月额度-本月已读)/当月剩余天数；
        # payg 模式 = (月预算/0.005 - 本月已读)/剩余天数；结果写 app_settings.daily_read_budget
        # 用户手动改过当日预算（键 daily_read_budget_manual=日期）则跳过
    def usage(self) -> BudgetUsage          # 仪表盘数据源，聚合 action_log
```

### 7.3 MatchEngine（`core/matcher.py`）

```python
@dataclass(frozen=True)
class MatchOutcome:
    status: Literal["queued", "no_match"]
    queue_id: int | None
    reason: str                             # 中文（LLM reason 或流程原因）

class MatchEngine:
    def __init__(self, llm: LLMClient, translator: Translator,
                 session_factory, settings): ...

    def run(self, target: TargetTweet, account: Account) -> MatchOutcome:
        """v1.0 §8.3 全流程。副作用：更新 target.process_status（queued/no_match）、
        target.text_zh、写入 review_queue(pending)。
        LLM 抛 LLMError/LLMFormatError → 按 no_match 处理并记日志（v1.0 §11）。"""

    def pick_candidates(self, lang: str, tags: list[str]) -> list[Material]:
        """kind='reply' AND status='active' AND lang=lang；
        与 tags 有交集者优先，不足 10 条时用无交集素材补足到全量（FR-6.1 无交集则全量）；
        同优先级按 (usage_count ASC, last_used_at ASC) 排序，截取 ≤10 条。"""

    def fallback_translate(self, lang: str, tags: list[str]) -> Material | None:
        """FR-6.2：同语言无素材时，从翻译组挑他语言版本（同排序规则取 1 条）现场翻译，
        生成临时候选（不入 materials 表，队列条目 is_auto_translated=1、material_id 指向源素材）。"""
```

### 7.4 监控与搜索 job（`core/monitor.py` / `core/search.py`）

```python
@dataclass
class MonitorStats:
    users_polled: int; tweets_fetched: int; queued: int; filtered: int; errors: int

class MonitorJob:
    def __init__(self, budget, match_engine, session_factory, settings): ...
    def run_once(self) -> MonitorStats:
        """v1.0 §8.1。入口先 budget.can_spend(len(enabled_users)*5)，不足直接返回。
        逐个推主处理，单推主异常不影响其余（try/except 收集到 errors）。
        使用的账号：MVP 取第一个 active official 账号（读走官方额度）。"""
    def precheck(self, t: TweetData) -> str | None:
        """FR-4.3 预检，返回过滤原因码或 None：
        'retweet' / 'blacklisted' / 'already_replied'（查 interactions）
        / 'author_cooldown' / 'too_old'（> tweet_max_age_hours）/ 'own_account'（自有账号发的）。
        被过滤推文仍写入 target_tweets(process_status='filtered')，存档不进队列（FR-4.4）。"""

class ScoredCandidate(NamedTuple):
    tweet: TweetData; score: int; reason: str

class SearchJob:
    def __init__(self, budget, match_engine, llm, session_factory, settings): ...
    def run_once(self) -> SearchStats:      # 字段同 MonitorStats 增 rules_run
    def run_rule(self, rule: SearchRule, dry_run: bool = False) -> list[ScoredCandidate]:
        """v1.0 §8.2 两级漏斗。dry_run=True（UI 试运行按钮）：拉取+LLM 打分后直接返回，
        不写 target_tweets、不推进 newest_id_cursor、不触发 MatchEngine；
        读额度照扣、action_log 照记。"""
```

### 7.5 Dispatcher（`core/dispatcher.py`）

```python
class Dispatcher:
    def __init__(self, guard: ComplianceGuard, session_factory, settings): ...

    def tick(self) -> None:
        """每 60s。对每个 status='active' 账号：
        1) guard 软条件预判（活跃时段 / next_allowed_at）不满足 → 跳过该账号本轮
        2) 取该账号最老 approved 条目（ix_queue_dispatch）；无则跳过
        3) 条目置 sending（乐观锁：UPDATE ... WHERE status='approved'，影响行数=0 则放弃）
        4) guard.check 硬违规 → skipped(skip_reason)；软违规 → 回置 approved
        5) send_item()
        账号间互不影响（逐账号 try/except）。"""

    def send_item(self, account: Account, item: ReviewQueueItem) -> None:
        """媒体上传（逐个 upload_media，MediaError→failed）→ post/reply →
        成功：item→sent(sent_tweet_id, sent_at)、写 interactions + action_log、
              素材 usage_count+1/last_used_at、account.next_allowed_at = now+random(min,max 区间)
        失败：按 §5.4 策略。写 interactions 与置 sent 在同一事务；
        interactions 唯一索引冲突（IntegrityError）说明竞态重复 → item 置 failed(error='去重账本冲突')。"""
```

### 7.6 调度与启动（`core/scheduler.py` / `core/startup.py` / `core/schedule_calc.py`）

```python
def build_scheduler(deps: AppDeps) -> BackgroundScheduler:
    """APScheduler。4 个 job（全部 max_instances=1, coalesce=True, misfire_grace_time=60）：
    - scheduled_check：every 60s。扫 scheduled_posts(status='active', next_run_at<=now)：
      生成 review_queue 条目（auto_approve→status='approved'，否则 'pending'；post 类 expires_at=NULL），
      last_run_at=now，next_run_at=compute_next_run(...)（once→status='done'）。
    - monitor_poll：every monitor_interval_minutes 分钟。
    - semantic_search：按 search_runs_per_day 均分活跃时段（如 2 次→10:00/16:00，账号时区）。
    - dispatcher_tick：every 60s。
    每个 job 函数体整体 try/except Exception + loguru（NFR-3 隔离）。"""

@dataclass
class StartupReport:
    messages: list[str]                     # 中文摘要，UI 启动后弹出

def run_startup_recovery(deps: AppDeps) -> StartupReport:
    """v1.0 §8.5 顺序执行：备份 db（backup_db，保留 14 份，失败仅告警不阻断）→
    补定时推文（宽限期内补队列 / 超期置 missed 并顺延）→ 队列过期扫描（pending 且
    expires_at<now → expired）→ 残留 sending 条目回置 approved（上次异常退出）→
    Budget.recompute_daily()。监控/搜索补扫无需专门处理（游标机制）。"""

def compute_next_run(schedule_type: str, schedule_expr: str,
                     after: datetime, tz: str) -> datetime | None:
    """once:'YYYY-MM-DDTHH:MM'(账号时区)→晚于 after 则返回该时刻否则 None；
    daily:'HH:MM'；weekly:'mon,thu 21:00'；cron:5 段表达式（用 croniter）。
    返回 UTC。表达式非法抛 ValueError（中文消息，UI 保存时校验）。"""
```

### 7.7 LLMClient / Translator / Writer（`llm/`）

```python
class LLMError(Exception): ...          # 网络/HTTP/超时（message 中文）
class LLMFormatError(LLMError): ...     # 重试 1 次后仍非法 JSON

class LLMClient:
    def __init__(self, base_url: str, api_key: str, settings, session_factory): ...
    def chat_json(self, scene: str, messages: list[dict],
                  required_keys: list[str], tier: Literal["light","strong"] = "light",
                  temperature: float = 0.2, timeout_sec: int = 60) -> dict:
        """§6.0 契约的唯一实现入口。scene 用于选模型覆写（app_settings 可按场景覆盖
        llm_model_<scene>，无则用档位默认）与 action_log 的 endpoint=f'llm.{scene}'。
        required_keys 做最小结构校验（缺键视同格式错误参与重试）。"""
    def ping(self) -> str               # 设置页测试连接：发一条 1 token 请求，异常转中文说明

class Translator:
    def __init__(self, llm: LLMClient): ...
    def translate_material(self, text: str, source_lang: str, target_lang: str) -> str
    def tweet_to_zh(self, text: str) -> str        # 失败返回 ""（审核页显示「翻译失败」，不阻断入队）
    def detect_lang(self, text: str) -> str        # scene=detect_lang；失败返回 'und'

@dataclass(frozen=True)
class GeneratedDraft:
    lang: str; text: str; over_length: bool        # weighted_len>280 → UI 标红仅可编辑后入库

class Writer:
    def __init__(self, llm: LLMClient): ...
    def generate(self, topic: str, points: str, style: str,
                 langs: list[str], n: int = 3) -> list[GeneratedDraft]
```

---

## 8. UI 页面线框（NiceGUI 组件层级）

### 8.0 公共外壳（`ui/layout.py`）

页面注册：每页独立路由 `@ui.page('/')`、`/queue`、`/materials`、`/watched`、`/rules`、`/schedule`、`/settings`。刷新机制：各页 `ui.timer(interval, refresh)` 轮询 DB（仪表盘 30s、队列 5s、其余 15s），NiceGUI WebSocket 自动推送渲染；后台 job 的新条目/异常通过 `ui.notify` 在下一次轮询时提示。

```
shell(title) —— contextmanager，所有页面套用
├─ ui.header (深色)
│  ├─ ui.label('x-operator')
│  ├─ ui.tabs/链接行: 仪表盘|审核队列(徽标:pending数)|素材库|监控推主|搜索规则|定时计划|设置
│  └─ ui.badge: 熔断中/auth_error 账号数（红色，异常时才显示）
└─ ui.column.classes('max-w-5xl mx-auto p-4')   ← 页面内容插槽
```

### 8.1 仪表盘（`ui/dashboard.py`，路由 `/`）

```
render_dashboard()
├─ ui.row: 概况卡 ×4（ui.card + 大数字 ui.label + 说明）
│    今日发送 | 队列积压(pending) | 今日 no_match 率 | 24h 失败/异常数(红)
├─ ui.row
│  ├─ ui.card 读额度: ui.linear_progress(reads_today/daily_budget) + 文本
│  │    「今日 132/330 · 本月 2,410 · 熔断:未触发」；熔断时进度条红色+提示行
│  └─ ui.card LLM 用量: 本月调用次数 + 估算费用（单价未配则只显示次数）
├─ ui.card 最近 7 天发送: ui.echart 柱状图（按日期，post/reply 堆叠，多账号时按账号分色）
└─ ui.card 最近异常: ui.table(columns=[时间,来源,中文说明], rows=action_log 最近 10 条失败)
   └─ 无异常时 ui.label('暂无异常 ✨')
```

### 8.2 审核队列（`ui/queue.py`，路由 `/queue`）

```
render_queue()
├─ ui.row 筛选栏: ui.select(账号) ui.select(类型 post/reply) ui.select(状态,默认 pending)
│  └─ ui.button('批量跳过所选') （勾选模式）
├─ ui.column 卡片列表（默认 status=pending, created_at 升序；ui.timer(5s) 增量刷新）
│  └─ queue_card(item)  ← ui/components.py，每条一张 ui.card
│     ├─ ui.row 头部: 账号徽标 | 类型徽标 | is_auto_translated→黄色徽标'自动翻译，请重点检查'
│     │   | final_text 含 URL→灰徽标'含链接（计费约$0.20）' | 过期倒计时 ui.label
│     ├─ (reply 类) ui.card.tight 目标推文区:
│     │   ├─ ui.label @author (ui.link '打开推文' 新标签)
│     │   ├─ ui.label 原文 (原语言)
│     │   └─ ui.label 中文翻译 (灰色；空则'翻译失败')
│     ├─ ui.expansion 'AI 理由 (置信度 0.85)': ui.label(llm_reason)
│     ├─ ui.textarea(final_text, 可编辑) + 字数 ui.label(weighted_len/280, 超长红色+禁批准)
│     ├─ ui.row 媒体预览: ui.image / 视频 ui.icon+文件名
│     └─ ui.row 按钮: [批准](主色) [跳过] [跳过并拉黑作者](红,confirm_dialog)
│        └─ [手动模式] → 复制文案到剪贴板+打开推文链接+出现[标记已发]按钮(FR-6.7)
└─ 空状态: ui.label('队列已清空 🎉')
```

按钮语义：「批准」= 保存 textarea 当前文本到 final_text + status→approved + decided_at（即「编辑后批准”与「批准」同一按钮）。「标记已发」= status→sent(sent_tweet_id=NULL) + 写 interactions。

### 8.3 素材库（`ui/materials.py`，路由 `/materials`）

```
render_materials()
├─ ui.row 工具栏: [新建素材] [AI 撰写] | 筛选 ui.select(类型/语言/状态) ui.input(标签)
├─ ui.column 翻译组列表（按 translation_group_id 分组，组内语言并排 FR-2.2）
│  └─ group_row(group)
│     └─ ui.row（横向滚动）
│        └─ material_card(m) ×组内数量
│           ├─ 徽标行: lang | kind | status | created_by='ai'→'AI'徽标 | 使用次数
│           ├─ ui.label(text, 截断 3 行, 点击展开)
│           ├─ 媒体缩略图 ui.row
│           └─ 按钮: [编辑] [启用/归档] [翻译▾](目标语言 ui.menu → 调 Translator, FR-2.3)
├─ dialog_edit(m)  编辑/新建弹窗:
│  ├─ ui.select(kind) ui.select(lang) ui.textarea(text)+字数 ui.input(scenario_tags)
│  ├─ ui.upload(媒体, 校验 jpg/png/webp≤5MB / mp4≤512MB, FR-2.5) + alt 文本输入
│  └─ [存为草稿] [保存并启用]
└─ dialog_ai_write  AI 撰写弹窗（FR-2.4）:
   ├─ ui.input(主题) ui.textarea(要点) ui.input(风格) ui.select(语言,多选) ui.number(N)
   ├─ [生成] → ui.spinner → 候选列表（每条 ui.textarea 可改 + over_length 红标）
   └─ 每条 [入库(草稿)] ；多语言勾选「归入同一翻译组」
```

### 8.4 监控推主（`ui/watched.py`，路由 `/watched`）

```
render_watched()
├─ ui.row: ui.input('@handle') [添加] ← 调 get_user_by_handle 解析 x_user_id，
│    失败 ui.notify('找不到该用户/网络错误'，中文)
└─ ui.table(rows=watched_users)
   columns: handle | 备注(行内编辑) | 含回复 ui.switch | 启用 ui.switch
          | 最近命中数 | 最后拉取游标时间 | [删除](confirm)
```

### 8.5 搜索规则（`ui/search_rules.py`，路由 `/rules`）

```
render_rules()
├─ [新建规则]
├─ rule_card(r) ×N（ui.card）
│  ├─ 名称 + 启用 ui.switch + 上次运行时间
│  ├─ ui.input(keyword_query, 等宽字体) 附说明链接「X 搜索语法」
│  ├─ ui.textarea(semantic_criteria)
│  ├─ ui.row: ui.select(lang) ui.number(max_results 10-100) ui.slider(min_llm_score 0-10)
│  └─ [保存] [试运行] [删除]
└─ dialog_dry_run: [试运行] → SearchJob.run_rule(dry_run=True)
   ├─ ui.spinner('拉取并打分中…（会消耗读额度）')
   └─ ui.table: 推文文本(截断) | score | reason | 达标✓(score>=阈值 绿色)
```

### 8.6 定时计划（`ui/schedule.py`，路由 `/schedule`）

```
render_schedule()
├─ [新建计划]
├─ ui.table(rows=scheduled_posts)
│  columns: 账号 | 素材摘要 | 类型/表达式 | 下次执行(本地时间) | auto_approve ui.switch
│         | 状态(missed→红色徽标'已错过，已顺延') | [暂停/恢复] [删除]
└─ dialog_edit_schedule:
   ├─ ui.select(账号) ui.select(素材, 仅 kind='post' 且 active)
   ├─ ui.select(schedule_type) + 按类型切换的表达式输入
   │    once: ui.date+ui.time | daily: ui.time | weekly: 星期多选+ui.time | cron: ui.input
   ├─ 保存时 compute_next_run 校验，非法 ui.notify(ValueError 中文消息)
   └─ ui.switch(auto_approve) 附风险说明'开启后到点自动发出，不经人工审核'
```

### 8.7 设置（`ui/settings_page.py`，路由 `/settings`）

```
render_settings()  —— ui.tabs: 账号 | LLM | 合规参数 | 预算 | 黑名单
├─ tab 账号
│  ├─ account_card(a) ×N: handle | access_type 徽标 | is_primary 星标
│  │   | status(auth_error→红色+'凭据已失效，请更新'）| 限速参数摘要
│  │   | [编辑] [测试连接]→get_me→ui.notify('连接成功：@handle'/中文错误)
│  │   | [暂停/启用]
│  └─ dialog_account:
│     ├─ ui.input(handle) ui.switch(is_primary) ui.radio(access_type)
│     │    联动校验: is_primary=True 时 unofficial 选项禁用+提示（FR-1.3）
│     ├─ official: 4 个 ui.input(password 型, 已存值显示'●●●已配置'不回显 NFR-2)
│     │    + ui.expansion'如何申请 API 凭据'(引导文)
│     ├─ unofficial: 说明'请在命令行运行 scripts/login_helper.py 生成 cookies'
│     │    + cookies 文件存在性检测 ✓/✗ + 风险提示（NFR-1 保守限速说明）
│     └─ 限速区: ui.number(daily_post/reply_limit) ui.number(min/max_interval)
│        ui.time(active_hours) ui.select(timezone)；低于风险阈值改动出黄色警示
├─ tab LLM: ui.input(base_url) ui.input(api_key, password) [测试连接](LLMClient.ping)
│  └─ 分场景模型名 ui.input ×(light/strong + 可选场景覆写)
├─ tab 合规参数: cooldown_days | grace_period_hours | reply_ttl_hours
│  | tweet_max_age_hours | nurture_days | match_confidence_threshold（全部 ui.number+说明）
├─ tab 预算: ui.radio(billing_mode) | monthly_read_quota / monthly_budget_usd
│  | daily_read_budget(显示自动值，可手动覆盖) | budget_reserve_reads
│  | monitor_interval_minutes | search_runs_per_day
└─ tab 黑名单: ui.input('@handle 或 user_id') [添加] + ui.table(handle|原因|加入时间|[移除])
```

---

## 9. 边界情况清单（每流程 ≥5 项）

### 9.1 定时发布（FR-3）

| # | 情况 | 处理方式 |
|---|---|---|
| A1 | 计划引用的素材被归档/删除 | scheduled_check 生成条目前校验素材 status='active'；不满足则计划置 paused，UI 提示「素材已不可用」 |
| A2 | 程序停了 3 天，daily 计划积压多次 | 补扫只按「当前 next_run_at 是否在宽限期内」补**一次**，绝不逐次补发；其余按 missed 顺延（coalesce 语义） |
| A3 | 电脑休眠唤醒，job 大面积 misfire | APScheduler `misfire_grace_time=60` + `coalesce=True`：合并为一次执行；超过宽限的由补扫逻辑统一处理 |
| A4 | once 计划的时间填了过去时刻 | 保存时 compute_next_run 返回 None → UI 拒绝保存并提示 |
| A5 | 同一素材被两个计划同时到点引用 | 允许（不同账号属正常）；同账号则第二条发送时可能触发 X 重复内容 → DuplicateContent → failed，属预期护栏 |
| A6 | auto_approve 计划到点但账号 auth_error | 条目正常生成为 approved；dispatcher 的软条件（ACCOUNT_NOT_ACTIVE）使其滞留，账号恢复后自动发出 |
| A7 | DST/时区切换导致 next_run_at 跳变 | compute_next_run 全部经账号时区 ZoneInfo 计算再转 UTC，跳过不存在的本地时刻（顺延到下一有效时刻） |

### 9.2 监控轮询（FR-4）

| # | 情况 | 处理方式 |
|---|---|---|
| B1 | 推主改名/注销/被封 | get_user_tweets 抛 TargetNotFound → 该监控项 enabled=0，UI 黄色提示「用户不可用」；不影响其余推主 |
| B2 | 推主转为受保护（锁推） | PermissionDenied → 同 B1 处理（提示语区分） |
| B3 | 单次新推超过 max_results=5 | since_id 拉取按新→旧返回，多出的旧推下轮由游标补上（不丢，只延迟）；游标推进到本批 newest_id |
| B4 | 游标推文被作者删除 | X API since_id 语义不受删除影响（按 id 比较），无需处理；twifork 客户端过滤同理 |
| B5 | 新推是对我们自有账号的回复 | precheck 'own_account' 分支同时检查 author 与 in_reply_to 是否自有账号，过滤存档（避免自我对话循环） |
| B6 | 同一推文被监控和搜索同时抓到 | target_tweets.tweet_id UNIQUE，后到者 INSERT 冲突 → 忽略（`INSERT OR IGNORE` / 捕获 IntegrityError） |
| B7 | 中途预算耗尽 | 每个推主处理前检查 can_spend(5)；不足则终止本轮，已处理部分保留，UI 显示熔断状态 |
| B8 | 首次添加推主后立刻轮询 | last_seen_tweet_id 为空 → 只取最新 1 条设游标、process 不处理（v1.0 §8.1 不回溯轰炸） |

### 9.3 语义搜索（FR-5）

| # | 情况 | 处理方式 |
|---|---|---|
| C1 | keyword_query 语法非法 | 官方返回 400 → PermissionDenied（消息含「查询语法错误」）→ 规则 enabled=0 + UI 提示；试运行按钮可直接暴露此错误 |
| C2 | 粗筛 0 条命中 | 正常收尾：不调 LLM（省成本），newest_id 为 None 不动游标，last_run_at 照更新 |
| C3 | LLM 打分返回的 tweet_id 有遗漏/多余 | 遗漏的按 score=0 处理（宁低勿高）；多余的丢弃；两种情况都记 warning 日志 |
| C4 | LLM 打分格式错误（重试后仍坏） | 本批候选全部按 no_match 存档（process_status='filtered'，reason='LLM格式错误'），游标照常推进（避免死循环重扫同一批） |
| C5 | 候选里包含自有账号/黑名单/已回复的推文 | 本地过滤在 LLM 打分**之前**执行（省 LLM 成本），过滤者存档 filtered |
| C6 | 规则 7 天未运行，start_time 早于窗口 | start_time=max(last_run_at, now-7d)（v1.0 §8.2）已覆盖；再钳制到 now-7d+60s 防 API 400 |
| C7 | 达标推文很多（一次 15 条全过阈值） | 全部进 MatchEngine/队列；发送侧由日上限与间隔自然限流，队列积压由人审消化（设计如此，不加额外限制） |

### 9.4 匹配引擎（FR-6）

| # | 情况 | 处理方式 |
|---|---|---|
| D1 | 目标推文 lang 缺失且 LLM 判定失败 | detect_lang 返回 'und' → 按 no_match 存档（reason='语言无法判定'） |
| D2 | LLM 返回的 choice 不在候选 id 列表内 | 视同格式错误：走一次纠错重试，仍错按 no_match |
| D3 | adapted_text 超 280 加权长度 | 不自动截断（可能截坏链接）：照常入队但 UI 字数标红禁批准，需人工改短；llm_reason 附注「超长」 |
| D4 | adapted_text 丢失素材中的 URL | 入队前校验：素材含 URL 而 adapted_text 无 → 自动在结尾补「 → URL」；仍超长则按 D3 |
| D5 | 现场翻译候选（FR-6.2）翻译失败 | Translator 抛异常 → 按 no_match 存档（reason='自动翻译失败'），不入队 |
| D6 | 推文中译失败 | tweet_to_zh 返回 ""，照常入队，审核卡片显示「翻译失败」（不因辅助信息阻断主流程） |
| D7 | 匹配进行中推文作者被拉黑 | 允许入队（黑名单在发送前由 ComplianceGuard 再拦，最终闸口兜底） |

### 9.5 发送分发（FR-7）

| # | 情况 | 处理方式 |
|---|---|---|
| E1 | 发送成功但进程在写库前崩溃 | 无法完全避免（X 无幂等键）。缓解：置 sending 持久化在发送前；重启补扫把残留 sending 回置 approved，再发时若 X 判重复→DuplicateContent→failed（不会二次发出相同内容）；人工核对 failed 原因 |
| E2 | 两个自有账号的条目指向同一目标推文 | 先发者写入 interactions；后发者被 guard ALREADY_REPLIED 拦截置 skipped（唯一索引兜底竞态） |
| E3 | 目标推文在批准后被删除 | reply 抛 TargetNotFound → failed，error_msg「目标推文已删除」，不重试 |
| E4 | 媒体文件在磁盘上被删 | upload_media 前检查文件存在，缺失 → MediaError → failed「媒体文件缺失」 |
| E5 | 4 图中第 3 张上传失败 | 整条中止（不发不完整媒体组）→ failed；已上传的 media_id 弃用（X 侧自动过期） |
| E6 | 活跃时段只剩 1 分钟时取到条目 | guard 在发送瞬间校验一次即可；边界溢出几十秒可接受，不做发送中撤回 |
| E7 | 重试期间账号被用户手动 paused | 每次重试前重读账号状态，非 active 则条目回置 approved 停止重试 |
| E8 | X 返回成功但响应解析失败 | 视为成功但 sent_tweet_id=NULL，记 warning；宁可少记 id 不可重发 |
| E9 | random 间隔生成后账号参数被改小 | next_allowed_at 已固化不回改；下一条用新参数（简单可预期） |

### 9.6 审核队列 UI（FR-6.4~6.7）

| # | 情况 | 处理方式 |
|---|---|---|
| F1 | 用户点批准的同时条目刚过期 | 状态更新用乐观锁 `UPDATE ... WHERE status='pending'`；失败则 ui.notify「该条目已过期/状态已变化」并刷新 |
| F2 | 编辑后的文案超 280 加权长度 | 字数实时显示，超长「批准」按钮禁用 |
| F3 | 用户清空文案后批准 | 空文本/纯空白禁止批准（按钮禁用） |
| F4 | 「跳过并拉黑」的作者已在黑名单 | 幂等：blacklist UNIQUE(x_user_id)，重复添加忽略，条目照常 skipped |
| F5 | 手动模式点了「标记已发」但其实没发 | 无法校验，以用户操作为准写入 interactions（去重账本从严：宁可多记防重复打扰） |
| F6 | 两个浏览器标签同时操作同一条目 | 同 F1 乐观锁；后操作者收到状态冲突提示 |

### 9.7 翻译 / AI 撰写（FR-2.3~2.4）

| # | 情况 | 处理方式 |
|---|---|---|
| G1 | 一键翻译的目标语言版本已存在于翻译组 | 弹确认「该语言已有版本，将新建一条并列草稿」；不覆盖旧版 |
| G2 | 翻译结果超 280 加权长度 | 存为草稿并标红，需人工压缩后才能启用 |
| G3 | 原文全是 URL/@/# 无可译文本 | 翻译结果≈原文，照常入库（无害）；UI 不特殊处理 |
| G4 | AI 撰写返回条数与请求的 语言×N 不符 | 有多少展示多少，缺失时 ui.notify「模型返回条数不足」；多余的丢弃 |
| G5 | AI 撰写返回了未请求的语言 | 丢弃该条，记 warning |
| G6 | 翻译组内源素材被归档 | 组内其他语言版本不受影响；FR-6.2 现场翻译只从 status='active' 的组员里挑源 |

### 9.8 账号与凭据（FR-1）

| # | 情况 | 处理方式 |
|---|---|---|
| H1 | 把已有 unofficial 账号勾选为主号 | UI 禁用该操作并提示；DB CHECK 约束兜底（写入直接报错） |
| H2 | secrets.toml 缺少 credential_ref 对应节 | factory 抛 CredentialMissing → 测试连接/发送时中文提示「凭据未配置」；账号不自动置 auth_error（配置问题≠凭据失效） |
| H3 | cookies 文件被手动删除 | UnofficialXClient 首调用抛 AuthExpired → 账号 auth_error + UI 提示重新运行 login_helper |
| H4 | 发送中途 AuthExpired | 条目回置 approved（非条目问题），账号置 auth_error；账号恢复后条目自动继续 |
| H5 | 删除账号但其队列/interactions 有历史 | 不物理删除：账号只有 paused（外键引用保持完整）；UI「停用」语义 |
| H6 | min_interval > max_interval 的输入 | UI 校验拒绝；DB CHECK (max>=min) 兜底 |

### 9.9 预算与熔断（FR-8.3）

| # | 情况 | 处理方式 |
|---|---|---|
| I1 | 单次拉取跨越预算线（剩 3 条拉回 5 条） | 允许本次完成并记满 5，之后 can_spend 变 False 熔断（预算是软限制，保留水位吸收超支） |
| I2 | 用户月中改月度额度/预算 | recompute_daily 在下次启动或设置保存时重算；当日已消耗不追溯 |
| I3 | 跨月边界（月末最后一天→1 号） | 月用量按 created_at 的 UTC 月聚合，自然翻新；启动补扫重算日预算 |
| I4 | action_log 被清理导致月用量低估 | v1 不自动清理 action_log（单用户量小）；文档注明勿手动删 |
| I5 | 熔断后用户手动调大日预算 | 设置保存即时生效，can_spend 恢复 True，下轮 job 自动恢复（无需重启） |

### 9.10 启动补扫（v1.0 §8.5）

| # | 情况 | 处理方式 |
|---|---|---|
| J1 | 备份时 db 正在写（WAL） | 用 sqlite3 backup API（`Connection.backup`）而非复制文件，保证一致性；失败仅告警 |
| J2 | backup 目录超 14 份 | 按文件名时间排序删最旧；删除失败不阻断启动 |
| J3 | 双击启动了第二个实例 | 启动时抢占 `data/app.lock`（文件锁）；抢占失败弹「程序已在运行」并退出（防 job 双跑/端口冲突） |
| J4 | 上次异常退出留下 sending 条目 | 回置 approved（§7.6）；配合 E1 的 DuplicateContent 兜底 |
| J5 | 系统时钟被回拨 | next_run_at/next_allowed_at 比较全用 UTC；回拨只会推迟动作（安全方向），不特殊处理 |
| J6 | 数据库文件损坏 | 启动失败时中文提示「数据库损坏，可从 data/backup/ 恢复最近备份」，附恢复步骤说明 |

---

## 10. 附录

### 10.1 `config/settings.toml` 示例（非敏感，入库前的初始默认）

```toml
[app]
port = 8080          # NiceGUI 端口
language = "zh-CN"

[data]
dir = "data"         # 相对工程根

# 其余运行参数首启动时从本文件种子写入 app_settings 表，之后以表为准（UI 可改）
[defaults]
cooldown_days = 7
grace_period_hours = 2
reply_ttl_hours = 48
billing_mode = "payg"
monthly_budget_usd = 60
monitor_interval_minutes = 50
search_runs_per_day = 2
match_confidence_threshold = 0.7
```

### 10.2 启动脚本行为（start.bat 为主，start.sh 对齐）

1. 检测 `uv` 不存在 → 提示安装命令（winget/官方脚本）并暂停
2. `uv sync`（首次自动建 venv 装依赖）
3. `uv run python -m x_operator.main`
4. main 内：初始化/迁移 db → 启动补扫 → 启动 scheduler → `ui.run(port, show=True)` 自动开浏览器
5. 崩溃退出时 `pause`（Windows）保留窗口可见错误

### 10.3 `.gitignore` 必含

```
config/secrets.toml
data/
*.pyc
.venv/
```

### 10.4 v1.0 → v1.1 差异摘要

| 主题 | v1.0 | v1.1 细化 |
|---|---|---|
| X 计费 | 待核实 | 已核实（§0.1），Budget 双模式 quota/payg，含链接推文提示 |
| twifork | 待核实 | 已核实可用（2.3.5，import twikit），async 同步封装方案（§3.4） |
| 接口 | 模块清单 | 类/方法签名/异常语义（§3、§7） |
| 数据模型 | 字段清单 | 完整 DDL + 索引 + CHECK + 部分唯一索引（§4） |
| API 调用 | 适配器方法名 | tweepy/twifork 逐方法映射 + 异常映射 + 限流策略（§5） |
| Prompt | 场景表 | 5+1 场景 system/user 全文 + JSON Schema + 日语 few-shot（§6） |
| UI | 页面要素表 | NiceGUI 组件层级线框（§8） |
| 边界情况 | 零散提及 | 10 个流程 × ≥5 条清单（§9） |

