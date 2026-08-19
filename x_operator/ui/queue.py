"""审核队列（design-v1.1 §8.2）：核心页。逐条卡片，可编辑文案、批准/跳过/拉黑。"""
from __future__ import annotations

from nicegui import ui

from ..db.database import get_conn, utcnow_iso
from .layout import shell


def _load(status: str):
    with get_conn() as conn:
        items = conn.execute(
            "SELECT rq.*, a.handle AS acc_handle, tt.author_handle, tt.author_id, tt.text AS tgt_text, "
            "tt.text_zh, tt.tweet_id AS tgt_tweet_id "
            "FROM review_queue rq JOIN accounts a ON a.id=rq.account_id "
            "LEFT JOIN target_tweets tt ON tt.id=rq.target_tweet_id "
            "WHERE rq.status=? ORDER BY rq.created_at ASC", (status,)).fetchall()
    return items


def _approve(item_id: int, text: str, refresh):
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET final_text=?, status='approved', decided_at=? WHERE id=? AND status='pending'",
                     (text, utcnow_iso(), item_id))
        conn.commit()
    ui.notify("已批准，等待分发发送", type="positive")
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


def register(jobs) -> None:
    @ui.page("/queue")
    def queue_page():
        with shell("/queue"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("审核队列").classes("text-2xl font-bold")
                status_sel = ui.select(
                    {"pending": "待审核", "approved": "待发送", "sent": "已发送",
                     "skipped": "已跳过", "failed": "失败", "expired": "已过期"},
                    value="pending").props("dense outlined")
                ui.button("触发发送（Mock）", on_click=lambda: (_dispatch(jobs), render())).props("outline")

            body = ui.column().classes("w-full gap-3")

            def render():
                body.clear()
                items = _load(status_sel.value)
                with body:
                    if not items:
                        ui.label("此状态下暂无条目 🎉").classes("text-gray-400")
                        return
                    for it in items:
                        _card(it, render)

            status_sel.on("update:model-value", lambda e: render())
            render()
            ui.timer(5.0, render)


def _weighted_len(text: str) -> int:
    # 简化的 X 权重长度：CJK/全角算 2，其余算 1
    n = 0
    for ch in text:
        n += 2 if ord(ch) > 0x1100 else 1
    return n // 1  # 近似


def _card(it, refresh):
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.badge(f"@{it['acc_handle']}").classes("bg-slate-600")
            ui.badge("回复" if it["action_type"] == "reply" else "发帖").classes("bg-blue-600")
            if it["is_auto_translated"]:
                ui.badge("自动翻译，请重点检查").classes("bg-yellow-600")
            if "http://" in (it["final_text"] or "") or "https://" in (it["final_text"] or ""):
                ui.badge("含链接（计费约 $0.20）").classes("bg-gray-500")

        if it["action_type"] == "reply" and it["tgt_text"]:
            with ui.card().classes("bg-slate-50 w-full"):
                ui.label(f"@{it['author_handle']} 的推文").classes("text-xs text-gray-500")
                ui.label(it["tgt_text"]).classes("text-sm")
                if it["text_zh"]:
                    ui.label("中文：" + it["text_zh"]).classes("text-xs text-gray-500")

        if it["llm_reason"]:
            conf = f"（置信度 {it['llm_confidence']:.2f}）" if it["llm_confidence"] is not None else ""
            with ui.expansion(f"AI 理由 {conf}").classes("w-full"):
                ui.label(it["llm_reason"])

        ta = ui.textarea(value=it["final_text"]).classes("w-full").props("outlined autogrow")
        wl_label = ui.label("").classes("text-xs")

        def update_len():
            wl = _weighted_len(ta.value or "")
            wl_label.text = f"约 {wl}/280 字符"
            wl_label.classes(replace="text-xs " + ("text-red-500" if wl > 280 else "text-gray-400"))
        ta.on("update:model-value", lambda e: update_len())
        update_len()

        with ui.row().classes("gap-2"):
            if it["status"] == "pending":
                ui.button("批准", on_click=lambda: _approve(it["id"], ta.value, refresh)).props("color=primary")
                ui.button("跳过", on_click=lambda: _skip(it["id"], refresh)).props("outline")
                if it["action_type"] == "reply":
                    ui.button("跳过并拉黑作者",
                              on_click=lambda: _skip_blacklist(it["id"], it["author_id"], it["author_handle"], refresh)
                              ).props("color=negative outline")
            else:
                ui.label(f"状态：{it['status']}" + (f" · {it['skip_reason']}" if it["skip_reason"] else "")
                         + (f" · 错误：{it['error_msg']}" if it["error_msg"] else "")).classes("text-sm text-gray-500")
                if it["sent_tweet_id"]:
                    ui.label(f"发送 id：{it['sent_tweet_id']}").classes("text-xs text-gray-400")


def _dispatch(jobs):
    try:
        n = jobs.dispatcher.tick()
        ui.notify(f"本轮发送 {n} 条（Mock）", type="positive")
    except Exception as e:
        ui.notify(f"发送出错：{e}", type="negative")
