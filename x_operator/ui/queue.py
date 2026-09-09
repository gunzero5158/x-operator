"""审核队列（design-v1.1 §8.2）：核心页。逐条卡片，可编辑文案、换素材、批准/跳过/拉黑/删除。

自动刷新只在「条目集合变了」时才重绘，避免把用户正在编辑的文案冲掉。
已发送条目显示 X 上的链接与「回查核实」结果（发送接口返回成功 ≠ 一定真的发出去了）。
"""
from __future__ import annotations

from nicegui import run, ui

from ..core import media, textlimit
from ..core.compliance import SKIP_REASON_LABEL
from ..db.database import get_conn, utcnow_iso
from .layout import (QUEUE_STATUS_LABEL, confirm, fmt_time, fmt_views, notify_long, run_job,
                     shell, tag, tag_legend, tweet_link)
from .media_widget import MediaField, media_badge, media_strip
from .pickers import pick_material_dialog

ORIGIN_LABEL = {"ai_match": "AI 匹配素材", "manual": "手动选素材", "ai_write": "AI 撰写", "scheduled": "定时发帖计划"}
VERIFY_LABEL = {"ok": ("已回查：X 上能查到 ✅", "text-green-600"),
                "missing": ("⚠ 发送接口返回成功，但回查时在 X 上查不到——可能被限制/静默丢弃，请点链接确认", "text-red-600"),
                "unknown": ("未能回查（网络/权限问题），请点链接确认", "text-gray-500")}


_LIMIT = 200


