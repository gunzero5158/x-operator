"""适配器工厂（design-v1.1 §3.5）。

MVP：全局 dry_run 开关（app_settings.dry_run）为真时，所有账号一律返回 MockXClient，
零凭据零风险跑通全流程。关掉 dry_run 后按 account.access_type 返回真实适配器
（当前为占位，接了凭据即生效）。

主号禁 unofficial 的三重保险之一在此：is_primary 且 unofficial 直接 ValueError。
"""
from __future__ import annotations

import sqlite3

from .. import config
from .base import XClient
from .mock import MockXClient
from .real import OfficialXClient, UnofficialXClient

_cache: dict[int, XClient] = {}


def get_client(account: sqlite3.Row) -> XClient:
    aid = account["id"]
    if aid in _cache:
        return _cache[aid]

    if account["is_primary"] and account["access_type"] == "unofficial":
        raise ValueError("主号不允许使用非官方（twifork）通道——封号风险过高（FR-1.3）")

    if config.get_bool("dry_run", True):
        client: XClient = MockXClient(handle=account["handle"])
    elif account["access_type"] == "official":
        client = OfficialXClient(credential_ref=account["credential_ref"])
    else:
        client = UnofficialXClient(credential_ref=account["credential_ref"])

    _cache[aid] = client
    return client


def invalidate(account_id: int | None = None) -> None:
    """凭据更新/停用/切换 dry_run 时清缓存。account_id=None 清全部。"""
    if account_id is None:
        _cache.clear()
    else:
        _cache.pop(account_id, None)
