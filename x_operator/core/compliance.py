"""ComplianceGuard（design-v1.1 §7.1）：发送前最终校验。

硬违规→条目置 skipped；软违规→条目保持 approved，本轮跳过、下轮再试。
只读不写库。所有 detail 为中文人话，可直接展示。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from .. import config
from ..db.database import get_conn, parse_iso, to_iso


class GuardCode(str, Enum):
    ACCOUNT_NOT_ACTIVE = "account_not_active"      # 软
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"  # 软
    INTERVAL_NOT_ELAPSED = "interval_not_elapsed"  # 软
    DAILY_LIMIT_REACHED = "daily_limit_reached"    # 软
    ALREADY_REPLIED = "already_replied"            # 硬
    AUTHOR_IN_COOLDOWN = "author_in_cooldown"      # 硬
    BLACKLISTED = "blacklisted"                    # 硬
    TARGET_EXPIRED = "target_expired"              # 硬


_HARD = {GuardCode.ALREADY_REPLIED, GuardCode.AUTHOR_IN_COOLDOWN,
         GuardCode.BLACKLISTED, GuardCode.TARGET_EXPIRED}


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    code: GuardCode | None
    hard: bool
    detail: str


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Tokyo")


def is_blacklisted(conn: sqlite3.Connection, author_id: str | None, author_handle: str | None) -> bool:
    """黑名单同时按数字 user_id 和 @handle 匹配——设置页手填的多半是 handle。"""
    h = (author_handle or "").lstrip("@").strip().lower()
    row = conn.execute(
        "SELECT 1 FROM blacklist WHERE x_user_id=? OR (?<>'' AND (lower(handle)=? OR lower(x_user_id)=?)) LIMIT 1",
        (author_id or "", h, h, h)).fetchone()
    return row is not None


class ComplianceGuard:
    def is_in_active_hours(self, account: sqlite3.Row, now: datetime) -> bool:
        tz = _tz(account["timezone"])
        local = now.astimezone(tz)
        start = account["active_hours_start"]
        end = account["active_hours_end"]
        if start == end:
            return True  # 全天
        t = local.strftime("%H:%M")
        if start < end:
            return start <= t < end
        return t >= start or t < end  # 跨日时段

    def daily_action_count(self, account_id: int, action: str, now: datetime, tz_name: str) -> int:
        tz = _tz(tz_name)
        local = now.astimezone(tz)
        day_start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = to_iso(day_start_local)
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM interactions WHERE account_id=? AND action=? AND sent_at>=?",
                (account_id, action, day_start_utc),
            ).fetchone()
        return row["c"]

    def effective_limits(self, account: sqlite3.Row) -> tuple[int, int]:
        post_lim = account["daily_post_limit"]
        reply_lim = account["daily_reply_limit"]
        if account["access_type"] == "unofficial":
            created = parse_iso(account["created_at"])
            nurture_days = config.get_int("nurture_days", 14)
            if created and (datetime.now(timezone.utc) - created) < timedelta(days=nurture_days):
                post_lim = max(1, post_lim // 2)
                reply_lim = max(1, reply_lim // 2)
        return post_lim, reply_lim

    def check(self, account: sqlite3.Row, item: sqlite3.Row, now: datetime | None = None) -> GuardResult:
        now = now or datetime.now(timezone.utc)

        if account["status"] != "active":
            return GuardResult(False, GuardCode.ACCOUNT_NOT_ACTIVE, False, "账号非活跃状态，暂不发送")

        if not self.is_in_active_hours(account, now):
            return GuardResult(False, GuardCode.OUTSIDE_ACTIVE_HOURS, False, "当前不在账号活跃时段内")

        # 发送间隔
        next_allowed = parse_iso(account["next_allowed_at"])
        if next_allowed and now < next_allowed:
            return GuardResult(False, GuardCode.INTERVAL_NOT_ELAPSED, False, "两次发送最小间隔未到")

        # 日上限
        post_lim, reply_lim = self.effective_limits(account)
        action = item["action_type"]
        limit = post_lim if action == "post" else reply_lim
        used = self.daily_action_count(account["id"], action, now, account["timezone"])
        if used >= limit:
            return GuardResult(False, GuardCode.DAILY_LIMIT_REACHED, False,
                               f"今日{'发帖' if action == 'post' else '回复'}已达上限（{used}/{limit}）")

        # 以下为硬违规（仅 reply 相关）
        if action == "reply" and item["target_tweet_id"] is not None:
            with get_conn() as conn:
                tgt = conn.execute("SELECT * FROM target_tweets WHERE id=?", (item["target_tweet_id"],)).fetchone()
                if tgt is not None:
                    # 黑名单
                    if is_blacklisted(conn, tgt["author_id"], tgt["author_handle"]):
                        return GuardResult(False, GuardCode.BLACKLISTED, True, "目标作者在黑名单中")
                    # 去重账本：该目标推文是否已被任一自有账号回过
                    dup = conn.execute(
                        "SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?",
                        (tgt["tweet_id"],)).fetchone()
                    if dup:
                        return GuardResult(False, GuardCode.ALREADY_REPLIED, True, "该推文已回复过（去重账本）")
                    # 作者冷却
                    cooldown_days = config.get_int("cooldown_days", 7)
                    cutoff = to_iso(now - timedelta(days=cooldown_days))
                    cd = conn.execute(
                        "SELECT 1 FROM interactions WHERE author_id=? AND sent_at>=? LIMIT 1",
                        (tgt["author_id"], cutoff)).fetchone()
                    if cd:
                        return GuardResult(False, GuardCode.AUTHOR_IN_COOLDOWN, True,
                                           f"作者处于 {cooldown_days} 天冷却期内")

        # 队列条目过期（reply 类）
        expires = parse_iso(item["expires_at"])
        if expires and now >= expires:
            return GuardResult(False, GuardCode.TARGET_EXPIRED, True, "该回复条目已过时效")

        return GuardResult(True, None, False, "通过")
