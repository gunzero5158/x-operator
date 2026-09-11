"""SQLite 连接管理与迁移。

- 每次取连接执行 spec 要求的 PRAGMA（WAL / 外键 / busy_timeout）。
- row_factory=sqlite3.Row，查询结果可按列名访问。
- 首次建库后写入 schema_version 并种子化 app_settings 默认值；旧库自动升级（v3 起清除演示数据）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .schema import DDL, SCHEDULED_POSTS_TABLE, SCHEMA_VERSION
from . import seed

_DB_PATH: Path | None = None
_local = threading.local()


def init_db(db_path: str | Path, setting_overrides: dict | None = None) -> None:
    """设置全局库路径并执行迁移 + 种子。幂等。

    setting_overrides：config/settings.toml [defaults] 里的值，只在键还不存在时写入（首启动生效，之后以设置页为准）。"""
    global _DB_PATH
    _DB_PATH = Path(db_path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(DDL)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            _migrate(conn, int(row["version"]))
        seed.seed_settings(conn, setting_overrides)
        conn.commit()


# (表, 列, 建列 SQL 片段) —— 旧库增量补列；ADD COLUMN 幂等靠 PRAGMA table_info 判断
_ADDED_COLUMNS = [
    ("accounts", "credentials", "TEXT NOT NULL DEFAULT '{}'"),
    ("materials", "deleted_at", "TEXT"),
    # v5：时间窗 / 回复方式下放到每条规则、每个推主；队列条目记录来源与发送后核实结果
    ("search_rules", "lookback_hours", "INTEGER NOT NULL DEFAULT 24"),
    ("search_rules", "reply_mode", "TEXT NOT NULL DEFAULT 'material'"),
    ("search_rules", "ai_brief", "TEXT NOT NULL DEFAULT ''"),
    ("search_rules", "allow_polish", "INTEGER NOT NULL DEFAULT 0"),
    ("watched_users", "lookback_hours", "INTEGER NOT NULL DEFAULT 24"),
    ("watched_users", "reply_mode", "TEXT NOT NULL DEFAULT 'material'"),
    ("watched_users", "ai_brief", "TEXT NOT NULL DEFAULT ''"),
    ("watched_users", "allow_polish", "INTEGER NOT NULL DEFAULT 0"),
    ("review_queue", "origin", "TEXT NOT NULL DEFAULT 'ai_match'"),
    ("review_queue", "verify_status", "TEXT"),
    # v6：搜索规则观看量门槛；抓取记录保存观看量
    ("search_rules", "min_views", "INTEGER NOT NULL DEFAULT 0"),
    ("target_tweets", "view_count", "INTEGER"),
    # v8：每条规则/推主可指定回复账号（NULL = 自动轮流）
    ("search_rules", "reply_account_id", "INTEGER"),
    ("watched_users", "reply_account_id", "INTEGER"),
    # v10：规则来源可选关键词搜索 / 某账号的推荐流 / 关注流
    ("search_rules", "source_kind", "TEXT NOT NULL DEFAULT 'search'"),
    ("search_rules", "feed_account_id", "INTEGER"),
    # v11：附件（配图 / 视频）——存本地相对路径的 JSON 列表，发送时再上传
    ("materials", "media_files", "TEXT NOT NULL DEFAULT '[]'"),
    ("review_queue", "final_media_files", "TEXT NOT NULL DEFAULT '[]'"),
    ("scheduled_posts", "media_files", "TEXT NOT NULL DEFAULT '[]'"),
    # v13：账号是否订阅 X Premium（会员不限推文长度；免费账号 280 单位 ≈ 140 个汉字）
    ("accounts", "is_premium", "INTEGER NOT NULL DEFAULT 0"),
    # v14：定时发帖（AI 按主题创作）的附件方式——fixed=每次都带这几个；pool=从素材池里每次随机挑一个
    ("scheduled_posts", "media_mode", "TEXT NOT NULL DEFAULT 'fixed'"),
    # v15：任务队列条目人工放行——从「已跳过」强制放回待审核，发送时不再按冷却 / 黑名单 / 时效拦
    ("review_queue", "force_send", "INTEGER NOT NULL DEFAULT 0"),
    # v16：抓取记录保存附件元信息（图片直链 / 视频封面，JSON 列表）；规则可选把它们一并送给打分模型
    ("target_tweets", "media", "TEXT NOT NULL DEFAULT '[]'"),
    ("search_rules", "read_media", "INTEGER NOT NULL DEFAULT 0"),
    # v17：读取账号池——撞 429 的账号暂停到什么时候（UTC ISO），期间不被挑去抓取
    ("accounts", "read_paused_until", "TEXT"),
    # v18：主贴和回复各自一个发送冷却——next_allowed_at 只管回复，主贴看这个
    ("accounts", "next_allowed_post_at", "TEXT"),
    # v19：免审核改为每条搜索规则 / 每个监控推主各自设置（开关 + 置信度阈值）
    ("search_rules", "auto_approve", "INTEGER NOT NULL DEFAULT 0"),
    ("search_rules", "auto_approve_min_confidence", "REAL NOT NULL DEFAULT 0.7"),
    ("watched_users", "auto_approve", "INTEGER NOT NULL DEFAULT 0"),
    ("watched_users", "auto_approve_min_confidence", "REAL NOT NULL DEFAULT 0.7"),
    # v20：规则 / 推主的「AI 按要求创作」可挂配图 / 视频——固定几个每次都带，或素材池每次随机挑 1 个
    ("search_rules", "media_files", "TEXT NOT NULL DEFAULT '[]'"),
    ("search_rules", "media_mode", "TEXT NOT NULL DEFAULT 'fixed'"),
    ("watched_users", "media_files", "TEXT NOT NULL DEFAULT '[]'"),
    ("watched_users", "media_mode", "TEXT NOT NULL DEFAULT 'fixed'"),
    # v21：搜索规则观看量门槛可设上限，和下限组成区间（0 = 不限）
    ("search_rules", "max_views", "INTEGER NOT NULL DEFAULT 0"),
    # v22：回复账号可选「自动轮流 / 只用指定的几个（1 个 = 固定）/ 自动轮流但排除几个」，账号 id 存 JSON 列表
    ("search_rules", "reply_account_mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("search_rules", "reply_account_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("watched_users", "reply_account_mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("watched_users", "reply_account_ids", "TEXT NOT NULL DEFAULT '[]'"),
]


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    """把旧版本库升级到 SCHEMA_VERSION。加列不改约束；v3 起清除旧版本写入的演示数据。"""
    for table, col, ddl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    if current < 3:
        removed = seed.purge_demo_data(conn)
        if removed:
            logging.getLogger("x_operator.db").info("已清除旧版演示数据：%s", removed)
    if current < 4:
        changed = seed.loosen_filters(conn)
        if changed:
            logging.getLogger("x_operator.db").info("已放宽旧默认过滤阈值：%s", changed)
    if current < 7:
        changed = seed.loosen_match(conn)
        if changed:
            logging.getLogger("x_operator.db").info("已放宽旧默认素材匹配门槛：%s", changed)
    if current < 9:
        _rebuild_scheduled_posts(conn)
    if current < 12:
        _rebuild_scheduled_posts(conn, reason="v12：计划类型新增 interval（每隔 N 小时）")
    if current < 22:
        changed = _split_zh_langs(conn)
        changed += _reply_account_modes(conn)
        if changed:
            logging.getLogger("x_operator.db").info("v22 迁移：%s", "；".join(changed))
    if current != SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))


def _split_zh_langs(conn: sqlite3.Connection) -> list[str]:
    """v22：语言「中文」拆成简体 zh-Hans / 繁体 zh-Hant。
    素材、抓取记录按正文字形判断（素材看不出来按简体，抓取记录看不出来保留 zh = 简繁未定）；
    搜索规则里选过「中文」的改成简繁都选（行为不变）；定时发帖的语言按发帖素材里多的那种。"""
    from ..core.langdetect import ZH_HANS, normalize, refine_tweet_lang, rule_langs_normalized, zh_script
    out: list[str] = []
    n = 0
    for r in conn.execute("SELECT id, text, lang FROM materials").fetchall():
        if normalize(r["lang"]) in ("zh", "zh-Hans", "zh-Hant") and r["lang"] not in ("zh-Hans", "zh-Hant"):
            new = zh_script(r["text"]) if normalize(r["lang"]) == "zh" else normalize(r["lang"])
            conn.execute("UPDATE materials SET lang=? WHERE id=?", (new or ZH_HANS, r["id"])); n += 1
    if n:
        out.append(f"{n} 条中文素材分成了简体 / 繁体")
    n = 0
    for r in conn.execute("SELECT id, text, lang FROM target_tweets WHERE lang IS NOT NULL AND lower(lang) LIKE 'zh%'").fetchall():
        new = refine_tweet_lang(r["lang"], r["text"] or "")
        if new != r["lang"]:
            conn.execute("UPDATE target_tweets SET lang=? WHERE id=?", (new, r["id"])); n += 1
    if n:
        out.append(f"{n} 条中文抓取记录标出了简繁")
    n = 0
    for r in conn.execute("SELECT id, lang FROM search_rules").fetchall():
        old = [x.strip() for x in (r["lang"] or "").split(",") if x.strip()]
        new = rule_langs_normalized(old)
        if new != old:
            conn.execute("UPDATE search_rules SET lang=? WHERE id=?", (",".join(new), r["id"])); n += 1
    if n:
        out.append(f"{n} 条搜索规则的「中文」改成简体 + 繁体")
    rows = conn.execute("SELECT id FROM scheduled_posts WHERE lower(pool_lang) LIKE 'zh%' AND pool_lang NOT IN ('zh-Hans','zh-Hant')").fetchall()
    if rows:
        cnt = {r["lang"]: r["c"] for r in conn.execute(
            "SELECT lang, COUNT(*) c FROM materials WHERE kind='post' AND lang IN ('zh-Hans','zh-Hant') GROUP BY lang")}
        pick = "zh-Hant" if cnt.get("zh-Hant", 0) > cnt.get("zh-Hans", 0) else "zh-Hans"
        conn.execute("UPDATE scheduled_posts SET pool_lang=? WHERE lower(pool_lang) LIKE 'zh%' AND pool_lang NOT IN ('zh-Hans','zh-Hant')", (pick,))
        out.append(f"{len(rows)} 个定时发帖计划的语言「中文」改成{'繁体' if pick == 'zh-Hant' else '简体'}")
    return out


def _reply_account_modes(conn: sqlite3.Connection) -> list[str]:
    """v22：原来指定了回复账号的规则 / 推主 → 「只用指定的账号」、名单里就这一个（行为不变）。"""
    out = []
    for table, what in (("search_rules", "搜索规则"), ("watched_users", "监控推主")):
        rows = conn.execute(f"SELECT id, reply_account_id FROM {table} WHERE reply_account_id IS NOT NULL AND reply_account_id>0").fetchall()
        for r in rows:
            conn.execute(f"UPDATE {table} SET reply_account_mode='include', reply_account_ids=?, reply_account_id=NULL WHERE id=?",
                         (f"[{int(r['reply_account_id'])}]", r["id"]))
        if rows:
            out.append(f"{len(rows)} 个{what}的指定回复账号改成新的账号名单")
    return out


def _rebuild_scheduled_posts(conn: sqlite3.Connection, reason: str = "v9：内容来源可选素材池 / AI 主题") -> None:
    """按当前 DDL 重建 scheduled_posts（v9 material_id 改可空 + 内容来源字段；v12 计划类型加 interval）。
    SQLite 不能改列约束，只能建新表、把两边都有的列搬过去。
    review_queue 里有指向 scheduled_posts(id) 的外键：关掉外键检查、用 legacy 改名方式，别的表里的引用文本不会被改写。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scheduled_posts)").fetchall()}
    ddl = (conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='scheduled_posts'").fetchone() or {"sql": ""})["sql"] or ""
    if "content_mode" in cols and "'interval'" in ddl:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(SCHEDULED_POSTS_TABLE.replace("IF NOT EXISTS scheduled_posts", "scheduled_posts_new"))
        new_cols = [r["name"] for r in conn.execute("PRAGMA table_info(scheduled_posts_new)").fetchall()]
        keep = [c for c in new_cols if c in cols]
        cl = ", ".join(keep)
        conn.execute(f"INSERT INTO scheduled_posts_new ({cl}) SELECT {cl} FROM scheduled_posts")
        conn.execute("DROP TABLE scheduled_posts")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE scheduled_posts_new RENAME TO scheduled_posts")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_sched_due ON scheduled_posts(status, next_run_at)")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    logging.getLogger("x_operator.db").info("已重建 scheduled_posts 表（%s）", reason)


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("数据库尚未初始化：请先调用 init_db()")
    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """线程内复用连接（sqlite3 连接不可跨线程共享；APScheduler/NiceGUI 多线程环境下
    用 threading.local 各线程一条连接）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def to_iso(dt: datetime) -> str:
    """库里统一的 UTC 时间格式（秒精度、Z 结尾）。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(ISO_FMT)


def utcnow_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, ISO_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except ValueError:
            return None
