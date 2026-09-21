"""Dashboard regression checks: temporary databases, no external requests."""
import os
from datetime import datetime, timedelta, timezone

os.environ["X_OPERATOR_MOCK"] = "1"

import pytest
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from x_operator import config
from x_operator.core.account_usage import daily_account_usage
from x_operator.core.compliance import ComplianceGuard
from x_operator.db import database
from x_operator.db.database import get_conn, init_db, to_iso
from x_operator.ui.daily_usage import DailyUsagePanel, RecentErrorsPanel

pytest_plugins = ["nicegui.testing.user_plugin"]


@pytest.fixture
def db(tmp_path):
    if getattr(database._local, "conn", None):
        database._local.conn.close()
        del database._local.conn
    init_db(tmp_path / "usage.db")


def account(handle, tz="Asia/Tokyo", access="official", status="active", created="2020-01-01T00:00:00Z"):
    with get_conn() as conn:
        aid = conn.execute("INSERT INTO accounts(handle,display_name,timezone,access_type,status,created_at,daily_post_limit,daily_reply_limit) "
                           "VALUES (?,?,?,?,?,?,4,6)", (handle, "Name " + handle, tz, access, status, created)).lastrowid
        conn.commit()
    return aid


def sent(aid, action, at):
    with get_conn() as conn:
        conn.execute("INSERT INTO interactions(account_id,action,tweet_id,sent_at) VALUES (?,?,?,?)",
                     (aid, action, f"{aid}-{action}-{at}", at))
        conn.commit()


def test_timezone_midnight_and_dispatch_agree(db):
    now = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)
    tokyo = account("tokyo")
    la = account("la", "America/Los_Angeles", status="paused")
    invalid = account("fallback", "not-a-zone")
    deleted = account("removed")
    for aid in (tokyo, la, invalid, deleted):
        for action in ("post", "reply"):
            for stamp in ("2026-09-20T07:00:00Z", "2026-09-20T14:59:59Z", "2026-09-20T15:00:00Z", "2026-09-21T00:00:00Z"):
                sent(aid, action, stamp)
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET deleted_at='2026-09-21T00:00:00Z' WHERE id=?", (deleted,))
        conn.execute("INSERT INTO review_queue(account_id,action_type,final_text,status) VALUES (?,'post','not sent','failed')", (tokyo,))
        conn.commit()
    rows = {r["id"]: r for r in daily_account_usage(now)}
    assert deleted not in rows
    assert rows[tokyo]["post_used"] == 2 and rows[la]["post_used"] == 4
    assert rows[invalid]["post_used"] == 2
    assert rows[tokyo]["day"] == "2026-09-21" and rows[la]["day"] == "2026-09-20"
    for row in rows.values():
        for action in ("post", "reply"):
            assert row[f"{action}_used"] == ComplianceGuard().daily_action_count(row["id"], action, now, row["timezone"])


def test_current_limits_nurture_and_lowering_below_usage(db):
    now = datetime.now(timezone.utc)
    aid = account("new", access="unofficial", created=to_iso(now - timedelta(days=1)))
    sent(aid, "post", to_iso(now))
    row = daily_account_usage(now)[0]
    assert (row["post_limit"], row["reply_limit"], row["post_remaining"], row["adjusted"]) == (2, 3, 1, True)
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET daily_post_limit=20,daily_reply_limit=30 WHERE id=?", (aid,))
        conn.commit()
    row = daily_account_usage(now)[0]
    assert (row["post_limit"], row["reply_limit"], row["post_remaining"]) == (10, 15, 9)
    config.set_value("nurture_days", 0)
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET daily_post_limit=0,daily_reply_limit=0 WHERE id=?", (aid,))
        conn.commit()
    row = daily_account_usage(now)[0]
    assert (row["post_used"], row["post_limit"], row["post_remaining"], row["reply_remaining"]) == (1, 0, 0, 0)


async def test_hundreds_pagination_filters_and_refresh_preserve_state(user: User, db):
    for i in range(137):
        account(f"writer{i:03}")
    panels = []
    @ui.page("/usage-test")
    def page():
        panels.append(DailyUsagePanel())
    await user.open("/usage-test")
    panel = panels[0]
    assert len(panel.table.rows) == 137 and panel.table.pagination["rowsPerPage"] == 10
    panel.table.pagination = {"page": 4, "rowsPerPage": 25, "sortBy": "reply_remaining", "descending": True}
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET daily_post_limit=40,daily_reply_limit=60 WHERE handle='writer090'")
        conn.commit()
    with user:
        panel.refresh()
    assert panel.table.pagination == {"page": 4, "rowsPerPage": 25, "sortBy": "reply_remaining", "descending": True}
    assert next(r for r in panel.table.rows if r["handle"] == "writer090")["reply_limit"] == 60
    panel.search.set_value("@writer090")
    await user.should_see("显示 1 / 137 个账号")
    assert panel.table.pagination["page"] == 1 and len(panel.table.rows) == 1
    with user:
        panel.refresh()
    assert panel.search.value == "@writer090" and len(panel.table.rows) == 1
    panel.search.set_value("no_such_account")
    await user.should_see("没有符合条件的账号")
    panel.search.set_value("")
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET daily_post_limit=0 WHERE handle='writer010'")
        conn.execute("UPDATE accounts SET status='paused' WHERE handle='writer020'")
        conn.commit()
    with user:
        panel.refresh()
    panel.scope.set_value("limited")
    assert [r["handle"] for r in panel.table.rows] == ["writer010"]
    panel.scope.set_value("inactive")
    assert [r["handle"] for r in panel.table.rows] == ["writer020"]


async def test_errors_collapsed_with_details_and_stable_refresh(user: User, db):
    panels = []
    stats = {"fails": 7, "recent_fail": [{"id": 1, "created_at": "2026-09-21T00:00:00Z", "endpoint": "SMOKE", "error": "test error " * 500}]}
    @ui.page("/errors-test")
    def page():
        panel = RecentErrorsPanel()
        panel.refresh(stats)
        panels.append(panel)
    await user.open("/errors-test")
    panel = panels[0]
    assert not panel.expansion.value
    UserInteraction(user, {panel.expansion}, None).trigger("update:modelValue", True)
    assert panel.table.pagination["rowsPerPage"] == 5
    UserInteraction(user, {panel.table}, None).trigger("error_detail", panel.table.rows[0])
    await user.should_see("异常详情")
    await user.should_see("test error " * 500)
    with user:
        panel.refresh(stats)
    assert panel.expansion.value
    await user.should_see("异常详情")
