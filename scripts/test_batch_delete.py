"""Selected deletion regression checks. Disposable DB, no scheduler or external calls."""
import os

os.environ["X_OPERATOR_MOCK"] = "1"

import pytest
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from x_operator.core.scheduler import Jobs
from x_operator.db import database
from x_operator.db.database import get_conn, init_db
from x_operator.ui import queue, targets

pytest_plugins = ["nicegui.testing.user_plugin"]


@pytest.fixture
def pages(tmp_path):
    if getattr(database._local, "conn", None):
        database._local.conn.close()
        del database._local.conn
    init_db(tmp_path / "batch.db")
    with get_conn() as conn:
        for name in ("one", "two"):
            conn.execute("INSERT INTO accounts(handle,status,access_type) VALUES (?,'paused','unofficial')", (name,))
            conn.execute("INSERT INTO search_rules(name,keyword_query,semantic_criteria) VALUES (?,'software','tools')", (name,))
        conn.commit()
    jobs = Jobs()
    queue.register(jobs)
    targets.register(jobs)


def target(status="filtered", source="search", rule=1):
    with get_conn() as conn:
        n = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM target_tweets").fetchone()[0]
        tid = conn.execute("INSERT INTO target_tweets(tweet_id,author_id,author_handle,text,process_status,source,source_rule_id,tweet_created_at) "
                           "VALUES (?, 'author', 'example', ?, ?, ?, ?, '2026-09-22T00:00:00Z')", (str(n), f"record {n}", status, source, rule)).lastrowid
        conn.commit()
    return tid


def task(status="pending", account=1, tid=None):
    with get_conn() as conn:
        qid = conn.execute("INSERT INTO review_queue(account_id,action_type,target_tweet_id,final_text,status) VALUES (?,?,?,?,?)",
                           (account, "reply" if tid else "post", tid, "example draft", status)).lastrowid
        conn.commit()
    return qid


def ids(table):
    assert table in ("review_queue", "target_tweets")
    with get_conn() as conn:
        return {row[0] for row in conn.execute(f"SELECT id FROM {table}")}


def cancel_dialog(user):
    buttons = {el for el in user.find(kind=ui.button).elements if el.text == "取消"}
    UserInteraction(user, buttons, None).click()


@pytest.mark.parametrize("status", ["pending", "approved", "failed", "skipped", "expired", "sent", "unsent"])
async def test_queue_each_list_delete_only_selected(user: User, pages, status):
    actual = "pending" if status == "unsent" else status
    first, other = task(actual), task(actual)
    outside = task(actual, account=2)
    await user.open(f"/queue?status={status}&account=1")
    delete = user.find(kind=ui.button, content="批量删除")
    assert not next(iter(delete.elements)).enabled
    user.find(f"queue-select-{first}").click()
    await user.should_see("已选 1 条")
    delete.click()
    await user.should_see("删除选中的 1 条任务？")
    cancel_dialog(user)
    await user.should_not_see("删除选中的 1 条任务？")
    assert ids("review_queue") == {first, other, outside}
    await user.should_see("已选 1 条")
    user.find(kind=ui.button, content="批量删除").click()
    await user.should_see("删除选中的 1 条任务？")
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 1 条，保留 0 条")
    assert ids("review_queue") == {other, outside}


@pytest.mark.parametrize("status", ["all", "new", "no_match", "filtered", "queued", "expired"])
async def test_targets_each_list_respects_source_and_visible_limit(user: User, pages, monkeypatch, status):
    actual = "new" if status == "all" else status
    hidden = target(actual)
    visible = {target(actual), target(actual)}
    outside = target(actual, source="monitor", rule=0)
    other_rule = target(actual, rule=2)
    monkeypatch.setattr(targets, "_LIMIT", 2)
    await user.open(f"/targets?status={status}&source=search&rule=1")
    user.find(kind=ui.button, content="全选当前列表").click()
    await user.should_see("已选 2 条")
    user.find(kind=ui.button, content="取消全选").click()
    await user.should_see("已选 0 条")
    user.find(kind=ui.button, content="全选当前列表").click()
    user.find(kind=ui.button, content="批量删除").click()
    await user.should_see("删除选中的 2 条抓取记录？")
    cancel_dialog(user)
    await user.should_not_see("删除选中的 2 条抓取记录？")
    await user.should_see("已选 2 条")
    user.find(kind=ui.button, content="批量删除").click()
    await user.should_see("删除选中的 2 条抓取记录？")
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 2 条，保留 0 条")
    assert ids("target_tweets") == {hidden, outside, other_rule}


