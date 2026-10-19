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
    assert table in ("review_queue", "target_tweets", "accounts", "watched_users", "search_rules")
    with get_conn() as conn:
        return {row[0] for row in conn.execute(f"SELECT id FROM {table}")}


def batch_action(user, action):
    selector = next(e for e in user.find(kind=ui.select).elements if e._props.get("label") == "批量操作")
    with user:
        selector.set_value(action)
    return user.find(kind=ui.button, content="执行批量操作")


def cancel_dialog(user):
    buttons = {el for el in user.find(kind=ui.button).elements if el.text == "取消"}
    UserInteraction(user, buttons, None).click()


@pytest.mark.parametrize('kind', ['watched', 'rules', 'accounts'])
async def test_admin_batch_cancel_and_fixed_selection(user: User, pages, kind):
    from x_operator.ui import watched, rules, settings_page
    from x_operator.ui.admin_batch import TABLES
    table, name = TABLES[kind]
    def add_record(label):
        with get_conn() as conn:
            if kind == 'watched':
                conn.execute('INSERT INTO watched_users(handle,x_user_id) VALUES (?,?)', (label, label))
            elif kind == 'rules':
                conn.execute("INSERT INTO search_rules(name,keyword_query,semantic_criteria) VALUES (?,'test','test')", (label,))
            else:
                conn.execute("INSERT INTO accounts(handle,status,access_type) VALUES (?,'paused','unofficial')", (label,))
            conn.commit()
    if kind == 'watched':
        add_record('one'); add_record('two')
    linked_target = target(source='monitor' if kind == 'watched' else 'search', rule=1) if kind != 'accounts' else None
    linked_task = task(tid=linked_target) if linked_target else None
    module = {'watched': watched, 'rules': rules, 'accounts': settings_page}[kind]
    module.register(Jobs())
    await user.open('/settings' if kind == 'accounts' else '/' + kind)
    user.find(kind=ui.button, content='全选列表所有条目').click()
    await user.should_see('已选 2 条')
    user.find(kind=ui.button, content='执行批量操作').click()
    title = {'watched': '删除选中的 2 个监控推主？', 'rules': '删除选中的 2 条搜索规则？', 'accounts': '删除选中的 2 个账号？'}[kind]
    await user.should_see(title)
    cancel_dialog(user)
    assert len(ids(table)) == 2
    user.find(kind=ui.button, content='执行批量操作').click()
    await user.should_see(title)
    add_record('new-after-confirmation')
    user.find(kind=ui.button, content='删除选中').click()
    await user.should_see('已删除 2 条，保留 0 条')
    with get_conn() as conn:
        assert [r[0] for r in conn.execute(f'SELECT {name} FROM {table}')] == ['new-after-confirmation']
        if linked_target:
            assert conn.execute('SELECT id FROM target_tweets WHERE id=?', (linked_target,)).fetchone()
            assert conn.execute('SELECT id FROM review_queue WHERE id=?', (linked_task,)).fetchone()


def test_account_batch_preserves_blocked_and_history(pages):
    from x_operator.ui.admin_batch import delete_records
    sent = task('sent', account=2)
    with get_conn() as conn:
        blocked = conn.execute("INSERT INTO accounts(handle,status,access_type) VALUES ('blocked','paused','unofficial')").lastrowid
        conn.execute("UPDATE accounts SET credentials=? WHERE id=2", ('{"test":"secret"}',))
        conn.execute('UPDATE search_rules SET reply_account_id=2 WHERE id=1')
        conn.commit()
    with get_conn() as conn:
        records = {r['id']: dict(r) for r in conn.execute('SELECT * FROM accounts')}
    task('pending', account=blocked)  # A task added after selection still blocks deletion.
    done, notes = delete_records('accounts', records)
    assert done == {1, 2}
    assert len(notes) == 1 and notes[0][0] == 'blocked'
    with get_conn() as conn:
        assert conn.execute('SELECT 1 FROM accounts WHERE id=1').fetchone() is None
        retired = conn.execute('SELECT deleted_at,credentials,status FROM accounts WHERE id=2').fetchone()
        assert retired['deleted_at'] and retired['credentials'] == '{}' and retired['status'] == 'paused'
        assert conn.execute('SELECT deleted_at FROM accounts WHERE id=?', (blocked,)).fetchone()[0] is None
        assert conn.execute('SELECT id FROM review_queue WHERE id=?', (sent,)).fetchone()
        assert conn.execute('SELECT reply_account_id FROM search_rules WHERE id=1').fetchone()[0] is None


