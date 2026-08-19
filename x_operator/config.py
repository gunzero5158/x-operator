"""设置读写：app_settings 表是运行时唯一真源（design-v1.1 §10.1）。

提供带类型转换的读取与写入。UI 设置页直接改这里，后台 job 每次读取即取最新值。
"""
from __future__ import annotations

from .db.database import get_conn


def get(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_int(key: str, default: int = 0) -> int:
    val = get(key)
    try:
        return int(val) if val is not None and val != "" else default
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    val = get(key)
    try:
        return float(val) if val is not None and val != "" else default
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = get(key)
    if val is None:
        return default
    return val.strip() in ("1", "true", "True", "yes", "on")


def set_value(key: str, value: str | int | float | bool) -> None:
    if isinstance(value, bool):
        value = "1" if value else "0"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def all_settings() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
