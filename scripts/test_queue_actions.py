"""Bulk queue actions and type filters: disposable DB, mock providers, no scheduler."""
from types import SimpleNamespace

import pytest
from nicegui import ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from scripts.test_batch_delete import pages, task, target, cancel_dialog
from x_operator.core import queue_actions
from x_operator.core.scheduler import Jobs
from x_operator.db.database import get_conn
from x_operator.ui import queue

pytest_plugins = ['nicegui.testing.user_plugin']


def row(item_id):
    with get_conn() as conn:
        return dict(conn.execute('SELECT * FROM review_queue WHERE id=?', (item_id,)).fetchone())


def records(*ids):
    return {i: row(i) for i in ids}


def edit(sql, args=()):
    with get_conn() as conn:
        conn.execute(sql, args)
        conn.commit()


def click(user, text):
    elements = {e for e in user.find(kind=ui.button).elements if e.text == text}
    assert elements, text
    UserInteraction(user, elements, None).click()


@pytest.mark.parametrize('action,status,destination', [
    ('restore', 'failed', 'pending'), ('approve', 'pending', 'approved'),
    ('skip', 'pending', 'skipped'), ('revert', 'approved', 'pending'),
    ('recheck', 'skipped', 'pending'), ('force', 'expired', 'pending'),
    ('force', 'skipped', 'pending'),
])
def test_each_action_only_changes_selected(pages, action, status, destination):
    selected, other = task(status), task(status)
    result = queue_actions.apply_selected(Jobs(), action, records(selected))
    assert result['done'] == {selected}
    assert row(selected)['status'] == destination
    assert row(other)['status'] == status


@pytest.mark.parametrize('action,status', [('restore','failed'), ('approve','pending'), ('skip','pending'), ('revert','approved'), ('recheck','skipped'), ('force','expired')])
def test_changed_status_or_account_is_preserved(pages, action, status):
    moved, sending, deleted = task(status), task(status), task(status)
    snapshot = records(moved, sending, deleted)
    edit('UPDATE review_queue SET account_id=2 WHERE id=?', (moved,))
    edit("UPDATE review_queue SET status='sending' WHERE id=?", (sending,))
    edit('DELETE FROM review_queue WHERE id=?', (deleted,))
    result = queue_actions.apply_selected(Jobs(), action, snapshot)
    assert not result['done'] and sum(result['reasons'].values()) == 3
    assert row(moved)['status'] == status and row(sending)['status'] == 'sending'


def test_restore_keeps_error_resets_retry_and_protects_ledger(pages):
    replied_target, fresh_target = target(), target()
    blocked, allowed = task('failed', tid=replied_target), task('failed', tid=fresh_target)
    edit("INSERT INTO interactions(account_id, action, tweet_id, sent_at) VALUES (1, 'reply', ?, '2026-01-01T00:00:00Z')", (str(replied_target),))
    edit("UPDATE review_queue SET error_msg='keep reason',retry_count=3,expires_at='2020-01-01T00:00:00Z' WHERE id=?", (allowed,))
    result = queue_actions.apply_selected(Jobs(), 'restore', records(blocked, allowed))
    assert result['done'] == {allowed}
    assert row(blocked)['status'] == 'failed'
    restored = row(allowed)
    assert restored['status'] == 'pending' and restored['retry_count'] == 0
    assert restored['error_msg'] == 'keep reason' and restored['expires_at'] > '2026-01-01'
    assert not restored['force_send']


def test_approval_validates_edited_text_and_concurrent_edits(pages):
    good, blank, long, changed = [task() for _ in range(4)]
    snapshot = records(good, blank, long, changed)
    edit("UPDATE review_queue SET final_text='changed elsewhere' WHERE id=?", (changed,))
    result = queue_actions.apply_selected(Jobs(), 'approve', snapshot, {good:'edited draft', blank:' ', long:'x'*1000, changed:'stale draft'})
    assert result['done'] == {good}
    assert row(good)['final_text'] == 'edited draft'
    assert all(row(i)['status'] == 'pending' for i in (blank, long, changed))
    assert row(changed)['final_text'] == 'changed elsewhere'


