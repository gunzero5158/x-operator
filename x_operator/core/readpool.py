"""读取账号池：监控 / 搜索每发一次请求前从这里挑账号。

目标是别撞 X 的 15 分钟窗口限额（429）：
- 小号（Cookie 通道）永远参与；官方 API 账号只有在设置里打开「官方 API 也参与抓取」才参与（按条计费，默认关）。
- 每次挑「当前 15 分钟窗口内请求次数最少」的小号（次数从 action_log 数，429 那次也算），
  小号都到了各自的窗口上限才轮到官方号（官方号还要过读额度熔断）。
- 撞到 429 的账号暂停一段时间（accounts.read_paused_until），期间不再被挑中。
- 请求之间随机停几秒（小号风控），测试 mock 模式下不停。

一个都挑不出来时 pick() 返回 (None, 原因)，调用方停掉本次运行、记住进度，等 rate_limit_pause_minutes 后再继续。
"""
from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import config
from ..adapters.factory import _testing_mock
from ..db.database import get_conn, parse_iso, to_iso
from . import budget

WINDOW_MINUTES = 15   # X 的限流窗口

# 设置项及默认值（seed.DEFAULT_SETTINGS 里同名）
DEFAULTS = {
    "read_official_enabled": 0,      # 官方 API 也参与抓取（小号都到上限时才用；计费）
    "read_cap_unofficial": 40,       # 每个小号每 15 分钟最多请求次数（X 对 Cookie 通道大约 50，留余量）
    "read_cap_official": 5,          # 每个官方号每 15 分钟最多请求次数（Basic 档的推主时间线接口就是 5）
    "rate_limit_pause_minutes": 15,  # 撞 429 / 账号都到上限后停多少分钟再继续
    "read_gap_min_seconds": 2,       # 两次请求之间随机间隔的下限
    "read_gap_max_seconds": 6,       # 上限
}


def setting(key: str) -> int:
    return config.get_int(key, DEFAULTS[key])


def official_enabled() -> bool:
    return bool(setting("read_official_enabled"))


def cap_for(account: sqlite3.Row) -> int:
    return max(1, setting("read_cap_official" if account["access_type"] == "official" else "read_cap_unofficial"))


def window_requests(conn: sqlite3.Connection, account_id: int, minutes: int = WINDOW_MINUTES) -> int:
    """该账号在最近 minutes 分钟内向 X 发过的读请求次数（成功失败都算，LLM 调用不算）。"""
    since = to_iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))
    row = conn.execute("SELECT COUNT(*) c FROM action_log WHERE account_id=? AND api_kind!='llm' AND created_at>=?",
                       (account_id, since)).fetchone()
    return int(row["c"] or 0)


def paused_until(account: sqlite3.Row) -> datetime | None:
    try:
        raw = account["read_paused_until"]
    except (IndexError, KeyError):
        return None
    if not raw:
        return None
    dt = parse_iso(raw)
    return dt if dt and dt > datetime.now(timezone.utc) else None


def gap_seconds() -> float:
    """请求之间随机停几秒；上限填 0 = 不停。"""
    lo, hi = max(0, setting("read_gap_min_seconds")), max(0, setting("read_gap_max_seconds"))
    if hi <= 0:
        return 0.0
    if lo > hi:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


@dataclass
class AccountUse:
    account: sqlite3.Row
    requests: int = 0        # 本次运行里用它发了几次请求


