"""审核队列（design-v1.1 §8.2）：核心页。逐条卡片，可编辑文案、换素材、批准/跳过/拉黑/删除。

自动刷新只在「条目集合变了」时才重绘，避免把用户正在编辑的文案冲掉。
已发送条目显示 X 上的链接与「回查核实」结果（发送接口返回成功 ≠ 一定真的发出去了）。
"""
from __future__ import annotations

from nicegui import run, ui

from ..db.database import get_conn, utcnow_iso
from .layout import (QUEUE_STATUS_LABEL, confirm, fmt_time, notify_long, run_job,
                     shell, tweet_link)
from .pickers import pick_material_dialog

ORIGIN_LABEL = {"ai_match": "AI 匹配素材", "manual": "手动选素材", "ai_write": "AI 撰写", "scheduled": "定时计划"}
VERIFY_LABEL = {"ok": ("已回查：X 上能查到 ✅", "text-green-600"),
                "missing": ("⚠ 发送接口返回成功，但回查时在 X 上查不到——可能被限制/静默丢弃，请点链接确认", "text-red-600"),
                "unknown": ("未能回查（网络/权限问题），请点链接确认", "text-gray-500")}


_LIMIT = 200


def _load(status: str):
    with get_conn() as conn:
        items = conn.execute(
            "SELECT rq.*, a.handle AS acc_handle, tt.author_handle, tt.author_id, tt.text AS tgt_text, "
            "tt.text_zh, tt.tweet_id AS tgt_tweet_id, tt.lang AS tgt_lang "
            "FROM review_queue rq JOIN accounts a ON a.id=rq.account_id "
            "LEFT JOIN target_tweets tt ON tt.id=rq.target_tweet_id "
            f"WHERE rq.status=? ORDER BY rq.created_at ASC LIMIT {_LIMIT}", (status,)).fetchall()
    return items


def _counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS c FROM review_queue GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}


def _approve(item_id: int, text: str, refresh):
    if not (text or "").strip():
        ui.notify("文案不能为空", type="negative"); return
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET final_text=?, status='approved', decided_at=? WHERE id=? AND status='pending'",
                     (text.strip(), utcnow_iso(), item_id))
        conn.commit()
    ui.notify("已批准，等待分发发送（可点右上「触发发送」立即尝试）", type="positive")
    refresh()


def _skip(item_id: int, refresh, reason: str = "manual_skip"):
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET status='skipped', skip_reason=?, decided_at=? WHERE id=?",
                     (reason, utcnow_iso(), item_id))
        conn.commit()
    ui.notify("已跳过", type="info")
    refresh()


def _skip_blacklist(item_id: int, author_id: str, author_handle: str, refresh):
    with get_conn() as conn:
        if author_id:
            conn.execute("INSERT INTO blacklist(x_user_id, handle, reason, created_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(x_user_id) DO NOTHING",
                         (author_id, author_handle or "", "审核时手动拉黑", utcnow_iso()))
        conn.execute("UPDATE review_queue SET status='skipped', skip_reason='blacklist', decided_at=? WHERE id=?",
                     (utcnow_iso(), item_id))
        conn.commit()
    ui.notify(f"已跳过并拉黑 @{author_handle}", type="warning")
    refresh()


def _revert_to_pending(item_id: int, refresh):
    with get_conn() as conn:
        cur = conn.execute("UPDATE review_queue SET status='pending', decided_at=NULL WHERE id=? AND status='approved'",
                           (item_id,))
        conn.commit()
    ui.notify("已撤回到待审核" if cur.rowcount else "该条目已开始发送，无法撤回", type="info" if cur.rowcount else "warning")
    refresh()


def _set_account(item_id: int, account_id: int) -> bool:
    """待审核条目临时改用别的账号发。"""
    with get_conn() as conn:
        cur = conn.execute("UPDATE review_queue SET account_id=? WHERE id=? AND status='pending'", (account_id, item_id))
        conn.commit()
    return cur.rowcount > 0


