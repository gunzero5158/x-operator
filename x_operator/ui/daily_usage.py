"""Stable, paginated dashboard panels: refresh data without replacing controls."""
from math import ceil

from nicegui import ui

from ..core.account_usage import daily_account_usage
from .i18n import t as _tr
from .layout import fmt_time


def _clamp_page(table, count):
    pagination = dict(table.pagination)
    size = pagination.get("rowsPerPage", 10) or 10
    pagination["page"] = min(pagination.get("page", 1), max(1, ceil(count / size)))
    table.pagination = pagination


class DailyUsagePanel:
    def __init__(self):
        self.rows = []
        with ui.card().classes("xo-daily-usage w-full"):
            with ui.row().classes("w-full items-center justify-between gap-3"):
                ui.label(_tr("账号今日发送与额度")).classes("font-semibold")
                with ui.row().classes("items-center gap-2"):
                    ui.label(_tr("每 5 秒刷新 · 按账号时区")).classes("text-xs text-gray-500")
                    ui.button(_tr("刷新"), icon="refresh", on_click=self.refresh).props("flat dense")
            self.summary = ui.label().classes("xo-usage-summary")
            with ui.row().classes("w-full items-center gap-3"):
                self.search = ui.input(_tr("搜索账号或显示名"), on_change=self.filter_changed) \
                    .props("outlined dense clearable").classes("xo-usage-search")
                self.scope = ui.select({"all": _tr("全部账号"), "active": _tr("启用中"),
                                        "limited": _tr("至少一项已达限额"), "sent": _tr("今日有发送"),
                                        "inactive": _tr("暂停或凭据失效")}, value="all", label=_tr("筛选"),
                                       on_change=self.filter_changed).props("outlined dense").classes("xo-usage-scope")
                self.result_count = ui.label().classes("text-xs text-gray-500")
            columns = [("handle", "账号"), ("status_label", "状态"), ("post_used", "主帖已发 / 上限"),
                       ("post_remaining", "主帖剩余"), ("reply_used", "回帖已发 / 上限"),
                       ("reply_remaining", "回帖剩余"), ("day", "当地日期 / 时区")]
            self.table = ui.table(columns=[{"name": key, "field": key, "label": _tr(label),
                                            "align": "left", "sortable": True} for key, label in columns], rows=[],
                                  row_key="id", pagination={"page": 1, "rowsPerPage": 10, "sortBy": "handle", "descending": False}) \
                .classes("xo-usage-table w-full").props(':rows-per-page-options="[10,25,50]" flat')
            self.table.add_slot("body-cell-handle", '''
                <q-td :props="props"><a :href="'/queue?account=' + props.row.id">@{{ props.row.handle }}</a>
                <div class="xo-usage-secondary ellipsis" style="max-width:180px">{{ props.row.display_name }}</div></q-td>''')
            for action in ("post", "reply"):
                self.table.add_slot(f"body-cell-{action}_used", '''
                    <q-td :props="props"><div class="xo-usage-value">{{ props.row.ACTION_used }} / {{ props.row.ACTION_limit }}</div>
                    <q-linear-progress size="3px" rounded class="q-mt-xs" style="width:100px"
                      :value="props.row.ACTION_limit > 0 ? Math.min(1, props.row.ACTION_used / props.row.ACTION_limit) : 1"
                      :color="props.row.ACTION_remaining === 0 ? 'warning' : 'primary'" />
                    <div v-if="props.row.adjusted" class="xo-usage-secondary">{{ props.row.ACTION_configured }}</div></q-td>'''.replace("ACTION", action))
                self.table.add_slot(f"body-cell-{action}_remaining", '''
                    <q-td :props="props"><span :class="{'xo-usage-exhausted': props.value === 0}">{{ props.value }}</span></q-td>''')
            self.table.add_slot("body-cell-day", '''
                <q-td :props="props">{{ props.row.day }}<div class="xo-usage-secondary">{{ props.row.timezone }}</div></q-td>''')
            self.empty = ui.label(_tr("没有符合条件的账号，请清空搜索或切换筛选。")).classes("text-sm text-gray-500")
            with ui.expansion(_tr("统计口径与限额说明"), icon="info_outline").classes("xo-help w-full text-sm"):
                ui.label(_tr("只统计本工具成功发送的主帖和回帖，以各账号当地零点为界；失败、待发送不计入。上限读取当前设置，养号期使用发送器实际生效的额度；设置原值显示在下方。调低上限后已发送数量不变，剩余最低为 0。剩余量不代表立即可发送，仍受账号状态、冷却与活跃时段等限制。"))
        self.refresh()

    def refresh(self):
        self.rows = daily_account_usage()
        self.summary.set_text(_tr("{accounts} 个账号 · 今日主帖 {posts} 条 · 回帖 {replies} 条 · {limited} 个账号有额度已用完",
                                  accounts=len(self.rows), posts=sum(r["post_used"] for r in self.rows),
                                  replies=sum(r["reply_used"] for r in self.rows),
                                  limited=sum(r["post_remaining"] == 0 or r["reply_remaining"] == 0 for r in self.rows)))
        self.apply_filter()

    def filter_changed(self):
        self.table.pagination = {**self.table.pagination, "page": 1}
        self.apply_filter()

    def apply_filter(self):
        query = (self.search.value or "").strip().lstrip("@").casefold()
        scope = self.scope.value
        labels = {"active": _tr("启用"), "paused": _tr("已暂停"), "auth_error": _tr("凭据失效")}
        rows = []
        for row in self.rows:
            if query and query not in row["handle"].casefold() and query not in row["display_name"].casefold():
                continue
            if scope == "active" and row["status"] != "active":
                continue
            if scope == "inactive" and row["status"] == "active":
                continue
            if scope == "limited" and row["post_remaining"] > 0 and row["reply_remaining"] > 0:
                continue
            if scope == "sent" and row["post_used"] + row["reply_used"] == 0:
                continue
            rows.append({**row, "status_label": labels[row["status"]],
                         "post_configured": _tr("设置 {limit} · 养号期", limit=row["daily_post_limit"]),
                         "reply_configured": _tr("设置 {limit} · 养号期", limit=row["daily_reply_limit"])})
        _clamp_page(self.table, len(rows))
        self.table.update_rows(rows)
        self.empty.set_visibility(not rows)
        self.result_count.set_text(_tr("显示 {count} / {total} 个账号", count=len(rows), total=len(self.rows)))


