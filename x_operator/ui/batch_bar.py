"""Shared selection and batch-action controls for record lists."""
from nicegui import ui
from .i18n import t as _tr

SELECTION_HINT = '全选本页会替换之前的选择；全选列表所有条目包含当前筛选的所有页。可取消个别勾选，执行前会确认数量，新增记录不会自动加入。'


class BatchBar:
    def __init__(self, actions, select_all, clear, execute, eligible, *, marker, descriptions=None, select_page=None):
        self.eligible = eligible
        self.descriptions = descriptions or {}
        self.count = 0
        self.running = False
        with ui.column().classes('xo-batch-bar w-full gap-2') as self.container:
            with ui.row().classes('w-full items-center gap-3 flex-wrap'):
                with ui.row().classes('items-center gap-2 flex-wrap'):
                    self.select_page = ui.button(_tr('全选本页'), on_click=select_page).props('flat dense') if select_page else None
                    self.select_all = ui.button(_tr('全选列表所有条目'), on_click=select_all).props('flat dense')
                    self.clear = ui.button(_tr('取消全选'), on_click=clear).props('flat dense')
                    self.count_label = ui.label().classes('text-sm').props('role=status aria-live=polite')
                ui.space()
                with ui.row().classes('items-center gap-2 flex-wrap max-w-full'):
                    self.action = ui.select(actions, value=next(iter(actions), None), label=_tr('批量操作')) \
                        .props('dense outlined').style('width: 15rem; max-width: 100%').mark(marker)
                    async def run_action():
                        action = self.action.value
                        if self.running or not action or not self.eligible(action):
                            return
                        self.running = True
                        self.update(self.count)
                        try:
                            await execute(action)
                        finally:
                            self.running = False
                            if not self.container.is_deleted:
                                self.update(self.count)
                    self.execute = ui.button(_tr('执行批量操作'), icon='playlist_play', on_click=run_action).props('outline')
            self.hint = ui.label().classes('text-xs text-slate-500')
        self.action.on_value_change(lambda: self.update(self.count))
        self.update(0)

    def update(self, count):
        self.count = count
        self.count_label.text = _tr('已选 {p0} 条', p0=count)
        action = self.action.value
        self.hint.text = _tr(self.descriptions.get(action, SELECTION_HINT))
        self.execute.props('color=negative' if action and (action == 'delete' or action.startswith('clear_')) else 'color=primary')
        self.execute.set_enabled(bool(action) and not self.running and self.eligible(action))
        self.clear.set_enabled(bool(count) and not self.running)
        self.select_all.set_enabled(not self.running)
        if self.select_page is not None:
            self.select_page.set_enabled(not self.running)
        self.action.set_enabled(not self.running)
