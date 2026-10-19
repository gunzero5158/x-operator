"""Snapshot-based batch deletion for configuration lists."""
import sqlite3

from nicegui import run, ui

from ..adapters import factory
from ..core.accounts import delete_account
from ..db.database import get_conn
from .batch_bar import BatchBar
from .i18n import t as _tr
from .layout import confirm, notify_long

TABLES = {'watched': ('watched_users', 'handle'), 'rules': ('search_rules', 'name'),
          'accounts': ('accounts', 'handle')}
TITLES = {'watched': '删除选中的 {count} 个监控推主？', 'rules': '删除选中的 {count} 条搜索规则？',
          'accounts': '删除选中的 {count} 个账号？'}
DETAILS = {
    'watched': '停止后续监控；已抓取记录和已生成任务会保留。正在运行的监控可能仍会完成本轮。此操作不可撤销。',
    'rules': '移除这些搜索规则并停止后续处理；已抓取记录和已生成任务会保留。已发出的网络请求无法撤回。此操作不可撤销。',
    'accounts': '逐个检查关联任务，不能删除的账号会保留并说明原因。可删除账号的凭据会清空；发送历史和去重账本会保留，规则中的账号引用会清理。此操作不可撤销。',
}


def delete_records(kind, records):
    table, identity = TABLES[kind]
    done, notes = set(), []
    for item_id, expected in records.items():
        try:
            with get_conn() as conn:
                # Account reference checks and deletion must share a write lock.
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute(f'SELECT * FROM {table} WHERE id=?', (item_id,)).fetchone()
                if current is None or any(current[key] != expected[key] for key in (identity, 'created_at')):
                    conn.rollback()
                    notes.append((expected[identity], '记录已改变或已删除'))
                    continue
                if kind == 'accounts':
                    result, message = delete_account(item_id)
                    if result == 'blocked':
                        conn.rollback()
                        notes.append((expected[identity], message))
                        continue
                else:
                    conn.execute(f'DELETE FROM {table} WHERE id=?', (item_id,))
                    conn.commit()
                done.add(item_id)
            if kind == 'accounts':
                factory.invalidate(item_id)
        except sqlite3.Error:
            notes.append((expected[identity], '删除失败，请稍后重试'))
    return done, notes


class AdminBatch:
    def __init__(self, kind, rows, refresh):
        self.kind = kind
        self.records = {row['id']: dict(row) for row in rows}
        self.selected = set()
        self.boxes = {}
        self.updating = False

        async def execute(_action):
            records = {i: self.records[i] for i in self.selected}
            names = [r[TABLES[kind][1]] for r in records.values()]
            preview = '\n'.join(names[:20]) + ('\n…' if len(names) > 20 else '')
            if not await confirm(_tr(TITLES[kind], count=len(records)),
                                 _tr(DETAILS[kind]) + '\n\n' + preview,
                                 ok_label=_tr('删除选中')):
                return
            done, notes = await run.io_bound(delete_records, kind, records)
            message = _tr('已删除 {done} 条，保留 {kept} 条', done=len(done), kept=len(records) - len(done))
            for name, reason in notes:
                message += '\n' + name + '：' + _tr(reason)
            if not self.bar.container.client.is_deleted:
                notify_long(message, ok=not notes)
                refresh()

        self.bar = BatchBar({'delete': _tr('批量删除')}, lambda: self.select(True), lambda: self.select(False),
                            execute, lambda _: bool(self.selected), marker=kind + '-batch-action',
                            descriptions={'delete': DETAILS[kind]}, select_page=lambda: self.select(True))

    def select(self, value):
        self.selected = set(self.records) if value else set()
        self.updating = True
        try:
            for checkbox in self.boxes.values():
                checkbox.set_value(value)
        finally:
            self.updating = False
        self.bar.update(len(self.selected))

    def checkbox(self, item_id):
        def change(event):
            if self.updating:
                return
            if event.value:
                self.selected.add(item_id)
            else:
                self.selected.discard(item_id)
            self.bar.update(len(self.selected))
        self.boxes[item_id] = ui.checkbox(_tr('选择'), on_change=change).mark(f'{self.kind}-select-{item_id}')
