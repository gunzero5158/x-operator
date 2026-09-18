"""ComplianceGuard（design-v1.1 §7.1）：发送前最终校验。

已过时效→expired；其他硬违规→skipped；软违规→approved，本轮跳过、下轮再试。
check/check_hard 只读；重新判断与恢复操作会更新队列。所有 detail 为中文人话，可直接展示。
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


# 任务队列「已跳过」条目上 skip_reason 的中文；分发器写 GuardCode.value，人工跳过写 manual_skip / blacklist
SKIP_REASON_LABEL = {
    GuardCode.AUTHOR_IN_COOLDOWN.value: "作者冷却期内（按所有账号的最近成功回复计算）",
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


def next_allowed_key(action_type: str) -> str:
    """账号上记录「下次可发时间」的列：主贴 next_allowed_post_at，回复 next_allowed_at（两套冷却互不影响）。"""
    return "next_allowed_post_at" if action_type == "post" else "next_allowed_at"


def _display_time(value: datetime | None) -> str:
    if value is None:
        return "未记录"
    name = config.get("display_timezone") or "Asia/Tokyo"
    return value.astimezone(_tz(name)).strftime("%m-%d %H:%M") + f"（{name}）"


def author_cooldown(conn: sqlite3.Connection, author_id: str | None,
                    now: datetime | None = None) -> GuardResult | None:
    """抓取和发送共用的作者冷却：全部账号成功回复的最近一次，截止瞬间即放行。"""
    days = max(0, config.get_int("cooldown_days", 7))
    if not author_id or not days:
        return None
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT MAX(sent_at) AS last_reply FROM interactions "
                       "WHERE action='reply' AND author_id=?", (author_id,)).fetchone()
    last = parse_iso(row["last_reply"])
    until = last + timedelta(days=days) if last else None
    if until is None or now >= until:
        return None
    return GuardResult(False, GuardCode.AUTHOR_IN_COOLDOWN, True,
                       f"作者冷却期 {days} 天（所有账号共用）：最近成功回复 {_display_time(last)}；"
                       f"冷却结束 {_display_time(until)}。与回复条目的有效期分别计算")


def expire_queue_items(conn: sqlite3.Connection, now: datetime, *, item_id: int | None = None) -> int:
    """只归档未发送且到期的回复；发送中、失败和人工放行条目不在清扫范围。调用方提交事务。"""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    query = ("SELECT id FROM review_queue WHERE action_type='reply' "
             "AND status IN ('pending','approved','skipped') AND force_send=0 "
             "AND expires_at IS NOT NULL AND expires_at<=?")
    args: list = [to_iso(now)]
    if item_id is not None:
        query += " AND id=?"
        args.append(item_id)
    ids = [r["id"] for r in conn.execute(query, args)]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    conn.execute(f"UPDATE review_queue SET status='expired', skip_reason='target_expired', decided_at=? "
                 f"WHERE id IN ({marks})", [to_iso(now), *ids])
    conn.execute("UPDATE target_tweets SET process_status='expired', "
                 "llm_relevance_reason='回复草稿已过有效期；可重新生成并审核，作者冷却仍单独检查' "
                 f"WHERE process_status='queued' AND id IN (SELECT target_tweet_id FROM review_queue WHERE id IN ({marks})) "
                 "AND NOT EXISTS (SELECT 1 FROM review_queue q WHERE q.target_tweet_id=target_tweets.id "
                 "AND q.status IN ('pending','approved','sending','sent'))", ids)
    return len(ids)


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

            # 发送间隔：主贴和回复各自一个冷却，互不影响
            next_allowed = parse_iso(account[next_allowed_key(item["action_type"])])
            if next_allowed and now < next_allowed:
                return GuardResult(False, GuardCode.INTERVAL_NOT_ELAPSED, False,
                                   f"两次{'发帖' if item['action_type'] == 'post' else '回复'}的最小间隔未到")

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
        """同时收集硬拦截原因，过期优先归类；人工放行仍检查同一推文去重。"""
        now = now or datetime.now(timezone.utc)
        forced = bool(_row_get(item, "force_send", 0))
        blocks: list[GuardResult] = []
        expires = parse_iso(item["expires_at"])
        if item["action_type"] == "reply" and expires and now >= expires and not forced:
            blocks.append(GuardResult(False, GuardCode.TARGET_EXPIRED, True,
                                      f"回复草稿已过时效：入队 {_display_time(parse_iso(_row_get(item, 'created_at')))}，"
                                      f"到期 {_display_time(expires)}；等待作者冷却不会延长草稿有效期"))
        if item["action_type"] == "reply" and item["target_tweet_id"] is not None:
            with get_conn() as conn:
                tgt = conn.execute("SELECT * FROM target_tweets WHERE id=?", (item["target_tweet_id"],)).fetchone()
                if tgt is not None:
                    if not forced and is_blacklisted(conn, tgt["author_id"], tgt["author_handle"]):
                        blocks.append(GuardResult(False, GuardCode.BLACKLISTED, True, "目标作者在黑名单中"))
                    if conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?",
                                    (tgt["tweet_id"],)).fetchone():
                        blocks.append(GuardResult(False, GuardCode.ALREADY_REPLIED, True, "该推文已回复过（去重账本）"))
                    if not forced:
                        cooldown = author_cooldown(conn, tgt["author_id"], now)
                        if cooldown:
                            blocks.append(cooldown)
        if blocks:
            return GuardResult(False, blocks[0].code, True, "；".join(b.detail for b in blocks))
        return GuardResult(True, None, False, "通过（人工放行）" if forced else "通过")

    def recheck_skipped(self, item_id: int, now: datetime | None = None) -> tuple[bool, str]:
        """「已跳过」条目重新判断：按现在的情况把跳过规则（黑名单 / 已回复过 / 作者冷却 / 时效）全部再查一遍，
        都不再成立 → 放回待审核（时效不动）；已过期 → 移到已过期；其他拦截 → 保持已跳过，原因更新为当前结果。返回 (是否放回, 说明)。"""
        now = now or datetime.now(timezone.utc)
        with get_conn() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
            if item is None:
                conn.commit()
                return False, "条目不存在"
            if item["status"] != "skipped":
                conn.commit()
                return False, "只有「已跳过」的条目能重新判断"
            gr = self.check_hard(item, now)
            if not gr.ok:
                if gr.code == GuardCode.TARGET_EXPIRED:
                    expired = expire_queue_items(conn, now, item_id=item_id)
                    conn.commit()
                    return False, ("已移到「已过期」：" if expired else "条目状态已改变，请刷新：") + gr.detail
                code = gr.code.value if gr.code else "guard"
                changed = conn.execute("UPDATE review_queue SET skip_reason=? WHERE id=? AND status='skipped'",
                                       (code, item_id)).rowcount
                conn.commit()
                return False, gr.detail if changed else "条目状态已改变，请刷新"
            changed = conn.execute("UPDATE review_queue SET status='pending', skip_reason=NULL, decided_at=NULL "
                                   "WHERE id=? AND status='skipped'", (item_id,)).rowcount
            if not changed:
                conn.commit()
                return False, "条目状态已改变，请刷新"
            if item["target_tweet_id"]:
                conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=? AND process_status IN ('expired','no_match','filtered')",
                             (item["target_tweet_id"],))
            conn.commit()
        return True, "已放回待审核"

    def force_restore(self, item_id: int, *, expected_status: str | None = None) -> tuple[bool, str]:
        """人工放行：不管跳过原因，直接放回待审核，并标 force_send=1——发送时不再按冷却 / 黑名单 / 时效拦；
        时效清空（不再自动过期，由人负责）。「已回复过」仍会拦，因为去重账本记不了第二条。返回 (是否成功, 说明)。"""
        return self._force_transition(item_id, expected_status, "pending")

    def claim_expired(self, item_id: int, account_id: int) -> tuple[bool, str]:
        """分发器持有账号锁后，原子地将过期草稿人工放行并认领为发送中。"""
        return self._force_transition(item_id, "expired", "sending", account_id)

    def _force_transition(self, item_id: int, expected_status: str | None, destination: str,
                          account_id: int | None = None) -> tuple[bool, str]:
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
            if item is None:
                conn.rollback()
                return False, "条目不存在"
            if item["status"] not in ("skipped", "expired") or (expected_status and item["status"] != expected_status):
                conn.rollback()
                return False, "条目状态已改变；只有「已跳过」或「已过期」的条目能人工放行，请刷新后再处理"
            if account_id is not None and item["account_id"] != account_id:
                conn.rollback()
                return False, "发送账号已改变，请刷新后重新确认"
            if item["action_type"] == "reply":
                tgt = conn.execute("SELECT tweet_id FROM target_tweets WHERE id=?", (item["target_tweet_id"],)).fetchone()
                if tgt is None:
                    conn.rollback()
                    return False, "目标推文记录已不存在，不能恢复发送"
                if conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?", (tgt["tweet_id"],)).fetchone():
                    conn.rollback()
                    return False, "该推文已回复过，去重账本不允许重复回复；重新生成也不会绕过去重"
                other = conn.execute("SELECT id FROM review_queue WHERE target_tweet_id=? AND id<>? "
                                     "AND status IN ('pending','approved','sending','sent') LIMIT 1",
                                     (item["target_tweet_id"], item_id)).fetchone()
                if other:
                    conn.rollback()
                    return False, f"同一推文已有任务 #{other['id']} 待处理或已发送，请先处理该任务"
            changed = conn.execute("UPDATE review_queue SET status=?, skip_reason=NULL, decided_at=?, force_send=1, expires_at=NULL "
                                   "WHERE id=? AND status=?", (destination, utcnow_iso() if destination == "sending" else None,
                                                              item_id, item["status"])).rowcount
            if not changed:
                conn.commit()
                return False, "条目状态已改变（可能已过期），请刷新后再处理"
            if item["target_tweet_id"]:
                conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=? AND process_status IN ('expired','no_match','filtered')",
                             (item["target_tweet_id"],))
            conn.commit()
        return True, "已人工放行到待审核" if destination == "pending" else "已人工放行，正在发送"

    def restore_failed(self, item_id: int) -> tuple[bool, str]:
        """「失败」条目捞回任务队列：放回待审核（人再看一眼、批准后重新发），重试计数清零，上次的错误原因保留在条目上；
        时效已过的按当前设置重新给一段时效。「已回复过」的不能捞（去重账本只记一次）。返回 (是否成功, 说明)。"""
        with get_conn() as conn:
            item = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
            if item is None:
                return False, "条目不存在"
            if item["status"] != "failed":
                return False, "只有「失败」的条目能捞回"
            if item["action_type"] == "reply" and item["target_tweet_id"]:
                tgt = conn.execute("SELECT tweet_id FROM target_tweets WHERE id=?", (item["target_tweet_id"],)).fetchone()
                if tgt and conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?", (tgt["tweet_id"],)).fetchone():
                    return False, "该推文已回复过（上次其实发出去了），去重账本不允许再回一次"
            now = datetime.now(timezone.utc)
            expires = parse_iso(item["expires_at"])
            new_exp = item["expires_at"]
            note = ""
            if item["action_type"] == "reply" and expires and expires <= now and not item["force_send"]:
                new_exp = to_iso(now + timedelta(hours=config.get_int("reply_ttl_hours", 48)))
                note = "，时效已过、按当前设置重新计时"
            conn.execute("UPDATE review_queue SET status='pending', decided_at=NULL, retry_count=0, expires_at=? WHERE id=?",
                         (new_exp, item_id))
            if item["target_tweet_id"]:
                conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=? AND process_status IN ('expired','no_match','filtered')",
                             (item["target_tweet_id"],))
            conn.commit()
        return True, "已捞回待审核" + note + "。上次失败原因仍显示在条目上，批准前请先看一眼"

    def recheck_all_skipped(self, now: datetime | None = None) -> dict:
        """批量重新判断所有「已跳过」条目。返回 {restored, expired, still, reasons:{原因: 条数}}。"""
        with get_conn() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM review_queue WHERE status='skipped' ORDER BY created_at").fetchall()]
        out = {"restored": 0, "expired": 0, "still": 0, "reasons": {}}
        for i in ids:
            ok, detail = self.recheck_skipped(i, now)
            if ok:
                out["restored"] += 1
            elif detail.startswith("已移到「已过期」"):
                out["expired"] += 1
            else:
                out["still"] += 1
                out["reasons"][detail] = out["reasons"].get(detail, 0) + 1
        return out
