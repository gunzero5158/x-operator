"""Read-only dashboard usage, using the same ledger and limits as dispatch."""
from datetime import datetime, timezone

from .compliance import ComplianceGuard, _tz
from ..db.database import get_conn, to_iso


def daily_account_usage(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    guard = ComplianceGuard()
    with get_conn() as conn:
        accounts = conn.execute(
            "SELECT id,handle,display_name,status,access_type,created_at,timezone,"
            "daily_post_limit,daily_reply_limit FROM accounts WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        counts = {}
        # One indexed aggregation per timezone, rather than two queries per account.
        for name in {a["timezone"] for a in accounts}:
            start = now.astimezone(_tz(name)).replace(hour=0, minute=0, second=0, microsecond=0)
            for row in conn.execute(
                "SELECT i.account_id,i.action,COUNT(*) AS n FROM accounts a "
                "JOIN interactions i ON i.account_id=a.id "
                "WHERE a.deleted_at IS NULL AND a.timezone=? AND i.action IN ('post','reply') AND i.sent_at>=? "
                "GROUP BY i.account_id,i.action", (name, to_iso(start))
            ):
                counts[row["account_id"], row["action"]] = row["n"]
    result = []
    for account in accounts:
        row = dict(account)
        row["timezone"] = _tz(row["timezone"]).key
        row["day"] = now.astimezone(_tz(row["timezone"])).strftime("%Y-%m-%d")
        post_limit, reply_limit = guard.effective_limits(account)
        for action, limit in (("post", post_limit), ("reply", reply_limit)):
            used = counts.get((row["id"], action), 0)
            row[f"{action}_used"] = used
            row[f"{action}_limit"] = limit
            row[f"{action}_remaining"] = max(0, limit - used)
        row["adjusted"] = (post_limit, reply_limit) != (row["daily_post_limit"], row["daily_reply_limit"])
        result.append(row)
    return result
