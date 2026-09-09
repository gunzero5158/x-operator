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


# 审核队列「已跳过」条目上 skip_reason 的中文；分发器写 GuardCode.value，人工跳过写 manual_skip / blacklist
SKIP_REASON_LABEL = {
    GuardCode.AUTHOR_IN_COOLDOWN.value: "作者冷却期内（最近刚回过这个作者）",
    GuardCode.ALREADY_REPLIED.value: "该推文已回复过（去重账本）",
    GuardCode.BLACKLISTED.value: "作者在黑名单",
    GuardCode.TARGET_EXPIRED.value: "条目已过时效",
    "manual_skip": "手动跳过",
    "blacklist": "手动跳过并拉黑作者",
    "guard": "合规拦截",
}


_HARD = {GuardCode.ALREADY_REPLIED, GuardCode.AUTHOR_IN_COOLDOWN,
         GuardCode.BLACKLISTED, GuardCode.TARGET_EXPIRED}


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    code: GuardCode | None
    hard: bool
    detail: str


def _row_get(row, key: str, default=None):
    """sqlite3.Row 没有 .get；测试里手拼的旧结构可能缺列。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


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

    def check(self, account: sqlite3.Row, item: sqlite3.Row, now: datetime | None = None,
              skip_timing: bool = False) -> GuardResult:
        """发送前的完整校验。skip_timing：人工「立即发送」时不看活跃时段和发送间隔；账号状态、日上限和硬违规照查。"""
        now = now or datetime.now(timezone.utc)

        if account["status"] != "active":
            return GuardResult(False, GuardCode.ACCOUNT_NOT_ACTIVE, False, "账号非活跃状态，暂不发送")

        if not skip_timing:
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

        return self.check_hard(item, now)

    def check_hard(self, item: sqlite3.Row, now: datetime | None = None) -> GuardResult:
        """硬违规：黑名单 / 已回复过 / 作者冷却 / 条目过时效。和账号状态、时段、间隔、日上限无关，
        所以「已跳过」条目的「重新判断」只跑这一段。
        人工放行（force_send=1）的条目只查「已回复过」——同一条推文的回复在去重账本里是唯一的，回第二次记不了账；
        冷却 / 黑名单 / 时效都是人工放行时明知故犯的，不再拦。"""
        now = now or datetime.now(timezone.utc)
        action = item["action_type"]
        forced = bool(_row_get(item, "force_send", 0))
        if action == "reply" and item["target_tweet_id"] is not None:
            with get_conn() as conn:
                tgt = conn.execute("SELECT * FROM target_tweets WHERE id=?", (item["target_tweet_id"],)).fetchone()
                if tgt is not None:
                    # 黑名单
                    if not forced and is_blacklisted(conn, tgt["author_id"], tgt["author_handle"]):
                        return GuardResult(False, GuardCode.BLACKLISTED, True, "目标作者在黑名单中")
                    # 去重账本：该目标推文是否已被任一自有账号回过
                    dup = conn.execute(
                        "SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?",
                        (tgt["tweet_id"],)).fetchone()
                    if dup:
                        return GuardResult(False, GuardCode.ALREADY_REPLIED, True, "该推文已回复过（去重账本）")
                    # 作者冷却
                    if forced:
                        return GuardResult(True, None, False, "通过（人工放行）")
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
        if expires and now >= expires and not forced:
            return GuardResult(False, GuardCode.TARGET_EXPIRED, True, "该回复条目已过时效")

        return GuardResult(True, None, False, "通过")

    def recheck_skipped(self, item_id: int, now: datetime | None = None) -> tuple[bool, str]:
        """「已跳过」条目重新判断：按现在的情况把跳过规则（黑名单 / 已回复过 / 作者冷却 / 时效）全部再查一遍，
        都不再成立 → 放回待审核（时效不动）；仍不通过 → 保持已跳过，skip_reason 更新成现在的原因。返回 (是否放回, 说明)。"""
        now = now or datetime.now(timezone.utc)
        with get_conn() as conn:
            item = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
            if item is None:
                return False, "条目不存在"
            if item["status"] != "skipped":
                return False, "只有「已跳过」的条目能重新判断"
            gr = self.check_hard(item, now)
            if not gr.ok:
                code = gr.code.value if gr.code else "guard"
                if code != item["skip_reason"]:
                    conn.execute("UPDATE review_queue SET skip_reason=? WHERE id=?", (code, item_id))
                    conn.commit()
                return False, gr.detail
            conn.execute("UPDATE review_queue SET status='pending', skip_reason=NULL, decided_at=NULL WHERE id=?", (item_id,))
            if item["target_tweet_id"]:
                conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=? AND process_status IN ('expired','no_match','filtered')",
                             (item["target_tweet_id"],))
            conn.commit()
        return True, "已放回待审核"

    def force_restore(self, item_id: int) -> tuple[bool, str]:
        """人工放行：不管跳过原因，直接放回待审核，并标 force_send=1——发送时不再按冷却 / 黑名单 / 时效拦；
        时效清空（不再自动过期，由人负责）。「已回复过」仍会拦，因为去重账本记不了第二条。返回 (是否成功, 说明)。"""
        with get_conn() as conn:
            item = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
            if item is None:
                return False, "条目不存在"
            if item["status"] != "skipped":
                return False, "只有「已跳过」的条目能人工放行"
            if item["action_type"] == "reply" and item["target_tweet_id"]:
                tgt = conn.execute("SELECT tweet_id FROM target_tweets WHERE id=?", (item["target_tweet_id"],)).fetchone()
                if tgt and conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?", (tgt["tweet_id"],)).fetchone():
                    return False, "该推文已回复过，去重账本不允许再回一次；想再回请到抓取记录里重新生成"
            conn.execute("UPDATE review_queue SET status='pending', skip_reason=NULL, decided_at=NULL, force_send=1, expires_at=NULL WHERE id=?",
                         (item_id,))
            if item["target_tweet_id"]:
                conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=? AND process_status IN ('expired','no_match','filtered')",
                             (item["target_tweet_id"],))
            conn.commit()
        return True, "已人工放行到待审核"

    def recheck_all_skipped(self, now: datetime | None = None) -> dict:
        """批量重新判断所有「已跳过」条目。返回 {restored, still, reasons:{原因: 条数}}。"""
        with get_conn() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM review_queue WHERE status='skipped' ORDER BY created_at").fetchall()]
        out = {"restored": 0, "still": 0, "reasons": {}}
        for i in ids:
            ok, detail = self.recheck_skipped(i, now)
            if ok:
                out["restored"] += 1
            else:
                out["still"] += 1
                out["reasons"][detail] = out["reasons"].get(detail, 0) + 1
        return out