def _active_account_options() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, handle, is_primary FROM accounts WHERE status='active' ORDER BY is_primary DESC, id").fetchall()
    return {a["id"]: f"@{a['handle']}" + ("（主号）" if a["is_primary"] else "") for a in rows}


def _swap_material(item_id: int, material_id: int, text: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET material_id=?, final_text=?, origin='manual', llm_reason='人工换用素材' "
                     "WHERE id=? AND status='pending'", (material_id, text, item_id))
        conn.commit()


def _delete(item_id: int) -> None:
    with get_conn() as conn:
        row = conn.execute("SELECT target_tweet_id, status FROM review_queue WHERE id=?", (item_id,)).fetchone()
        conn.execute("DELETE FROM review_queue WHERE id=?", (item_id,))
        if row and row["target_tweet_id"] and row["status"] not in ("sent",):
            conn.execute("UPDATE target_tweets SET process_status='no_match', "
                         "llm_relevance_reason='审核队列条目已被手动删除，可重新选素材 / AI 撰写' WHERE id=? AND process_status='queued'",
                         (row["target_tweet_id"],))
        conn.commit()


def _delete_all(status: str) -> int:
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM review_queue WHERE status=?", (status,)).fetchall()]
    for i in ids:
        _delete(i)
    return len(ids)


