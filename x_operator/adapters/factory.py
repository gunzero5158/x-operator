"""适配器工厂（design-v1.1 §3.5）。

按 account.access_type 返回真实适配器，凭据来自 accounts.credentials（JSON）：
- official   → OfficialXClient（tweepy，X API v2）
- unofficial → UnofficialXClient（twifork/twikit，Cookie 或 密码+TOTP 登录）

get_client() 带缓存（同一账号复用连接/登录态），同一账号的首次创建加锁——密码登录要十几秒，
UI 线程和调度线程同时来的话不会各登一次；get_real_client() 不走缓存，给「测试连接」用。
主号禁 unofficial 的三重保险之一在此：is_primary 且 unofficial 直接 ValueError。

仅供自动化测试：环境变量 X_OPERATOR_MOCK=1 时返回 MockXClient（UI 里没有任何开关）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

from ..db.database import get_conn
from .base import XClient
from .real import (OfficialXClient, UnofficialXClient, credentials_ready,
                   parse_credentials)

_cache: dict[int, XClient] = {}
_cache_lock = threading.Lock()
_create_locks: dict[int, threading.Lock] = {}


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


def _testing_mock() -> bool:
    return os.environ.get("X_OPERATOR_MOCK") == "1"


def get_real_client(account: sqlite3.Row) -> XClient:
    """真实适配器（不走缓存）。凭据不全时抛 CredentialMissing。"""
    if account["is_primary"] and account["access_type"] == "unofficial":
        raise ValueError("主号不允许使用非官方（twifork）通道——封号风险过高（FR-1.3）")
    if _testing_mock():
        from .mock import MockXClient
        return MockXClient(handle=account["handle"])
    creds = account_credentials(account)
    if account["access_type"] == "official":
        return OfficialXClient(credentials=creds)
    return UnofficialXClient(credentials=creds, on_cookies_refreshed=_persist_cookies(account["id"]))


def get_client(account: sqlite3.Row) -> XClient:
    aid = account["id"]
    with _cache_lock:
        client = _cache.get(aid)
        if client is not None:
            return client
        lock = _create_locks.setdefault(aid, threading.Lock())
    with lock:
        with _cache_lock:
            client = _cache.get(aid)
            if client is not None:
                return client
        client = get_real_client(account)
        with _cache_lock:
            _cache[aid] = client
        return client


def invalidate(account_id: int | None = None) -> None:
    """凭据更新/停用时清缓存。account_id=None 清全部。"""
    with _cache_lock:
        if account_id is None:
            _cache.clear()
        else:
            _cache.pop(account_id, None)