@pytest.mark.parametrize('kind', ['queue', 'targets'])
async def test_pagers_and_explicit_selection_scopes(user: User, pages, kind):
    for _ in range(63):
        task('failed') if kind == 'queue' else target('filtered')
    await user.open('/queue?status=failed' if kind == 'queue' else '/targets?status=filtered')
    assert len(user.find(kind=ui.checkbox).elements) == 25
    with user:
        next(iter(user.find(f'{kind}-bottom-size').elements)).set_value(50)
    assert len(user.find(kind=ui.checkbox).elements) == 50
    assert next(iter(user.find(f'{kind}-top-size').elements)).value == 50
    with user:
        next(iter(user.find(f'{kind}-bottom-page').elements)).set_value(1)
    assert len(user.find(kind=ui.checkbox).elements) == 13
    assert next(iter(user.find(f'{kind}-top-page').elements)).value == 1
    user.find(kind=ui.button, content='全选本页').click()
    await user.should_see('已选 13 条')
    user.find(kind=ui.button, content='全选列表所有条目').click()
    await user.should_see('已选 63 条')
    with user:
        next(iter(user.find(f'{kind}-top-page').elements)).set_value(0)
    await user.should_see('已选 63 条')
    user.find(kind=ui.button, content='全选本页').click()
    await user.should_see('已选 50 条')
    with user:
        next(iter(user.find(f'{kind}-top-page').elements)).set_value(1)
    assert all(not e.value for e in user.find(kind=ui.checkbox).elements)
    user.find(kind=ui.button, content='全选本页').click()
    await user.should_see('已选 13 条')
    batch_action(user, 'delete').click()
    await user.should_see('删除选中的 13 条任务？' if kind == 'queue' else '删除选中的 13 条抓取记录？')
    cancel_dialog(user)
    assert len(ids('review_queue' if kind == 'queue' else 'target_tweets')) == 63


async def test_queue_pagination_keeps_selection_and_hidden_draft(user: User, pages, monkeypatch):
    monkeypatch.setattr(queue, '_LIMIT', 2)
    all_ids = {task(), task(), task()}
    newest = max(all_ids)
    await user.open('/queue?status=pending')
    await user.should_see('第 1 / 2 页 · 共 3 条')
    editor = next(e for e in user.find(kind=ui.textarea).elements if e.value == 'example draft')
    # Identify the task by its editor and select it explicitly.
    with user:
        editor.set_value('edited on the first page')
    user.find(kind=ui.button, content='全选列表所有条目').click()
    user.find(f'queue-select-{newest}').click()
    await user.should_see('已选 2 条')
    batch_action(user, 'delete')
    user.find(kind=ui.button, content='下一页').click()
    await user.should_see('第 2 / 2 页 · 共 3 条')
    await user.should_see('已选 2 条')
    action = next(e for e in user.find(kind=ui.select).elements if e._props.get('label') == '批量操作')
    assert action.value == 'delete'
    user.find(kind=ui.button, content='上一页').click()
    await user.should_see('edited on the first page')
    checkbox = next(iter(user.find(f'queue-select-{newest}').elements))
    assert not checkbox.value
    user.find(kind=ui.button, content='全选列表所有条目').click()
    user.find(kind=ui.button, content='下一页').click()
    batch_action(user, 'approve').click()
    await user.should_see('对选中的 3 条任务执行「批准」？')
    user.find(kind=ui.button, content='确认执行').click()
    await user.should_see('已完成 3 条，未完成 0 条')
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='approved'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE final_text='edited on the first page'").fetchone()[0] == 1