def _load(status: str):
    with get_conn() as conn:
        items = conn.execute(
            "SELECT rq.*, a.handle AS acc_handle, tt.author_handle, tt.author_id, tt.text AS tgt_text, "
            "tt.text_zh, tt.tweet_id AS tgt_tweet_id, tt.lang AS tgt_lang, tt.view_count AS tgt_views, "
            "tt.llm_relevance_score AS tgt_score, tt.tweet_created_at AS tgt_created_at, tt.source AS tgt_source, "
            "sr.min_llm_score AS rule_min "
            "FROM review_queue rq JOIN accounts a ON a.id=rq.account_id "
            "LEFT JOIN target_tweets tt ON tt.id=rq.target_tweet_id "
            "LEFT JOIN search_rules sr ON sr.id=tt.source_rule_id AND tt.source='search' "
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
        acc = conn.execute("SELECT a.* FROM accounts a JOIN review_queue rq ON rq.account_id=a.id WHERE rq.id=?", (item_id,)).fetchone()
    if acc is not None and textlimit.over_by(text, acc):
        ui.notify(textlimit.over_message(text, acc) + "。请删减，或点「AI 缩写」，或换一个 Premium 账号发", type="negative", multi_line=True, close_button=True, timeout=12000); return
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
        rows = conn.execute("SELECT id, handle, is_primary, is_premium FROM accounts WHERE status='active' ORDER BY is_primary DESC, id").fetchall()
    return {a["id"]: f"@{a['handle']}" + ("（主号）" if a["is_primary"] else "") + ("（会员）" if a["is_premium"] else "") for a in rows}


def _account_limits() -> dict:
    with get_conn() as conn:
        return {a["id"]: textlimit.limit_for(a) for a in conn.execute("SELECT id, is_premium FROM accounts").fetchall()}


def _shorten(jobs, item_id: int, text: str, account_id: int) -> tuple[str, str]:
    """审核队列里手动点「AI 缩写」。返回 (新正文, 说明)；失败时新正文为空。"""
    from ..core.matcher import extract_must_include
    with get_conn() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        lang = (conn.execute("SELECT tt.lang FROM review_queue rq LEFT JOIN target_tweets tt ON tt.id=rq.target_tweet_id WHERE rq.id=?",
                             (item_id,)).fetchone() or {"lang": ""})["lang"] or ""
    new, note = textlimit.fit(text, acc, jobs.llm, extract_must_include(text), lang)
    if new == text:
        return "", note or "正文没有超出上限，不需要缩写"
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET final_text=? WHERE id=? AND status='pending'", (new, item_id)); conn.commit()
    return new, note


def _swap_material(item_id: int, material_id: int, text: str) -> None:
    with get_conn() as conn:
        mat = conn.execute("SELECT media_files FROM materials WHERE id=?", (material_id,)).fetchone()
        conn.execute("UPDATE review_queue SET material_id=?, final_text=?, final_media_files=?, origin='manual', llm_reason='人工换用素材' "
                     "WHERE id=? AND status='pending'", (material_id, text, mat["media_files"] if mat else "[]", item_id))
        conn.commit()


def _set_media(item_id: int, files: list[str]) -> bool:
    """待审核条目改附件。"""
    with get_conn() as conn:
        cur = conn.execute("UPDATE review_queue SET final_media_files=? WHERE id=? AND status='pending'",
                           (media.dump_files(files), item_id))
        conn.commit()
    return cur.rowcount > 0


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
                    recheck_btn = ui.button("重新判断全部已跳过", icon="refresh").props("outline dense") \
                        .tooltip("逐条再查黑名单 / 是否已回复过 / 作者冷却；都不成立的放回待审核")
                    clear_btn = ui.button("清空此状态", icon="delete_sweep").props("outline color=negative dense")
                    ui.button("触发发送", icon="send",
                              on_click=lambda: run_job(jobs.dispatcher.tick, "发送", render)).props("outline")

            ui.label("流程：待审核 → 批准 → 待发送 → 分发器按账号活跃时段/间隔自动发出（或点「触发发送」立即尝试）→ 已发送（自动回查 X 上是否真的存在）。"
                     ).classes("text-xs text-gray-400")
            tag_legend(["account", "reply", "post", "source", "ai", "warn", "media", "metric"])
            with ui.expansion("卡片上的标签是什么意思？", icon="help_outline").classes("w-full text-sm"):
                ui.markdown(
                    "- **回复 / 发帖**：回复 = 回在别人推文下面；发帖 = 自己账号发主贴（来自定时发帖计划）。\n"
                    "- **来源**：AI 匹配素材 / 手动选素材 / AI 撰写 / 定时发帖计划——这条文案是怎么来的。\n"
                    "- **📎 附件**：发送时会随正文一起上传的配图 / 视频。\n"
                    "- **含链接**：正文里有 http 链接。只是提醒，不是这条的实际扣费：官方 API 通道发含链接的推文按 X 的定价约 $0.20/条"
                    "（小号 Cookie 通道免费）；而且在别人帖子下带外链容易被折叠或限流，回复类建议只 @ 不带链接。\n"
                    "- **自动翻译，请重点检查**：文案是机器翻译过来的，发之前多看一眼。\n"
                    "- **时效至**：待审核条目过了这个时间会自动标「已过期」（设置 → 合规参数「回复条目时效」）。"
                ).classes("text-xs text-gray-600")
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
                        _card(it, render, delete_cb, swap_cb, verify_cb, attach_cb, shorten_cb, state["dirty"], recheck_cb, force_cb, send_now_cb)

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

            async def attach_cb(it):
                state["busy"] += 1
                try:
                    files = await _attach_dialog(media.parse_files(it["final_media_files"]))
                finally:
                    state["busy"] -= 1
                if files is None:
                    return
                if _set_media(it["id"], files):
                    ui.notify("附件已更新" if files else "已去掉附件", type="positive")
                else:
                    ui.notify("该条目已不是待审核状态", type="warning")
                render()

            async def shorten_cb(it, text: str, account_id: int):
                if not jobs.llm.configured:
                    ui.notify("AI 缩写需要先到「设置 → LLM」配置网关", type="warning"); return
                ui.notify("AI 缩写中…", type="info")
                new, note = await run.io_bound(_shorten, jobs, it["id"], text, account_id)
                notify_long(note, ok=bool(new), kind=None if new else "warning")
                if new:
                    state["dirty"].discard(it["id"]); render()

            async def verify_cb(it):
                ui.notify("正在到 X 上回查…", type="info")
                st = await run.io_bound(jobs.dispatcher.verify_item, it["id"])
                notify_long(VERIFY_LABEL.get(st, ("未知", ""))[0], ok=(st == "ok"), kind=None if st == "ok" else ("negative" if st == "missing" else "warning"))
                render()

            def recheck_cb(it):
                ok, detail = jobs.guard.recheck_skipped(it["id"])
                ui.notify(("已放回待审核，可在「待审核」里批准" if ok else f"仍不通过：{detail}"),
                          type="positive" if ok else "warning", multi_line=True)
                render()

            async def force_cb(it):
                reason = SKIP_REASON_LABEL.get(it["skip_reason"] or "", it["skip_reason"] or "未记录原因")
                state["busy"] += 1
                try:
                    ok = await confirm("强制放回待审核？",
                                       f"当前跳过原因：{reason}。放行后这条会绕过作者冷却 / 黑名单 / 时效检查，批准即发；"
                                       "请确认你清楚为什么要这么做。", ok_label="放行", color="warning")
                finally:
                    state["busy"] -= 1
                if not ok:
                    return
                done, detail = jobs.guard.force_restore(it["id"])
                ui.notify(detail, type="positive" if done else "negative", multi_line=True)
                render()

            async def send_now_cb(it):
                ui.notify("正在发送…", type="info")
                ok, detail = await run.io_bound(jobs.dispatcher.send_now, it["id"])
                notify_long(detail, ok=ok, kind=None if ok else "warning")
                render()

            def recheck_all():
                n = _counts().get("skipped", 0)
                if not n:
                    ui.notify("没有已跳过的条目", type="info"); return
                res = jobs.guard.recheck_all_skipped()
                parts = [f"放回待审核 {res['restored']} 条", f"仍跳过 {res['still']} 条"]
                if res["reasons"]:
                    parts.append("；".join(f"{k} {v} 条" for k, v in res["reasons"].items()))
                notify_long("重新判断完成：" + "，".join(parts), ok=res["restored"] > 0, kind=None if res["restored"] else "info")
                render()
            recheck_btn.on_click(recheck_all)

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

            def on_status_change():
                recheck_btn.set_visibility(status_sel.value == "skipped")
                render()
            status_sel.on("update:model-value", lambda e: on_status_change())
            recheck_btn.set_visibility(False)
            render()
            ui.timer(5.0, lambda: render(force=False))


def _status_options() -> dict:
    c = _counts()
    return {k: f"{v}（{c.get(k, 0)}）" for k, v in QUEUE_STATUS_LABEL.items() if k != "sending"} | \
        ({"sending": f"发送中（{c['sending']}）"} if c.get("sending") else {})


async def _attach_dialog(initial: list[str]):
    """改附件的弹窗。返回新列表，取消返回 None。"""
    with ui.dialog() as dlg, ui.card().classes("w-[640px] max-w-[95vw] max-h-[92vh] overflow-auto"):
        ui.label("这条的配图 / 视频").classes("text-lg font-bold")
        mf = MediaField(initial, note="发送时用这条的发送账号上传。")

        def ok():
            err = media.check_set(mf.files)
            if err:
                ui.notify(err, type="negative"); return
            dlg.submit(list(mf.files))
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
            ui.button("保存附件", icon="save", on_click=ok).props("color=primary")
    dlg.open()
    return await dlg


def _card(it, refresh, delete_cb, swap_cb, verify_cb, attach_cb, shorten_cb, dirty: set, recheck_cb=None, force_cb=None, send_now_cb=None):
    files = media.parse_files(it["final_media_files"])
    limits = _account_limits()
    cur_acc = {"id": it["account_id"]}
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2 w-full"):
            if it["status"] == "pending":
                opts = _active_account_options()
                if it["account_id"] not in opts:
                    opts = {it["account_id"]: f"@{it['acc_handle']}（未启用）", **opts}
                acc_sel = ui.select(opts, value=it["account_id"], label="发送账号").props("dense outlined").classes("w-44") \
                    .tooltip("这条由哪个账号发出；改了就按新账号的间隔/日上限/活跃时段发")
                def on_acc_change(e):
                    if _set_account(it["id"], int(acc_sel.value)):
                        cur_acc["id"] = int(acc_sel.value); update_len()
                        ui.notify("已改用 " + opts.get(acc_sel.value, "") + " 发送", type="positive")
                    else:
                        ui.notify("该条目已不是待审核状态", type="warning")
                acc_sel.on("update:model-value", on_acc_change)
            else:
                tag(f"@{it['acc_handle']}", "account", "发送账号")
            tag("回复" if it["action_type"] == "reply" else "发帖", it["action_type"] if it["action_type"] in ("reply", "post") else "reply",
                "回复 = 回在别人推文下；发帖 = 自己账号发主贴")
            origin = it["origin"] or ("scheduled" if it["scheduled_post_id"] else "ai_match")
            tag("来源：" + ORIGIN_LABEL.get(origin, origin), "ai" if origin == "ai_write" else "source", "这条文案是怎么来的")
            if it["is_auto_translated"]:
                tag("自动翻译", "warn", "文案是机器翻译过来的，发之前重点检查")
            if it["force_send"]:
                tag("人工放行", "warn", "从「已跳过」强制放回来的：发送时不按作者冷却 / 黑名单 / 时效拦，也不会自动过期")
            media_badge(files)
            if "http://" in (it["final_text"] or "") or "https://" in (it["final_text"] or ""):
                tag("含链接", "metric").tooltip("正文里有外链。官方 API 通道发含链接推文约 $0.20/条（小号通道免费）；回复里带外链易被折叠。详见页顶「标签是什么意思」")
            ui.label(f"#{it['id']} · {fmt_time(it['created_at'])}").classes("text-xs text-gray-400")
            if it["expires_at"] and it["status"] == "pending":
                ui.label(f"时效至 {fmt_time(it['expires_at'])}").classes("text-xs text-orange-400")
            ui.space()
            ui.button(icon="delete", on_click=lambda: delete_cb(it)).props("flat dense round color=negative").tooltip("删除此条目")

        if it["action_type"] == "reply" and it["tgt_text"]:
            with ui.card().classes("bg-slate-50 w-full"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.label(f"@{it['author_handle']} 的推文").classes("text-xs text-gray-500")
                    # 与「抓取记录」页的卡片保持同一套小标签：相关性 / 语言 / 观看量 / 发推时间
                    if it["tgt_score"] is not None:
                        sc = it["tgt_score"]
                        thr = it["rule_min"] if it["rule_min"] is not None else 7
                        tag(f"相关性 {sc}/10" + (f"（达标线 {thr}）" if it["tgt_source"] == "search" else ""),
                            "metric_ok" if sc >= thr else "metric_bad", "AI 给的相关性分；绿 = 达到规则的达标分，红 = 没达到")
                    if it["tgt_lang"]:
                        tag(it["tgt_lang"], "metric", "推文语言")
                    if it["tgt_views"] is not None:
                        tag(f"👁 {fmt_views(it['tgt_views'])}", "metric", "抓取时的观看量")
                    if it["tgt_created_at"]:
                        ui.label(f"发推于 {fmt_time(it['tgt_created_at'])}").classes("text-xs text-gray-400")
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
            wl = textlimit.weighted_len(ta.value or "")
            lim = limits.get(cur_acc["id"], textlimit.FREE_LIMIT)
            over = wl > lim
            wl_label.text = f"{wl}/{lim} 单位" + ("（会员账号）" if lim >= 1000 else "（中日韩每字 2、链接 23；≈140 个汉字）") + \
                            ("  ⚠ 超出上限，批准前请删减或点「AI 缩写」" if over else "")
            wl_label.classes(replace="text-xs " + ("text-red-500" if over else "text-gray-400"))
            if editable and shorten_btn is not None:
                shorten_btn.set_visibility(over)
            if editable:
                if (ta.value or "") != (it["final_text"] or ""):
                    dirty.add(it["id"])
                else:
                    dirty.discard(it["id"])
        ta.on("update:model-value", lambda e: update_len())
        shorten_btn = None
        media_strip(files)

        with ui.row().classes("gap-2 items-center flex-wrap"):
            if it["status"] == "pending":
                ui.button("批准", icon="check", on_click=lambda: _approve(it["id"], ta.value, refresh)).props("color=primary")
                ui.button("附件" if not files else f"附件（{len(files)}）", icon="attach_file", on_click=lambda: attach_cb(it)).props("outline") \
                    .tooltip("给这条加 / 换 / 去掉配图和视频")
                shorten_btn = ui.button("AI 缩写", icon="compress", on_click=lambda: shorten_cb(it, ta.value or "", cur_acc["id"])).props("outline color=orange") \
                    .tooltip("让 AI 把正文缩到这个账号的长度上限以内（保留链接和 @）")
                if it["action_type"] == "reply":
                    ui.button("换素材", icon="swap_horiz", on_click=lambda: swap_cb(it)).props("outline").tooltip("从素材库另选一条替换当前文案")
                ui.button("跳过", on_click=lambda: _skip(it["id"], refresh)).props("outline")
                if it["action_type"] == "reply":
                    ui.button("跳过并拉黑作者",
                              on_click=lambda: _skip_blacklist(it["id"], it["author_id"], it["author_handle"], refresh)
                              ).props("color=negative outline")
            elif it["status"] == "approved":
                ui.label("已批准，等待分发器发送").classes("text-sm text-gray-500")
                ui.button("立即发送", icon="bolt", on_click=lambda: send_now_cb(it)).props("outline dense color=orange") \
                    .tooltip("不等活跃时段和发送间隔，现在就发这一条（账号日上限和合规检查照常）")
                ui.button("撤回到待审核", on_click=lambda: _revert_to_pending(it["id"], refresh)).props("flat")
            elif it["status"] == "skipped":
                reason = it["skip_reason"] or ""
                ui.label("已跳过 · " + SKIP_REASON_LABEL.get(reason, reason or "未记录原因")).classes("text-sm text-gray-500")
                ui.button("重新判断", icon="refresh", on_click=lambda: recheck_cb(it)).props("outline dense") \
                    .tooltip("按现在的情况把跳过规则再查一遍（黑名单 / 是否已回复过 / 作者冷却 / 时效）；都不成立就放回待审核")
                ui.button("强制放回待审核", icon="lock_open", on_click=lambda: force_cb(it)).props("outline dense color=orange") \
                    .tooltip("人工放行：不管跳过原因直接放回待审核，发送时也不再按冷却 / 黑名单 / 时效拦（已回复过的除外）")
            else:
                ui.label(f"状态：{QUEUE_STATUS_LABEL.get(it['status'], it['status'])}"
                         + (f" · {it['skip_reason']}" if it["skip_reason"] else "")
                         + (f" · 错误：{it['error_msg']}" if it["error_msg"] else "")).classes("text-sm text-gray-500")
        update_len()
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
