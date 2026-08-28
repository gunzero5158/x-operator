"""抓取记录：监控/搜索抓回来的每一条推文都在这里——打分、理由、处理状态一目了然。

这一页回答「跑了监控/搜索之后到底抓到了什么、为什么没进审核队列」。
可对未匹配/已过滤的推文手动「重新匹配」，也可删除、拉黑作者、批量清理。
"""
from __future__ import annotations

from nicegui import ui

from ..db.database import get_conn, utcnow_iso
from .layout import (TARGET_STATUS_LABEL, confirm, fmt_time, run_job, shell,
                     tweet_link)

_LIMIT = 150


def _load(status: str, source: str):
    q = ("SELECT tt.*, sr.name AS rule_name, wu.handle AS watched_handle, "
         "(SELECT rq.id FROM review_queue rq WHERE rq.target_tweet_id=tt.id ORDER BY rq.id DESC LIMIT 1) AS queue_id, "
         "(SELECT rq.status FROM review_queue rq WHERE rq.target_tweet_id=tt.id ORDER BY rq.id DESC LIMIT 1) AS queue_status "
         "FROM target_tweets tt "
         "LEFT JOIN search_rules sr ON sr.id=tt.source_rule_id AND tt.source='search' "
         "LEFT JOIN watched_users wu ON wu.id=tt.source_rule_id AND tt.source='monitor' WHERE 1=1")
    args: list = []
    if status != "all":
        q += " AND tt.process_status=?"; args.append(status)
    if source != "all":
        q += " AND tt.source=?"; args.append(source)
    q += f" ORDER BY tt.fetched_at DESC, tt.id DESC LIMIT {_LIMIT}"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


def _counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT process_status, COUNT(*) AS c FROM target_tweets GROUP BY process_status").fetchall()
    return {r["process_status"]: r["c"] for r in rows}


def _delete(tid: int) -> str:
    with get_conn() as conn:
        ref = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE target_tweet_id=?", (tid,)).fetchone()["c"]
        if ref:
            return "该推文已进入审核队列，请先在「审核队列」里删除对应条目"
        conn.execute("DELETE FROM target_tweets WHERE id=?", (tid,))
        conn.commit()
    return ""


def _delete_bulk(statuses: list[str]) -> tuple[int, int]:
    """删除指定状态的抓取记录（跳过被审核队列引用的）。返回 (删除数, 保留数)。"""
    with get_conn() as conn:
        marks = ",".join("?" * len(statuses))
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM target_tweets WHERE process_status IN ({marks})", statuses).fetchall()]
    done = kept = 0
    for i in ids:
        if _delete(i):
            kept += 1
        else:
            done += 1
    return done, kept