async def test_queue_hidden_draft_does_not_overwrite_external_edit(user: User, pages, monkeypatch):
    monkeypatch.setattr(queue, '_LIMIT', 1)
    task()
    edited = task()
    await user.open('/queue?status=pending')
    with user:
        next(iter(user.find(kind=ui.textarea).elements)).set_value('local edit')
    user.find(kind=ui.button, content='下一页').click()
    with get_conn() as conn:
        conn.execute('UPDATE review_queue SET final_text=? WHERE id=?', ('external edit', edited))
        conn.commit()
    user.find(kind=ui.button, content='全选列表所有条目').click()
    batch_action(user, 'approve').click()
    await user.should_see('对选中的 2 条任务执行「批准」？')
    user.find(kind=ui.button, content='确认执行').click()
    await user.should_see('已完成 1 条，未完成 1 条')
    with get_conn() as conn:
        row = conn.execute('SELECT status,final_text FROM review_queue WHERE id=?', (edited,)).fetchone()
        assert tuple(row) == ('pending', 'external edit')


async def test_queue_batch_actions_include_statuses_on_other_pages(user: User, pages, monkeypatch):
    monkeypatch.setattr(queue, '_LIMIT', 1)
    approved = task('approved')
    task('pending')
    await user.open('/queue?status=unsent')
    user.find(kind=ui.button, content='全选列表所有条目').click()
    batch_action(user, 'revert').click()
    await user.should_see('对选中的 2 条任务执行「撤回到待审核」？')
    user.find(kind=ui.button, content='确认执行').click()
    await user.should_see('已完成 1 条，未完成 1 条')
    with get_conn() as conn:
        assert conn.execute('SELECT status FROM review_queue WHERE id=?', (approved,)).fetchone()[0] == 'pending'


@pytest.mark.parametrize("status", ["pending", "approved", "failed", "skipped", "expired", "sent", "unsent"])
async def test_queue_each_list_delete_only_selected(user: User, pages, status):
    actual = "pending" if status == "unsent" else status
    first, other = task(actual), task(actual)
    outside = task(actual, account=2)
    await user.open(f"/queue?status={status}&account=1")
    delete = batch_action(user, "delete")
    assert not next(iter(delete.elements)).enabled
    user.find(f"queue-select-{first}").click()
    await user.should_see("已选 1 条")
    delete.click()
    await user.should_see("删除选中的 1 条任务？")
    cancel_dialog(user)
    await user.should_not_see("删除选中的 1 条任务？")
    assert ids("review_queue") == {first, other, outside}
    await user.should_see("已选 1 条")
    batch_action(user, "delete").click()
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
    user.find(kind=ui.button, content="全选列表所有条目").click()
    await user.should_see("已选 3 条")
    user.find(kind=ui.button, content="取消全选").click()
    await user.should_see("已选 0 条")
    user.find(kind=ui.button, content="全选列表所有条目").click()
    batch_action(user, "delete").click()
    await user.should_see("删除选中的 3 条抓取记录？")
    cancel_dialog(user)
    await user.should_not_see("删除选中的 3 条抓取记录？")
    await user.should_see("已选 3 条")
    batch_action(user, "delete").click()
    await user.should_see("删除选中的 3 条抓取记录？")
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 3 条，保留 0 条")
    assert ids("target_tweets") == {outside, other_rule}