def register(jobs) -> None:
    @ui.page("/queue")
    def queue_page():
        with shell("/queue"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("审核队列").classes("text-2xl font-bold")
                with ui.row().classes("items-center gap-2"):
                    status_sel = ui.select(_status_options(), value="pending").props("dense outlined")
                    clear_btn = ui.button("清空此状态", icon="delete_sweep").props("outline color=negative dense")
                    ui.button("触发发送", icon="send",
                              on_click=lambda: run_job(jobs.dispatcher.tick, "发送", render)).props("outline")

            ui.label("流程：待审核 → 批准 → 待发送 → 分发器按账号活跃时段/间隔自动发出（或点「触发发送」立即尝试）→ 已发送（自动回查 X 上是否真的存在）。"
                     ).classes("text-xs text-gray-400")
            body = ui.column().classes("w-full gap-3")
            # dirty：正在改文案的条目 id；busy：有弹窗开着。两者任一非空时自动刷新只更新计数、不重绘卡片，
            # 免得把用户改到一半的文案或开着的弹窗冲掉
            state = {"sig": None, "dirty": set(), "busy": 0}
            paused_hint = ui.label("").classes("text-xs text-orange-500")

            def signature(items) -> tuple:
                return tuple((it["id"], it["status"], it["verify_status"]) for it in items)

            def render(force: bool = True):
                items = _load(status_sel.value)
                sig = signature(items)
                if not force:
                    if sig == state["sig"]:
                        return
                    if state["dirty"] or state["busy"]:
                        paused_hint.text = "列表有更新，但你正在编辑/操作，暂不刷新（改完点批准或跳过后会自动刷新）"
                        return
                paused_hint.text = ""
                state["sig"] = sig
                state["dirty"].clear()
                status_sel.set_options(_status_options(), value=status_sel.value)
                body.clear()
                with body:
                    if not items:
                        ui.label("此状态下暂无条目 🎉").classes("text-gray-400")
                        return
                    if len(items) >= _LIMIT:
                        ui.label(f"只显示最早的 {_LIMIT} 条，处理掉一些后会显示更多").classes("text-xs text-gray-400")
                    for it in items:
                        _card(it, render, delete_cb, swap_cb, verify_cb, state["dirty"])

            async def delete_cb(it):
                if it["status"] == "pending" or it["status"] == "approved":
                    state["busy"] += 1
                    try:
                        ok = await confirm("删除这条待处理的条目？",
                                           "对应的抓取记录会退回「达标但未生成回复」，之后可在抓取记录页重新处理。")
                    finally:
                        state["busy"] -= 1
                    if not ok:
                        return
                _delete(it["id"])
                ui.notify("已删除", type="positive")
                render()

            async def swap_cb(it):
                state["busy"] += 1
                try:
                    res = await pick_material_dialog(it["tgt_text"] or "", it["tgt_lang"], title="换一条素材")
                finally:
                    state["busy"] -= 1
                if res is None:
                    return
                mid, text = res
                _swap_material(it["id"], mid, text)
                ui.notify("已换用所选素材", type="positive")
                render()

            async def verify_cb(it):
                ui.notify("正在到 X 上回查…", type="info")
                st = await run.io_bound(jobs.dispatcher.verify_item, it["id"])
                notify_long(VERIFY_LABEL.get(st, ("未知", ""))[0], ok=(st == "ok"), kind=None if st == "ok" else ("negative" if st == "missing" else "warning"))
                render()

            async def clear_all():
                st = status_sel.value
                n = _counts().get(st, 0)
                if not n:
                    ui.notify("没有可删除的条目", type="info"); return
                state["busy"] += 1
                try:
                    ok = await confirm(f"删除全部 {n} 条「{QUEUE_STATUS_LABEL.get(st, st)}」条目？",
                                       "已发送记录删除后不影响去重账本（不会重复回复同一推文）。", ok_label="全部删除")
                finally:
                    state["busy"] -= 1
                if ok:
                    _delete_all(st)
                    ui.notify(f"已删除 {n} 条", type="positive")
                    render()
            clear_btn.on_click(clear_all)

            status_sel.on("update:model-value", lambda e: render())
            render()
            ui.timer(5.0, lambda: render(force=False))


def _status_options() -> dict:
    c = _counts()
    return {k: f"{v}（{c.get(k, 0)}）" for k, v in QUEUE_STATUS_LABEL.items() if k != "sending"} | \
        ({"sending": f"发送中（{c['sending']}）"} if c.get("sending") else {})


def _weighted_len(text: str) -> int:
    n = 0
    for ch in text:
        n += 2 if ord(ch) > 0x1100 else 1
    return n


def _card(it, refresh, delete_cb, swap_cb, verify_cb, dirty: set):
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2 w-full"):
            if it["status"] == "pending":
                opts = _active_account_options()
                if it["account_id"] not in opts:
                    opts = {it["account_id"]: f"@{it['acc_handle']}（未启用）", **opts}
                acc_sel = ui.select(opts, value=it["account_id"], label="发送账号").props("dense outlined").classes("w-44") \
                    .tooltip("这条由哪个账号发出；改了就按新账号的间隔/日上限/活跃时段发")
                acc_sel.on("update:model-value", lambda e: ui.notify("已改用 " + opts.get(acc_sel.value, "") + " 发送", type="positive")
                           if _set_account(it["id"], int(acc_sel.value)) else ui.notify("该条目已不是待审核状态", type="warning"))
            else:
                ui.badge(f"@{it['acc_handle']}").classes("bg-slate-600")
            ui.badge("回复" if it["action_type"] == "reply" else "发帖").classes("bg-blue-600")
            origin = it["origin"] or ("scheduled" if it["scheduled_post_id"] else "ai_match")
            ui.badge(ORIGIN_LABEL.get(origin, origin)).classes(
                "bg-purple-600" if origin == "ai_write" else ("bg-teal-600" if origin == "manual" else "bg-slate-500"))
            if it["is_auto_translated"]:
                ui.badge("自动翻译，请重点检查").classes("bg-yellow-600")
            if "http://" in (it["final_text"] or "") or "https://" in (it["final_text"] or ""):
                ui.badge("含链接（官方 API 计费约 $0.20；外链回复易被折叠）").classes("bg-gray-500")
            ui.label(f"#{it['id']} · {fmt_time(it['created_at'])}").classes("text-xs text-gray-400")
            if it["expires_at"] and it["status"] == "pending":
                ui.label(f"时效至 {fmt_time(it['expires_at'])}").classes("text-xs text-orange-400")
            ui.space()
            ui.button(icon="delete", on_click=lambda: delete_cb(it)).props("flat dense round color=negative").tooltip("删除此条目")

        if it["action_type"] == "reply" and it["tgt_text"]:
            with ui.card().classes("bg-slate-50 w-full"):
                ui.label(f"@{it['author_handle']} 的推文").classes("text-xs text-gray-500")
                ui.label(it["tgt_text"]).classes("text-sm whitespace-pre-wrap")
                if it["text_zh"]:
                    ui.label("中文：" + it["text_zh"]).classes("text-xs text-gray-500")
                tweet_link(it["author_handle"], it["tgt_tweet_id"])

        if it["llm_reason"]:
            conf = f"（置信度 {it['llm_confidence']:.2f}）" if it["llm_confidence"] is not None else ""
            with ui.expansion(f"生成说明 {conf}").classes("w-full"):
                ui.label(it["llm_reason"])

        editable = it["status"] == "pending"
        ta = ui.textarea(value=it["final_text"]).classes("w-full").props("outlined autogrow" + ("" if editable else " readonly"))
        wl_label = ui.label("").classes("text-xs")

        def update_len():
            wl = _weighted_len(ta.value or "")
            wl_label.text = f"约 {wl}/280 字符"
            wl_label.classes(replace="text-xs " + ("text-red-500" if wl > 280 else "text-gray-400"))
            if editable:
                if (ta.value or "") != (it["final_text"] or ""):
                    dirty.add(it["id"])
                else:
                    dirty.discard(it["id"])
        ta.on("update:model-value", lambda e: update_len())
        update_len()

        with ui.row().classes("gap-2 items-center flex-wrap"):
            if it["status"] == "pending":
                ui.button("批准", icon="check", on_click=lambda: _approve(it["id"], ta.value, refresh)).props("color=primary")
                if it["action_type"] == "reply":
                    ui.button("换素材", icon="swap_horiz", on_click=lambda: swap_cb(it)).props("outline").tooltip("从素材库另选一条替换当前文案")
                ui.button("跳过", on_click=lambda: _skip(it["id"], refresh)).props("outline")
                if it["action_type"] == "reply":
                    ui.button("跳过并拉黑作者",
                              on_click=lambda: _skip_blacklist(it["id"], it["author_id"], it["author_handle"], refresh)
                              ).props("color=negative outline")
            elif it["status"] == "approved":
                ui.label("已批准，等待分发器发送").classes("text-sm text-gray-500")
                ui.button("撤回到待审核", on_click=lambda: _revert_to_pending(it["id"], refresh)).props("flat")
            else:
                ui.label(f"状态：{QUEUE_STATUS_LABEL.get(it['status'], it['status'])}"
                         + (f" · {it['skip_reason']}" if it["skip_reason"] else "")
                         + (f" · 错误：{it['error_msg']}" if it["error_msg"] else "")).classes("text-sm text-gray-500")
        if it["status"] == "sent" and it["sent_tweet_id"]:
            with ui.row().classes("gap-2 items-center flex-wrap"):
                sid = str(it["sent_tweet_id"])
                if sid.isdigit():
                    ui.link("在 X 上查看已发出的这条 ↗", f"https://x.com/{it['acc_handle']}/status/{sid}", new_tab=True).classes("text-xs")
                else:
                    ui.label(f"发送 id：{sid}（旧演示数据，非真实）").classes("text-xs text-gray-400")
                ui.label(fmt_time(it["sent_at"])).classes("text-xs text-gray-400")
                text, cls = VERIFY_LABEL.get(it["verify_status"] or "unknown", VERIFY_LABEL["unknown"])
                ui.label(text).classes("text-xs " + cls)
                ui.button("重新回查", icon="fact_check", on_click=lambda: verify_cb(it)).props("flat dense")