def _blacklist(author_id: str, handle: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO blacklist(x_user_id, handle, reason, created_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(x_user_id) DO NOTHING", (author_id, handle or "", "抓取记录页手动拉黑", utcnow_iso()))
        conn.commit()


def register(jobs) -> None:
    @ui.page("/targets")
    def targets_page():
        with shell("/targets"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("抓取记录").classes("text-2xl font-bold")
                with ui.row().classes("items-center gap-2"):
                    status_f = ui.select(_status_options(), value="all").props("dense outlined")
                    source_f = ui.select({"all": "全部来源", "monitor": "监控推主", "search": "语义搜索"}, value="all").props("dense outlined")
                    ui.button("运行监控", icon="visibility", on_click=lambda: run_job(jobs.monitor.run_once, "监控", render)).props("outline dense")
                    ui.button("运行搜索", icon="manage_search", on_click=lambda: run_job(jobs.search.run_once, "搜索", render)).props("outline dense")
                    with ui.button(icon="delete_sweep").props("outline color=negative dense"):
                        with ui.menu():
                            ui.menu_item("清理已过滤 / 未匹配 / 已过期", on_click=lambda: clear(["filtered", "no_match", "expired"]))
                            ui.menu_item("清理全部抓取记录", on_click=lambda: clear(list(TARGET_STATUS_LABEL)))

            ui.label("监控/搜索抓到的每条推文都记录在此。「已进审核队列」的去审核队列处理；"
                     "「未匹配/已过滤」的可以点「重新匹配」再试一次（比如新加了素材之后）。").classes("text-xs text-gray-400")
            body = ui.column().classes("w-full gap-2")

            async def clear(statuses: list[str]):
                c = _counts()
                n = sum(c.get(s, 0) for s in statuses)
                if not n:
                    ui.notify("没有可清理的记录", type="info"); return
                if await confirm(f"删除 {n} 条抓取记录？", "已进入审核队列的记录会保留。", ok_label="删除"):
                    done, kept = _delete_bulk(statuses)
                    ui.notify(f"已删除 {done} 条" + (f"，{kept} 条因在审核队列中而保留" if kept else ""), type="positive")
                    render()

            async def rematch(tid: int):
                outcome = await run_job(lambda: jobs.match.rematch(tid), "重新匹配")
                if outcome is not None:
                    ui.notify(("已生成回复并进入审核队列：" if outcome.status == "queued" else "仍未匹配：") + outcome.reason,
                              type="positive" if outcome.status == "queued" else "warning", multi_line=True, close_button=True)
                render()

            def delete_one(tid: int):
                err = _delete(tid)
                ui.notify(err or "已删除", type="negative" if err else "positive")
                render()

            def blacklist(author_id: str, handle: str):
                _blacklist(author_id, handle)
                ui.notify(f"已拉黑 @{handle}，之后不再对其回复", type="warning")
                render()

            def render():
                body.clear()
                status_f.set_options(_status_options(), value=status_f.value)
                rows = _load(status_f.value, source_f.value)
                with body:
                    if not rows:
                        ui.label("还没有抓取记录。点上方「运行监控」或「运行搜索」试试。").classes("text-gray-400")
                        return
                    ui.label(f"最近 {len(rows)} 条" + ("（已达显示上限，可清理旧记录）" if len(rows) >= _LIMIT else "")).classes("text-xs text-gray-400")
                    for t in rows:
                        _card(t, rematch, delete_one, blacklist)

            status_f.on("update:model-value", lambda e: render())
            source_f.on("update:model-value", lambda e: render())
            render()


def _status_options() -> dict:
    c = _counts()
    opts = {"all": f"全部状态（{sum(c.values())}）"}
    for k, v in TARGET_STATUS_LABEL.items():
        opts[k] = f"{v}（{c.get(k, 0)}）"
    return opts


_STATUS_COLOR = {"queued": "bg-green-600", "no_match": "bg-orange-500", "filtered": "bg-gray-500",
                 "new": "bg-blue-500", "expired": "bg-gray-400"}


def _card(t, rematch, delete_one, blacklist):
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2 w-full"):
            src = f"监控 @{t['watched_handle']}" if t["source"] == "monitor" and t["watched_handle"] else \
                (f"搜索「{t['rule_name']}」" if t["rule_name"] else ("监控" if t["source"] == "monitor" else "搜索"))
            ui.badge(src).classes("bg-slate-600")
            ui.badge(TARGET_STATUS_LABEL.get(t["process_status"], t["process_status"])).classes(_STATUS_COLOR.get(t["process_status"], "bg-gray-500"))
            if t["llm_relevance_score"] is not None:
                sc = t["llm_relevance_score"]
                ui.badge(f"相关性 {sc}/10").classes("bg-emerald-600" if sc >= 7 else "bg-gray-500")
            if t["lang"]:
                ui.badge(t["lang"]).classes("bg-slate-400")
            ui.label(f"抓取于 {fmt_time(t['fetched_at'])} · 发推于 {fmt_time(t['tweet_created_at'])}").classes("text-xs text-gray-400")
            ui.space()
            ui.button(icon="delete", on_click=lambda: delete_one(t["id"])).props("flat dense round color=negative").tooltip("删除此记录")
        ui.label(f"@{t['author_handle'] or t['author_id']}").classes("text-xs text-gray-500")
        ui.label(t["text"]).classes("text-sm whitespace-pre-wrap")
        if t["text_zh"]:
            ui.label("中文：" + t["text_zh"]).classes("text-xs text-gray-500")
        if t["llm_relevance_reason"]:
            ui.label("说明：" + t["llm_relevance_reason"]).classes("text-xs text-gray-500")
        tweet_link(t["author_handle"], t["tweet_id"])
        with ui.row().classes("gap-2 items-center"):
            if t["process_status"] == "queued" and t["queue_id"]:
                ui.link(f"查看审核队列条目 #{t['queue_id']}（{t['queue_status']}）→", "/queue").classes("text-xs")
            elif t["process_status"] in ("no_match", "filtered", "expired", "new"):
                ui.button("重新匹配", icon="autorenew", on_click=lambda: rematch(t["id"])).props("flat dense")
            if t["author_id"]:
                ui.button("拉黑作者", on_click=lambda: blacklist(t["author_id"], t["author_handle"])).props("flat dense color=negative")