async def test_queue_select_all_preserves_edits_and_filter_clears_selection(user: User, pages, monkeypatch):
    hidden, first, second = task(), task(), task()
    other = task(account=2)
    monkeypatch.setattr(queue, "_LIMIT", 2)
    await user.open("/queue?account=1")
    editor = next(iter(user.find(kind=ui.textarea).elements))
    with user:
        editor.set_value("unsaved draft")
    user.find(kind=ui.button, content="全选列表所有条目").click()
    await user.should_see("已选 3 条")
    assert not editor.is_deleted and editor.value == "unsaved draft"
    account = min(user.find(kind=ui.select).elements, key=lambda el: el.id if el.label == "发送账号" else 100000)
    with user:
        account.set_value(2)
    UserInteraction(user, {account}, None).trigger("update:modelValue", {"value": account._values.index(2)})
    await user.should_see("已选 0 条")
    user.find(kind=ui.button, content="全选列表所有条目").click()
    batch_action(user, "delete").click()
    await user.should_see("删除选中的 1 条任务？")
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 1 条，保留 0 条")
    assert ids("review_queue") == {hidden, first, second}


async def test_queue_rechecks_after_confirmation_open(user: User, pages):
    changed, moved, stable = task(), task(), task()
    await user.open("/queue")
    user.find(kind=ui.button, content="全选列表所有条目").click()
    batch_action(user, "delete").click()
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


async def test_queue_all_results_can_exclude_one_and_ignore_new_items(user: User, pages, monkeypatch):
    hidden, first, second = task("failed"), task("failed"), task("failed")
    monkeypatch.setattr(queue, "_LIMIT", 2)
    await user.open("/queue?status=failed")
    user.find(kind=ui.button, content="全选列表所有条目").click()
    await user.should_see("已选 3 条")
    user.find(f"queue-select-{second}").click()
    await user.should_see("已选 2 条")
    added = task("failed")
    batch_action(user, "delete").click()
    await user.should_see("删除选中的 2 条任务？")
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 2 条，保留 0 条")
    assert ids("review_queue") == {second, added}


async def test_targets_all_results_can_exclude_one_and_ignore_new_items(user: User, pages, monkeypatch):
    hidden, first, second = target(), target(), target()
    monkeypatch.setattr(targets, "_LIMIT", 2)
    await user.open("/targets?status=filtered")
    user.find(kind=ui.button, content="全选列表所有条目").click()
    await user.should_see("已选 3 条")
    user.find(f"target-select-{second}").click()
    await user.should_see("已选 2 条")
    added = target()
    batch_action(user, "delete").click()
    await user.should_see("删除选中的 2 条抓取记录？")
    user.find(kind=ui.button, content="删除选中").click()
    await user.should_see("已删除 2 条，保留 0 条")
    assert ids("target_tweets") == {second, added}


async def test_targets_all_results_match_uses_selection_snapshot(user: User, pages, monkeypatch):
    selected = {target(), target(), target()}
    monkeypatch.setattr(targets, "_LIMIT", 2)
    received = []
    def matching(self, item_ids, progress, **kwargs):
        received.extend(item_ids)
    async def run_work(work, *args, **kwargs):
        work(lambda *args: None)
        ui.notify("mock matching completed")
    monkeypatch.setattr(type(Jobs().match), "rematch_many", matching)
    monkeypatch.setattr(targets, "run_job_with_progress", run_work)
    await user.open("/targets?status=filtered")
    user.find(kind=ui.button, content="全选列表所有条目").click()
    added = target()
    batch_action(user, "rematch").click()
    await user.should_see("对选中的 3 条抓取记录执行「批量自动匹配」？")
    cancel_dialog(user)
    assert not received
    batch_action(user, "rematch").click()
    await user.should_see("确认执行")
    user.find(kind=ui.button, content="确认执行").click()
    await user.should_see("mock matching completed")
    assert set(received) == selected and added not in received


async def test_targets_global_cleanup_is_explicit_and_keeps_new_records(user: User, pages):
    original = target()
    await user.open("/targets?status=queued")
    batch_action(user, "clear_all").click()
    await user.should_see("全局清理不受当前筛选和勾选影响")
    await user.should_see("删除 1 条抓取记录？")
    added = target()
    user.find(kind=ui.button, content="删除").click()
    await user.should_see("已删除 1 条，保留 0 条")
    assert ids("target_tweets") == {added}
