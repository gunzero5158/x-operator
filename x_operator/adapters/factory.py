"""适配器工厂（design-v1.1 §3.5）。

- dry_run（Mock 演示模式）为真：所有账号一律返回 MockXClient，零凭据零风险跑通全流程。
- 关掉 dry_run：按 account.access_type 返回真实适配器，凭据来自 accounts.credentials（JSON）。
- get_real_client()：无视 dry_run，强制拿真实适配器——给「测试连接」用，用户不必先关演示模式
  就能验证凭据对不对。

主号禁 unofficial 的三重保险之一在此：is_primary 且 unofficial 直接 ValueError。
"""
from __future__ import annotations

import json
import sqlite3

from .. import config
from ..db.database import get_conn
from .base import XClient
from .mock import MockXClient
from .real import (OfficialXClient, UnofficialXClient, credentials_ready,
                   parse_credentials)

_cache: dict[int, XClient] = {}


def _row_get(account: sqlite3.Row, key: str, default=None):
    try:
        return account[key]
    except (IndexError, KeyError):
        return default


def account_credentials(account: sqlite3.Row) -> dict:
    return parse_credentials(_row_get(account, "credentials", "{}"))


def credential_status(account: sqlite3.Row) -> tuple[bool, str]:
    """(凭据是否齐全, 中文说明) —— 账号卡片展示用。"""
    return credentials_ready(account["access_type"], account_credentials(account))


def _persist_cookies(account_id: int):
    def _cb(cookies: dict) -> None:
        with get_conn() as conn:
            row = conn.execute("SELECT credentials FROM accounts WHERE id=?", (account_id,)).fetchone()
            creds = parse_credentials(row["credentials"] if row else "{}")
            creds.update(cookies)
            conn.execute("UPDATE accounts SET credentials=? WHERE id=?",
                         (json.dumps(creds, ensure_ascii=False), account_id))
            conn.commit()
    return _cb


def get_real_client(account: sqlite3.Row) -> XClient:
    """强制真实适配器（不看 dry_run，不走缓存）。凭据不全时抛 CredentialMissing。"""
    if account["is_primary"] and account["access_type"] == "unofficial":
        raise ValueError("主号不允许使用非官方（twifork）通道——封号风险过高（FR-1.3）")
    creds = account_credentials(account)
    if account["access_type"] == "official":
        return OfficialXClient(credentials=creds)
    return UnofficialXClient(credentials=creds, on_cookies_refreshed=_persist_cookies(account["id"]))


def get_client(account: sqlite3.Row) -> XClient:
    aid = account["id"]
    if aid in _cache:
        return _cache[aid]

    if account["is_primary"] and account["access_type"] == "unofficial":
        raise ValueError("主号不允许使用非官方（twifork）通道——封号风险过高（FR-1.3）")

    if config.get_bool("dry_run", True):
        client: XClient = MockXClient(handle=account["handle"])
    else:
        client = get_real_client(account)

    _cache[aid] = client
    return client


def invalidate(account_id: int | None = None) -> None:
    """凭据更新/停用/切换 dry_run 时清缓存。account_id=None 清全部。"""
    if account_id is None:
        _cache.clear()
    else:
        _cache.pop(account_id, None)