@dataclass
class ReadPool:
    auto: bool = False                       # 自动轮询（官方号的读额度熔断更保守）
    used: dict[int, AccountUse] = field(default_factory=dict)
    _first: bool = True

    def pick(self) -> tuple[sqlite3.Row | None, str]:
        """挑一个现在能用的账号。返回 (账号行, 说明)；挑不出来时账号为 None，说明写清为什么、该做什么。"""
        with get_conn() as conn:
            smalls = conn.execute("SELECT * FROM accounts WHERE status='active' AND access_type='unofficial' ORDER BY id").fetchall()
            officials = conn.execute("SELECT * FROM accounts WHERE status='active' AND access_type='official' "
                                     "ORDER BY is_primary DESC, id ASC").fetchall()
            ranked: list[tuple[int, int, sqlite3.Row]] = []
            blocked: list[str] = []
            for a in smalls:
                until = paused_until(a)
                if until:
                    blocked.append(f"@{a['handle']} 撞过 429，暂停到 {_hm(until)}")
                    continue
                n = window_requests(conn, a["id"])
                if n >= cap_for(a):
                    blocked.append(f"@{a['handle']} 最近 {WINDOW_MINUTES} 分钟已请求 {n} 次（上限 {cap_for(a)}）")
                    continue
                ranked.append((n, self.used.get(a["id"], AccountUse(a)).requests, a))
            if ranked:
                ranked.sort(key=lambda x: (x[0], x[1], x[2]["id"]))
                a = ranked[0][2]
                return a, f"小号 @{a['handle']}（最近 {WINDOW_MINUTES} 分钟第 {ranked[0][0] + 1} 次）"
            if officials:
                if not official_enabled():
                    blocked.append("官方 API 账号未参与抓取（设置 → 预算 → 「官方 API 也参与抓取」是关的）")
                else:
                    for a in officials:
                        until = paused_until(a)
                        if until:
                            blocked.append(f"官方号 @{a['handle']} 撞过 429，暂停到 {_hm(until)}")
                            continue
                        n = window_requests(conn, a["id"])
                        if n >= cap_for(a):
                            blocked.append(f"官方号 @{a['handle']} 最近 {WINDOW_MINUTES} 分钟已请求 {n} 次（上限 {cap_for(a)}）")
                            continue
                        denied = budget.current().allow(self.auto)
                        if denied:
                            blocked.append(f"官方号 @{a['handle']}：{denied}")
                            continue
                        return a, f"官方号 @{a['handle']}（计费；最近 {WINDOW_MINUTES} 分钟第 {n + 1} 次）"
        if not smalls and not officials:
            return None, "没有状态为「启用」的账号，无法抓取。请到「设置 → 账号」添加并启用一个账号"
        if not smalls and officials and not official_enabled():
            return None, ("没有启用中的小号，而官方 API 账号默认不参与抓取（按条计费）。"
                          "要用官方号抓取，到「设置 → 预算」打开「官方 API 也参与抓取」")
        return None, "现在没有能用的抓取账号：" + "；".join(blocked)

    def note_request(self, account: sqlite3.Row) -> None:
        u = self.used.setdefault(account["id"], AccountUse(account))
        u.requests += 1

    def mark_rate_limited(self, account: sqlite3.Row, reset_at: datetime | None = None) -> datetime:
        """撞 429：按设置暂停该账号（X 告诉了恢复时间就取两者较晚的）。返回暂停到什么时候。"""
        until = datetime.now(timezone.utc) + timedelta(minutes=max(1, setting("rate_limit_pause_minutes")))
        if reset_at is not None:
            if reset_at.tzinfo is None:
                reset_at = reset_at.replace(tzinfo=timezone.utc)
            until = max(until, reset_at)
        with get_conn() as conn:
            conn.execute("UPDATE accounts SET read_paused_until=? WHERE id=?", (to_iso(until), account["id"]))
            conn.commit()
        return until

    def wait_gap(self) -> float:
        """两次请求之间随机停一下；第一次不停；mock 测试模式不停。返回实际停了几秒。"""
        if self._first:
            self._first = False
            return 0.0
        if _testing_mock():
            return 0.0
        secs = gap_seconds()
        if secs > 0:
            time.sleep(secs)
        return secs

    def summary(self) -> str:
        """「本次用 @a 12 次、@b 11 次（小号通道，不计费）」这样的一句。"""
        if not self.used:
            return ""
        parts = []
        billed = False
        for u in sorted(self.used.values(), key=lambda x: -x.requests):
            parts.append(f"@{u.account['handle']} {u.requests} 次")
            billed = billed or u.account["access_type"] == "official"
        return "本次抓取用了 " + "、".join(parts) + ("（含官方 API，计费）" if billed else "（小号通道，不计费）")


def resume_delay() -> timedelta:
    return timedelta(minutes=max(1, setting("rate_limit_pause_minutes")))


def pool_status() -> list[dict]:
    """仪表盘用：每个启用账号在当前窗口里的请求数 / 上限 / 暂停到几点 / 是否参与。"""
    out = []
    with get_conn() as conn:
        for a in conn.execute("SELECT * FROM accounts WHERE status='active' ORDER BY access_type DESC, is_primary DESC, id").fetchall():
            official = a["access_type"] == "official"
            out.append({
                "handle": a["handle"], "official": official,
                "requests": window_requests(conn, a["id"]), "cap": cap_for(a),
                "paused_until": paused_until(a),
                "participates": (not official) or official_enabled(),
            })
    return out


def _hm(dt: datetime) -> str:
    from ..ui.layout import fmt_time  # 延迟导入：按界面显示时区
    try:
        return fmt_time(to_iso(dt))
    except Exception:
        return dt.strftime("%H:%M UTC")


__all__ = ["ReadPool", "DEFAULTS", "WINDOW_MINUTES", "cap_for", "gap_seconds", "official_enabled",
           "paused_until", "pool_status", "resume_delay", "setting", "window_requests"]