async def test_queue_select_all_preserves_edits_and_filter_clears_selection(user: User, pages, monkeypatch):
    hidden, first, second = task(), task(), task()
    other = task(account=2)
    monkeypatch.setattr(queue, "_LIMIT", 2)
    await user.open("/queue?account=1")
    editor = next(iter(user.find(kind=ui.textarea).elements))
    with user:
        editor.set_value("unsaved draft")
    user.find(kind=ui.button, content="全选当前列表").click()
    await user.should_see("已选 2 条")
    assert not editor.is_deleted and editor.value == "unsaved draft"
    account = min(user.find(kind=ui.select).elements, key=lambda el: el.id if el.label == "发送账号" else 100000)
    with user:
        account.set_value(2)
    UserInteraction(user, {account}, None).trigger("update:modelValue", {"value": account._values.index(2)})
    await user.should_see("已选 0 条")
    user.find(kind=ui.button, content="全选当前列表").click()
    user.find(kind=ui.button, content="批量删除").click()
    await user.should_see("删除选中的 1 条任务？")
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 1 条，保留 0 条")
    assert ids("review_queue") == {hidden, first, second}


async def test_queue_rechecks_after_confirmation_open(user: User, pages):
    changed, moved, stable = task(), task(), task()
    await user.open("/queue")
    user.find(kind=ui.button, content="全选当前列表").click()
    user.find(kind=ui.button, content="批量删除").click()
    await user.should_see("删除选中的 3 条任务？")
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET status='sending' WHERE id=?", (changed,))
        conn.execute("UPDATE review_queue SET account_id=2 WHERE id=?", (moved,))
        conn.commit()
    added_later = task()
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 1 条，保留 2 条")
    assert ids("review_queue") == {changed, moved, added_later}


def test_targets_keep_references_and_changed_or_missing_records(pages):
    linked, changed, moved, missing, stable = [target() for _ in range(5)]
    records = {tid: ("filtered", "search", 1) for tid in (linked, changed, moved, missing, stable)}
    task(tid=linked)
    with get_conn() as conn:
        conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=?", (changed,))
        conn.execute("UPDATE target_tweets SET source_rule_id=2 WHERE id=?", (moved,))
        conn.execute("DELETE FROM target_tweets WHERE id=?", (missing,))
        conn.commit()
    assert targets._delete_selected(records) == (1, 4)
    assert ids("target_tweets") == {linked, changed, moved}


def test_queue_delete_keeps_ledger_materials_and_syncs_target(pages):
    lone, shared, sent = [target("queued") for _ in range(3)]
    q1, q2, q3 = task(tid=lone), task(tid=shared), task("sent", tid=sent)
    sibling = task("approved", tid=shared)
    sending = task("sending")
    with get_conn() as conn:
        conn.execute("INSERT INTO materials(kind,text,lang) VALUES ('reply','keep me','en')")
        conn.execute("INSERT INTO interactions(account_id,action,tweet_id,sent_at) VALUES (1,'reply','kept-ledger','2026-09-22T00:00:00Z')")
        conn.commit()
    assert queue._delete_selected({q1: ("pending", 1), q2: ("pending", 1), q3: ("sent", 1), sending: ("sending", 1)}) == (3, 1)
    assert ids("review_queue") == {sibling, sending}
    with get_conn() as conn:
        states = dict(conn.execute("SELECT id,process_status FROM target_tweets"))
        assert states == {lone: "no_match", shared: "queued", sent: "queued"}
        assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == 1
