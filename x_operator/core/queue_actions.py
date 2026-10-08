"""Selected queue operations. Revalidate each task before changing its state."""
from __future__ import annotations

from . import textlimit
from ..db.database import get_conn, utcnow_iso

ACTION_STATUSES = {
    'restore': {'failed'},
    'approve': {'pending'},
    'skip': {'pending'},
    'revert': {'approved'},
    'recheck': {'skipped'},
    'force': {'skipped', 'expired'},
    'transfer': {'pending', 'approved', 'failed', 'skipped', 'expired'},
    'verify': {'sent'},
}


def transition(action: str, item_id: int, snapshot: dict, text: str | None = None) -> tuple[bool, str]:
    """Approval uses the edited draft; never overwrite a concurrently changed draft."""
    if action not in ('approve', 'skip', 'revert'):
        return False, '不支持的批量操作'
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        item = conn.execute('SELECT * FROM review_queue WHERE id=?', (item_id,)).fetchone()
        if item is None or any(item[k] != snapshot[k] for k in ('status', 'account_id', 'action_type')):
            conn.rollback()
            return False, '条目状态或账号已改变，请刷新后重试'
        if item['status'] not in ACTION_STATUSES[action]:
            conn.rollback()
            return False, '所选操作不适用于此状态'
        if action == 'approve':
            if item['final_text'] != snapshot['final_text']:
                conn.rollback()
                return False, '文案已被其他操作修改，请刷新后重新审核'
            text = (item['final_text'] if text is None else text) or ''
            if not text.strip():
                conn.rollback()
                return False, '文案不能为空'
            account = conn.execute('SELECT * FROM accounts WHERE id=?', (item['account_id'],)).fetchone()
            if account is None or account['deleted_at']:
                conn.rollback()
                return False, '发送账号不存在或已删除'
            if textlimit.over_by(text, account):
                conn.rollback()
                return False, '文案超出账号长度上限，请修改后重试'
            conn.execute("UPDATE review_queue SET final_text=?, status='approved', decided_at=? WHERE id=? AND status='pending'",
                         (text.strip(), utcnow_iso(), item_id))
        elif action == 'skip':
            conn.execute("UPDATE review_queue SET status='skipped', skip_reason='manual_skip', decided_at=? WHERE id=? AND status='pending'",
                         (utcnow_iso(), item_id))
        else:
            conn.execute("UPDATE review_queue SET status='pending', decided_at=NULL WHERE id=? AND status='approved'", (item_id,))
        conn.commit()
    return True, ''


def apply_selected(jobs, action: str, records: dict[int, dict], drafts: dict[int, str] | None = None) -> dict:
    """Only selected IDs; one failure does not abort the remaining records."""
    done, reasons, outcomes = set(), {}, {}
    for item_id, snapshot in records.items():
        try:
            with get_conn() as conn:
                current = conn.execute('SELECT * FROM review_queue WHERE id=?', (item_id,)).fetchone()
            if current is None or any(current[k] != snapshot[k] for k in ('status', 'account_id', 'action_type')):
                ok, detail = False, '条目状态或账号已改变，请刷新后重试'
            elif current['status'] not in ACTION_STATUSES.get(action, set()):
                ok, detail = False, '所选操作不适用于此状态'
            elif action in ('approve', 'skip', 'revert'):
                ok, detail = transition(action, item_id, snapshot, (drafts or {}).get(item_id))
            elif action == 'restore':
                ok, detail = jobs.guard.restore_failed(item_id, expected_account_id=snapshot['account_id'])
            elif action == 'recheck':
                ok, detail = jobs.guard.recheck_skipped(item_id, expected_account_id=snapshot['account_id'])
            elif action == 'force':
                ok, detail = jobs.guard.force_restore(item_id, expected_status=snapshot['status'], expected_account_id=snapshot['account_id'])
            elif action == 'verify':
                outcome = jobs.dispatcher.verify_item(item_id)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                ok, detail = outcome in ('ok', 'missing'), '未能回查，请稍后重试'
            else:
                ok, detail = False, '不支持的批量操作'
        except Exception:
            ok, detail = False, '处理失败，请稍后重试'
        if ok:
            done.add(item_id)
        else:
            reasons[detail] = reasons.get(detail, 0) + 1
    return {'done': done, 'reasons': reasons, 'outcomes': outcomes}
