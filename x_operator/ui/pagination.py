"""Matching top/bottom controls for bounded record lists."""
from nicegui import ui
from .i18n import t as _tr


def pagination(page, size, total, change_page, change_size, *, marker):
    pages = max(1, (total + size - 1) // size)
    with ui.row().classes('xo-pagination w-full items-center gap-2 flex-wrap').mark(marker):
        ui.button(_tr('上一页'), on_click=lambda: change_page(page - 1)).props('flat').set_enabled(page > 0)
        with ui.row().classes('items-center gap-2 flex-nowrap'):
            ui.label(_tr('页码')).classes('text-sm whitespace-nowrap')
            selector = ui.select({i: str(i + 1) for i in range(pages)}, value=page) \
                .props('dense outlined').classes('w-24').mark(marker + '-page')
            selector.props['aria-label'] = _tr('页码')
        selector.on_value_change(lambda e: change_page(int(e.value)))
        ui.button(_tr('下一页'), on_click=lambda: change_page(page + 1)).props('flat').set_enabled(page + 1 < pages)
        with ui.row().classes('items-center gap-2 flex-nowrap'):
            ui.label(_tr('每页条数')).classes('text-sm whitespace-nowrap')
            sizes = ui.select(sorted({25, 50, 100, size}), value=size) \
                .props('dense outlined').classes('w-24').mark(marker + '-size')
            sizes.props['aria-label'] = _tr('每页条数')
        sizes.on_value_change(lambda e: change_size(int(e.value)))
        ui.label(_tr('第 {page} / {pages} 页 · 共 {count} 条', page=page + 1, pages=pages, count=total)).classes('text-sm')
