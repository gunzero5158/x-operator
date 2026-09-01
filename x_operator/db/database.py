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


def init_db(db_path: str | Path) -> None:
    """设置全局库路径并执行迁移 + 种子。幂等。"""
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
        seed.seed_settings(conn)
        conn.commit()


# (表, 列, 建列 SQL 片段) —— 旧库增量补列；ADD COLUMN 幂等靠 PRAGMA table_info 判断
_ADDED_COLUMNS = [
    ("accounts", "credentials", "TEXT NOT NULL DEFAULT '{}'"),
    ("materials", "deleted_at", "TEXT"),
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


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except ValueError:
            return None