def test_mixed_unsent_action_leaves_ineligible_selected_tasks(pages):
    failed, pending, approved = task('failed'), task(), task('approved')
    result = queue_actions.apply_selected(Jobs(), 'restore', records(failed, pending, approved))
    assert result['done'] == {failed} and sum(result['reasons'].values()) == 2
    assert row(approved)['status'] == 'approved'


def test_guard_expected_account_checked_under_transaction(pages):
    jobs = Jobs()
    for action, status in [('restore_failed','failed'), ('recheck_skipped','skipped'), ('force_restore','expired')]:
        item_id = task(status, account=2)
        assert not getattr(jobs.guard, action)(item_id, expected_account_id=1)[0]
        assert row(item_id)['status'] == status


def test_type_filter_scopes_counts_ids_and_transfer(pages):
    reply = task('failed', tid=target())
    post, other = task('failed'), task('failed', account=2, tid=target())
    assert queue._matching_ids('failed', 1, 'reply') == [reply]
    assert [r['id'] for r in queue._load('failed', 1, 'post')] == [post]
    assert queue._counts(1, 'reply')['failed'] == 1
    assert queue._counts(1, 'reply')[queue.UNSENT] == 1
    assert '（1）' in queue._account_filter_options('failed', 'reply')[1]
    snapshot = records(reply)
    edit("UPDATE accounts SET status='active' WHERE id=2")
    result = queue.transfer_items([reply], [2], snapshot)
    assert result['moved_ids'] == [reply] and row(post)['account_id'] == 1
    assert row(other)['account_id'] == 2


def test_transfer_rechecks_confirmation_snapshot(pages):
    moved, changed, stable = [task('failed') for _ in range(3)]
    snapshot = records(moved, changed, stable)
    edit('UPDATE review_queue SET account_id=2 WHERE id=?', (moved,))
    edit("UPDATE review_queue SET status='approved' WHERE id=?", (changed,))
    edit("UPDATE accounts SET status='active' WHERE id=2")
    result = queue.transfer_items(list(snapshot), [2], snapshot)
    assert result['moved_ids'] == [stable] and result['not_movable'] == 2
    assert row(changed)['account_id'] == 1


def test_verify_reports_missing_and_unknown_without_sending(pages):
    sent = [task('sent') for _ in range(3)]
    response = dict(zip(sent, ('ok', 'missing', 'unknown')))
    jobs = SimpleNamespace(dispatcher=SimpleNamespace(verify_item=lambda item_id: response[item_id]))
    result = queue_actions.apply_selected(jobs, 'verify', records(*sent))
    assert result['done'] == set(sent[:2])
    assert result['outcomes'] == {'ok':1, 'missing':1, 'unknown':1}
    assert all(row(i)['status'] == 'sent' for i in sent)


@pytest.mark.parametrize('status,action', [('pending','approve'), ('approved','revert'), ('failed','restore'), ('skipped','recheck'), ('expired','force'), ('sent','verify'), ('unsent','restore')])
async def test_status_specific_toolbar_and_cancel(user: User, pages, status, action):
    task('failed' if status == 'unsent' else status)
    await user.open(f'/queue?status={status}')
    selector = next(iter(user.find('queue-batch-action').elements))
    assert action in selector.options
    execute = next(iter(user.find(kind=ui.button, content='执行批量操作').elements))
    assert not execute.enabled
    click(user, '全选当前列表')
    with user:
        selector.set_value(action)
    assert execute.enabled
    click(user, '执行批量操作')
    await user.should_see('确认执行')
    cancel_dialog(user)
    await user.should_not_see('确认执行')
    await user.should_see('已选 1 条')


