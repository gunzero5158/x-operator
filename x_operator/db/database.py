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

from .schema import DDL, SCHEMA_VERSION
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
    if current != SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))


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