class RecentErrorsPanel:
    def __init__(self):
        with ui.card().classes("xo-recent-errors w-full"):
            self.expansion = ui.expansion(_tr("最近异常"), icon="error_outline").classes("w-full")
            with self.expansion:
                ui.label(_tr("最近 20 条失败调用，每页 5 条；点击详情查看完整错误。")).classes("text-xs text-gray-500")
                self.table = ui.table(columns=[{"name": key, "field": key, "label": _tr(key), "align": "left"}
                                               for key in ("时间", "来源", "说明", "详情")], rows=[], row_key="id", pagination=5) \
                    .classes("xo-errors-table w-full").props('flat :rows-per-page-options="[5,10,20]"')
                self.table.add_slot("body-cell-说明", '''<q-td :props="props"><div class="xo-error-preview">{{ props.value }}</div></q-td>''')
                self.table.add_slot("body-cell-详情", '''<q-td :props="props"><q-btn flat dense color="primary"
                    :label="props.value" @click="$parent.$emit('error_detail', props.row)" /></q-td>''')
                self.table.on("error_detail", lambda event: self.show_detail(event.args))
                self.empty = ui.label(_tr("暂无异常 ✨")).classes("text-sm text-gray-500")

    def refresh(self, stats):
        self.expansion.set_text(_tr("最近异常 · 近 24 小时 {count} 次", count=stats["fails"]))
        rows = [{"id": r["id"], "时间": fmt_time(r["created_at"]), "来源": r["endpoint"],
                 "说明": r["error"] or "", "详情": _tr("详情")} for r in stats["recent_fail"]]
        _clamp_page(self.table, len(rows))
        self.table.update_rows(rows)
        self.empty.set_visibility(not rows)

    def show_detail(self, row):
        with ui.dialog() as dialog, ui.card().classes("xo-error-dialog"):
            ui.label(_tr("异常详情")).classes("font-semibold")
            ui.label(f'{row["时间"]} · {row["来源"]}').classes("text-xs text-gray-500")
            ui.label(row["说明"]).classes("whitespace-pre-wrap break-words w-full xo-error-full")
            ui.button(_tr("关闭"), on_click=dialog.close).props("flat")
        dialog.on("hide", dialog.delete)
        dialog.open()