async def test_failed_type_filter_batch_restore_and_concurrent_move(user: User, pages):
    selected, moved = task('failed', tid=target()), task('failed', tid=target())
    post, other = task('failed'), task('failed', account=2, tid=target())
    await user.open('/queue?status=failed&account=1')
    with user:
        next(iter(user.find('queue-type-filter').elements)).set_value('reply')
    await user.should_see(f'queue-select-{selected}')
    await user.should_not_see(f'queue-select-{post}')
    click(user, '全选当前列表')
    click(user, '执行批量操作')
    await user.should_see('确认执行')
    edit('UPDATE review_queue SET account_id=2 WHERE id=?', (moved,))
    click(user, '确认执行')
    await user.should_see('已完成 1 条，未完成 1 条', retries=30)
    assert row(selected)['status'] == 'pending'
    assert all(row(i)['status'] == 'failed' for i in (moved, post, other))


async def test_pending_bulk_approval_uses_editor_and_preserves_unselected_draft(user: User, pages):
    selected, other = task(), task()
    await user.open('/queue')
    editors = sorted(user.find(kind=ui.textarea).elements, key=lambda e: e.id)
    # Queue order is newest first.
    with user:
        editors[0].set_value('unsaved other')
        editors[1].set_value('selected edited text')
    user.find(f'queue-select-{selected}').click()
    click(user, '执行批量操作')
    await user.should_see('确认执行')
    click(user, '确认执行')
    await user.should_see('已完成 1 条，未完成 0 条', retries=30)
    assert row(selected)['status'] == 'approved' and row(selected)['final_text'] == 'selected edited text'
    assert row(other)['final_text'] == 'example draft'
    assert next(iter(user.find(kind=ui.textarea).elements)).value == 'unsaved other'


async def test_targets_source_change_clears_incompatible_rule(user: User, pages):
    search = target(source='search', rule=1)
    monitor = target(source='monitor', rule=0)
    await user.open('/targets?status=filtered&source=search&rule=1')
    select = next(e for e in user.find(kind=ui.select).elements if isinstance(e.options, dict) and 'monitor' in e.options)
    with user:
        select.set_value('monitor')
    await user.should_see(f'target-select-{monitor}')
    await user.should_not_see(f'target-select-{search}')
    assert select.value == 'monitor'

async def test_clear_filtered_tasks_keeps_other_types_new_rows_and_changed_tasks(user: User, pages):
    first, changed = task('failed', tid=target()), task('failed', tid=target())
    post, other = task('failed'), task('failed', account=2, tid=target())
    await user.open('/queue?status=failed&account=1&action_type=reply')
    scope = next(e for e in user.find(kind=ui.select).elements if e._props.get('label') == '操作范围')
    with user:
        scope.set_value('filtered')
    click(user, '批量删除')
    await user.should_see('全部删除')
    new = task('failed', tid=target())
    edit("UPDATE review_queue SET status='sent' WHERE id=?", (changed,))
    click(user, '全部删除')
    await user.should_see('已删除 1 条，保留 1 条', retries=30)
    with get_conn() as conn:
        remaining = {r[0] for r in conn.execute('SELECT id FROM review_queue')}
    assert remaining == {changed, post, other, new}


async def test_recheck_filtered_tasks_respects_account_and_type(user: User, pages):
    selected = task('skipped', tid=target())
    post, other = task('skipped'), task('skipped', account=2, tid=target())
    await user.open('/queue?status=skipped&account=1&action_type=reply')
    scope = next(e for e in user.find(kind=ui.select).elements if e._props.get('label') == '操作范围')
    operation = next(e for e in user.find(kind=ui.select).elements if e._props.get('label') == '批量操作')
    with user:
        scope.set_value('filtered')
        operation.set_value('recheck')
    click(user, '执行批量操作')
    await user.should_see('确认执行')
    click(user, '确认执行')
    await user.should_see('已完成 1 条，未完成 0 条', retries=30)
    assert row(selected)['status'] == 'pending'
    assert row(post)['status'] == row(other)['status'] == 'skipped'


def test_force_selected_replies_does_not_duplicate_active_task(pages):
    tid = target()
    first, second = task('expired', tid=tid), task('expired', tid=tid)
    result = queue_actions.apply_selected(Jobs(), 'force', records(first, second))
    assert len(result['done']) == 1 and sum(result['reasons'].values()) == 1
    assert sorted([row(first)['status'], row(second)['status']]) == ['expired','pending']
