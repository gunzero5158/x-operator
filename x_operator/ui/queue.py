"""任务队列（design-v1.1 §8.2）：核心页。逐条卡片，可编辑文案、换素材、批准/跳过/拉黑/删除。

顶部可按状态 + 发送账号筛选；选了某个账号后能把筛出来的条目批量转给其他账号或批量删除（账号凭据失效时清理用）。

自动刷新只在「条目集合变了」时才重绘，避免把用户正在编辑的文案冲掉。
已发送条目显示 X 上的链接与「回查核实」结果（发送接口返回成功 ≠ 一定真的发出去了）。
"""
from __future__ import annotations

from nicegui import run, ui

from ..core import media, textlimit
from ..core.compliance import SKIP_REASON_LABEL
from ..core.langdetect import lang_name
from ..db.database import get_conn, utcnow_iso
from .layout import page_title, detail_text, preview_text
from .layout import (QUEUE_STATUS_LABEL, confirm, fmt_time, fmt_views, hint, llm_wait, notify_long, run_job,
                     shell, source_label, tag, tag_legend, tweet_link)
from .media_widget import MediaField, media_badge, media_strip
from .pickers import pick_material_dialog

ORIGIN_LABEL = {"ai_match": "AI 匹配素材", "manual": "手动选素材", "ai_write": "AI 撰写", "scheduled": "定时发帖计划"}
VERIFY_LABEL = {"ok": ("已回查：X 上能查到 ✅", "text-green-600"),
                "missing": ("⚠ 发送接口返回成功，但回查时在 X 上查不到——可能被限制/静默丢弃，请点链接确认", "text-red-600"),
                "unknown": ("未能回查（网络/权限问题），请点链接确认", "text-gray-500")}


_LIMIT = 200


# 各状态按「这个状态自己的时间」倒序：待审核看创建时间，已批准 / 发送中 / 失败 / 已跳过 / 已过期看决定时间，已发送看发送时间
_ORDER_BY = {
    "pending": "rq.created_at DESC, rq.id DESC",
    "sent": "COALESCE(rq.sent_at, rq.decided_at, rq.created_at) DESC, rq.id DESC",
}
_ORDER_DEFAULT = "COALESCE(rq.decided_at, rq.created_at) DESC, rq.id DESC"


# 状态筛选里的「未发送的全部」：还有可能发出去的几种状态放在一起看，账号失效时一次筛全
UNSENT = "unsent"
UNSENT_STATUSES = ("pending", "approved", "failed")
UNSENT_LABEL = "未发送的全部（待审核 + 待发送 + 失败）"
# 能批量转给别的账号的状态（发送中 / 已发送的不能动）
TRANSFERABLE = ("pending", "approved", "failed", "skipped", "expired")
ALL_ACCOUNTS = 0
ACC_STATUS_LABEL = {"active": "", "paused": "已暂停", "auth_error": "凭据失效"}
DELETED_LABEL = "已删除"


def _where(status: str, account_id: int = ALL_ACCOUNTS) -> tuple[str, list]:
    if status == UNSENT:
        sql, args = f"rq.status IN ({','.join('?' * len(UNSENT_STATUSES))})", list(UNSENT_STATUSES)
    else:
        sql, args = "rq.status=?", [status]
    if account_id:
        sql += " AND rq.account_id=?"; args.append(int(account_id))
    return sql, args


def _load(status: str, account_id: int = ALL_ACCOUNTS):
    order = _ORDER_BY.get(status, _ORDER_DEFAULT)
    where, args = _where(status, account_id)
    with get_conn() as conn:
        items = conn.execute(
            "SELECT rq.*, a.handle AS acc_handle, a.deleted_at AS acc_deleted, tt.author_handle, tt.author_id, tt.text AS tgt_text, "
            "tt.text_zh, tt.tweet_id AS tgt_tweet_id, tt.lang AS tgt_lang, tt.view_count AS tgt_views, "
            "tt.llm_relevance_score AS tgt_score, tt.tweet_created_at AS tgt_created_at, tt.source AS tgt_source, "
            "tt.source_rule_id AS tgt_rule_id, sr.min_llm_score AS rule_min, sr.name AS rule_name, sr.source_kind AS rule_kind, "
            "wu.handle AS watched_handle "
            "FROM review_queue rq JOIN accounts a ON a.id=rq.account_id "
            "LEFT JOIN target_tweets tt ON tt.id=rq.target_tweet_id "
            "LEFT JOIN search_rules sr ON sr.id=tt.source_rule_id AND tt.source='search' "
            "LEFT JOIN watched_users wu ON wu.id=tt.source_rule_id AND tt.source='monitor' "
            f"WHERE {where} ORDER BY {order} LIMIT {_LIMIT}", args).fetchall()
    return items


def _counts(account_id: int = ALL_ACCOUNTS) -> dict[str, int]:
    """各状态条数（选了账号就只数它的），另带 unsent = 未发送的全部。"""
    with get_conn() as conn:
        if account_id:
            rows = conn.execute("SELECT status, COUNT(*) AS c FROM review_queue WHERE account_id=? GROUP BY status", (int(account_id),)).fetchall()
        else:
            rows = conn.execute("SELECT status, COUNT(*) AS c FROM review_queue GROUP BY status").fetchall()
    out = {r["status"]: r["c"] for r in rows}
    out[UNSENT] = sum(out.get(k, 0) for k in UNSENT_STATUSES)
    return out


def _account_filter_options(status: str) -> dict:
    """账号筛选下拉：{0: 全部账号, id: @handle · 凭据失效（当前状态下 N 条）}。停用 / 失效的账号也列出来，正是要清理它们；
    已删除的账号只在当前状态下还有它的记录时列出。"""
    with get_conn() as conn:
        accs = conn.execute("SELECT id, handle, status, deleted_at FROM accounts ORDER BY (deleted_at IS NOT NULL), (status='active'), is_primary DESC, id").fetchall()
        where, args = _where(status)
        cnt = {r["account_id"]: r["c"] for r in conn.execute(
            f"SELECT rq.account_id, COUNT(*) AS c FROM review_queue rq WHERE {where} GROUP BY rq.account_id", args)}
    opts = {ALL_ACCOUNTS: "全部账号"}
    for a in accs:
        if a["deleted_at"] and not cnt.get(a["id"]):
            continue
        st = DELETED_LABEL if a["deleted_at"] else ACC_STATUS_LABEL.get(a["status"], a["status"])
        opts[a["id"]] = f"@{a['handle']}" + (f" · {st}" if st else "") + f"（{cnt.get(a['id'], 0)}）"
    return opts


def _matching_ids(status: str, account_id: int = ALL_ACCOUNTS) -> list[int]:
    """当前筛选下的全部条目 id（不受页面只显示 200 条的限制）。"""
    where, args = _where(status, account_id)
    with get_conn() as conn:
        return [r["id"] for r in conn.execute(f"SELECT rq.id FROM review_queue rq WHERE {where} ORDER BY rq.id", args).fetchall()]


def transfer_items(item_ids: list[int], target_ids: list[int]) -> dict:
    """把条目批量转给其他账号：选多个目标账号就按顺序平均分。只转 TRANSFERABLE 状态的（发送中 / 已发送跳过）；
    目标账号必须是启用中的。待发送的条目如果超出新账号的长度上限（免费账号 280 单位），退回待审核让人删减，不然发送时必失败。
    返回 {moved, per_account: {handle: n}, back_to_pending, not_movable, error}。"""
    res = {"moved": 0, "per_account": {}, "back_to_pending": 0, "not_movable": 0, "error": ""}
    with get_conn() as conn:
        accs = {a["id"]: a for a in conn.execute("SELECT * FROM accounts WHERE status='active'").fetchall()}
        targets = [accs[i] for i in dict.fromkeys(int(t) for t in target_ids) if i in accs]
        if not targets:
            res["error"] = "没有选启用中的目标账号"
            return res
        items = conn.execute(f"SELECT id, status, final_text FROM review_queue WHERE id IN ({','.join('?' * len(item_ids))}) ORDER BY id",
                             list(item_ids)).fetchall() if item_ids else []
        k = 0
        for it in items:
            if it["status"] not in TRANSFERABLE:
                res["not_movable"] += 1
                continue
            acc = targets[k % len(targets)]
            k += 1
            if it["status"] == "approved" and textlimit.over_by(it["final_text"] or "", acc):
                cur = conn.execute("UPDATE review_queue SET account_id=?, status='pending', decided_at=NULL WHERE id=? AND status='approved'",
                                   (acc["id"], it["id"]))
                res["back_to_pending"] += cur.rowcount
            else:
                cur = conn.execute("UPDATE review_queue SET account_id=? WHERE id=? AND status=?", (acc["id"], it["id"], it["status"]))
            if cur.rowcount:
                res["moved"] += 1
                res["per_account"][acc["handle"]] = res["per_account"].get(acc["handle"], 0) + 1
            else:
                res["not_movable"] += 1   # 刚好被分发器拿去发了
        conn.commit()
    return res


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
    """任务队列里手动点「AI 缩写」。返回 (新正文, 说明)；失败时新正文为空。"""
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
                         "llm_relevance_reason='任务队列条目已被手动删除，可重新选素材 / AI 撰写' WHERE id=? AND process_status='queued'",
                         (row["target_tweet_id"],))
        conn.commit()


def _delete_all(status: str, account_id: int = ALL_ACCOUNTS) -> int:
    ids = _matching_ids(status, account_id)
    for i in ids:
        _delete(i)
    return len(ids)


def register(jobs) -> None:
    @ui.page("/queue")
    def queue_page(status: str = "pending", account: int = ALL_ACCOUNTS):
        with shell("/queue"):
            if status not in QUEUE_STATUS_LABEL and status != UNSENT:
                status = "pending"
            if account not in _account_filter_options(status):
                account = ALL_ACCOUNTS
            with ui.row().classes("xo-page-heading w-full"):
                page_title("任务队列", "审核文案，安排每一次发布")
                with ui.row().classes("xo-toolbar w-full items-center gap-2"):
                    status_sel = ui.select(_status_options(account), value=status).props("dense outlined")
                    acc_filter = ui.select(_account_filter_options(status), value=account, label="发送账号") \
                        .props("dense outlined").classes("min-w-40") \
                        .tooltip("只看某个账号的条目；账号凭据失效时选它，再批量转给其他账号或批量删除")
                    recheck_btn = ui.button("重新判断全部已跳过", icon="refresh").props("outline dense") \
                        .tooltip("逐条再查黑名单 / 是否已回复过 / 作者冷却；都不成立的放回待审核")
                    transfer_btn = ui.button("批量转给其他账号", icon="swap_horiz").props("outline dense color=primary") \
                        .tooltip("把当前筛出来的全部条目改由别的启用账号发送（选多个就平均分）")
                    clear_btn = ui.button("批量删除", icon="delete_sweep").props("outline color=negative dense") \
                        .tooltip("删除当前筛选（状态 + 账号）下的全部条目，不只是页面上显示的")
                    ui.button("触发发送", icon="send",
                              on_click=lambda: run_job(jobs.dispatcher.tick, "发送", render)).props("outline")
            acc_hint = ui.label("").classes("text-sm text-orange-600")

            hint("流程：待审核 → 批准 → 待发送 → 分发器按账号活跃时段/间隔自动发出（或点「触发发送」立即尝试）→ 已发送（自动回查 X 上是否真的存在）。"
                     , after_row=True)
            with ui.expansion("审核与标签说明", icon="help_outline").classes("xo-help w-full text-sm"):
                tag_legend(["account", "reply", "post", "source", "ai", "warn", "media", "metric"])
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

            def acc_id() -> int:
                return int(acc_filter.value or 0)

            def render(force: bool = True):
                items = _load(status_sel.value, acc_id())
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
                status_sel.set_options(_status_options(acc_id()), value=status_sel.value)
                acc_filter.set_options(_account_filter_options(status_sel.value), value=acc_id())
                sync_toolbar()
                body.clear()
                with body:
                    if not items:
                        ui.label("此状态下暂无条目 🎉" if not acc_id() else "这个账号在此状态下没有条目").classes("text-gray-400")
                        return
                    if len(items) >= _LIMIT:
                        ui.label(f"只显示最新的 {_LIMIT} 条，处理掉一些后会显示更多").classes("text-xs text-gray-400")
                    for it in items:
                        _card(it, render, delete_cb, swap_cb, verify_cb, attach_cb, shorten_cb, state["dirty"], recheck_cb, force_cb, send_now_cb, restore_failed_cb)

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
                client = ui.context.client
                async with llm_wait("AI 缩写", scene="shorten", result_link=("查看任务队列", "/queue")) as task:
                    new, note = await run.io_bound(_shorten, jobs, it["id"], text, account_id)
                    task.finish(note, ok=bool(new))
                if not client.is_deleted:
                    with client.content:
                        notify_long(note, ok=bool(new), kind=None if new else "warning")
                        if new:
                            state["dirty"].discard(it["id"])
                            state["sig"] = None
                            render(force=False)

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

            def restore_failed_cb(it):
                done, detail = jobs.guard.restore_failed(it["id"])
                ui.notify(detail, type="positive" if done else "negative", multi_line=True)
                if done:
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

            def scope_text() -> str:
                st = status_sel.value
                label = UNSENT_LABEL if st == UNSENT else QUEUE_STATUS_LABEL.get(st, st)
                who = _account_handle(acc_id())
                return (f"@{who} 的" if who else "") + f"「{label}」"

            def sync_toolbar():
                st, aid = status_sel.value, acc_id()
                recheck_btn.set_visibility(st == "skipped" and not aid)
                transfer_btn.set_visibility(bool(aid) and st not in ("sent", "sending") and not _account_deleted(aid))
                acc_hint.text = ""
                if aid:
                    with get_conn() as conn:
                        a = conn.execute("SELECT handle, status, deleted_at FROM accounts WHERE id=?", (aid,)).fetchone()
                    if a is not None and a["deleted_at"]:
                        acc_hint.text = f"@{a['handle']} 已删除，这里只剩它的历史记录（保留着用于去重和作者冷却）。"
                    elif a is not None and a["status"] != "active":
                        n = _counts(aid).get(UNSENT, 0)
                        acc_hint.text = (f"@{a['handle']} 当前「{ACC_STATUS_LABEL.get(a['status'], a['status'])}」，名下 {n} 条未发送的条目发不出去："
                                         "可以「批量转给其他账号」，或「批量删除」。状态选「未发送的全部」可以一次处理完。")

            async def clear_all():
                st, aid = status_sel.value, acc_id()
                n = len(_matching_ids(st, aid))
                if not n:
                    ui.notify("没有可删除的条目", type="info"); return
                state["busy"] += 1
                try:
                    ok = await confirm(f"删除{scope_text()}全部 {n} 条条目？",
                                       "没发出去的条目删除后，对应的抓取记录会退回「达标但未生成回复」，之后可以重新处理；"
                                       "已发送记录删除后不影响去重账本（不会重复回复同一推文）。", ok_label="全部删除")
                finally:
                    state["busy"] -= 1
                if ok:
                    done = _delete_all(st, aid)
                    ui.notify(f"已删除 {done} 条", type="positive")
                    render()
            clear_btn.on_click(clear_all)

            async def transfer_all():
                st, aid = status_sel.value, acc_id()
                ids = _matching_ids(st, aid)
                if not ids:
                    ui.notify("没有可转移的条目", type="info"); return
                state["busy"] += 1
                try:
                    targets = await _transfer_dialog(scope_text(), len(ids), aid)
                finally:
                    state["busy"] -= 1
                if not targets:
                    return
                res = transfer_items(ids, targets)
                if res["error"]:
                    ui.notify(res["error"], type="negative"); return
                parts = [f"已转移 {res['moved']} 条：" + "、".join(f"@{h} {n} 条" for h, n in res["per_account"].items())]
                if res["back_to_pending"]:
                    parts.append(f"其中 {res['back_to_pending']} 条待发送的超出新账号的长度上限，已退回待审核，请删减或点「AI 缩写」")
                if res["not_movable"]:
                    parts.append(f"{res['not_movable']} 条正在发送或已发送，没动")
                notify_long("；".join(parts), ok=res["moved"] > 0, kind=None if res["moved"] else "warning")
                render()
            transfer_btn.on_click(transfer_all)

            def on_filter_change():
                render()
            status_sel.on("update:model-value", lambda e: on_filter_change())
            acc_filter.on("update:model-value", lambda e: on_filter_change())
            render()
            ui.timer(5.0, lambda: render(force=False))


def _status_options(account_id: int = ALL_ACCOUNTS) -> dict:
    c = _counts(account_id)
    return {k: f"{v}（{c.get(k, 0)}）" for k, v in QUEUE_STATUS_LABEL.items() if k != "sending"} | \
        ({"sending": f"发送中（{c['sending']}）"} if c.get("sending") else {}) | {UNSENT: f"{UNSENT_LABEL}（{c[UNSENT]}）"}


def _account_deleted(account_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT deleted_at FROM accounts WHERE id=?", (int(account_id),)).fetchone()
    return bool(row and row["deleted_at"])


def _account_handle(account_id: int) -> str:
    if not account_id:
        return ""
    with get_conn() as conn:
        row = conn.execute("SELECT handle FROM accounts WHERE id=?", (int(account_id),)).fetchone()
    return row["handle"] if row else ""


async def _transfer_dialog(scope: str, n: int, source_id: int) -> list[int] | None:
    """选转给哪些账号。返回目标账号 id 列表，取消返回 None。"""
    opts = {k: v for k, v in _active_account_options().items() if k != source_id}
    with ui.dialog() as dlg, ui.card().classes("w-[560px] max-w-[95vw]"):
        ui.label("批量转给其他账号").classes("text-lg font-bold")
        ui.label(f"把{scope}共 {n} 条条目改由下面选的账号发送。").classes("text-sm")
        if not opts:
            ui.label("除了这个账号没有其他启用中的账号：先到「设置 → 账号」启用或添加一个。").classes("text-sm text-orange-600")
        sel = ui.select(opts, value=[], multiple=True, label="转给哪些账号（选多个 = 平均分）").classes("w-full").props("outlined use-chips")
        hint("条目状态不变：待审核的仍待审核，待发送的按新账号的活跃时段 / 发送间隔 / 日上限发出；"
                 "失败的转过去后可以再点「捞回待审核」。待发送的条目超出新账号长度上限（免费账号 280 单位）会退回待审核。"
                 "发送中 / 已发送的不会动。", after_row=True)

        def ok():
            if not sel.value:
                ui.notify("先选至少一个账号", type="warning"); return
            dlg.submit([int(x) for x in sel.value])
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
            ui.button("转移", icon="swap_horiz", on_click=ok).props("color=primary")
    dlg.open()
    return await dlg


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


def _card(it, refresh, delete_cb, swap_cb, verify_cb, attach_cb, shorten_cb, dirty: set, recheck_cb=None, force_cb=None, send_now_cb=None,
          restore_failed_cb=None):
    files = media.parse_files(it["final_media_files"])
    limits = _account_limits()
    cur_acc = {"id": it["account_id"]}
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-2 w-full"):
            if it["status"] == "pending" and it["error_msg"]:
                ui.label(f"上次发送失败：{it['error_msg']}（已捞回，批准前请确认问题已解决）").classes("text-xs text-red-600 w-full")
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
                tag(f"@{it['acc_handle']}" + ("（已删除）" if it["acc_deleted"] else ""), "account",
                    "发送账号" + ("：这个账号已经删除，记录保留用于去重和作者冷却" if it["acc_deleted"] else ""))
            tag("回复" if it["action_type"] == "reply" else "发帖", it["action_type"] if it["action_type"] in ("reply", "post") else "reply",
                "回复 = 回在别人推文下；发帖 = 自己账号发主贴")
            origin = it["origin"] or ("scheduled" if it["scheduled_post_id"] else "ai_match")
            tag("来源：" + ORIGIN_LABEL.get(origin, origin), "ai" if origin == "ai_write" else "source", "这条文案是怎么来的")
            if it["target_tweet_id"] and it["tgt_source"]:
                tag(source_label(it["tgt_source"], it["rule_name"], it["rule_kind"], it["watched_handle"]), "source",
                    "目标推文是哪条搜索规则 / 哪个监控推主抓来的（回复方式、免审核、回复账号都按它的设置）")
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
            with ui.column().classes("xo-source-quote w-full"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.label(f"@{it['author_handle']} 的推文").classes("text-xs text-gray-500")
                    # 与「抓取记录」页的卡片保持同一套小标签：相关性 / 语言 / 观看量 / 发推时间
                    if it["tgt_score"] is not None:
                        sc = it["tgt_score"]
                        thr = it["rule_min"] if it["rule_min"] is not None else 7
                        tag(f"相关性 {sc}/10" + (f"（达标线 {thr}）" if it["tgt_source"] == "search" else ""),
                            "metric_ok" if sc >= thr else "metric_bad", "AI 给的相关性分；绿 = 达到规则的达标分，红 = 没达到")
                    if it["tgt_lang"]:
                        tag(lang_name(it["tgt_lang"]), "metric", "推文语言")
                    if it["tgt_views"] is not None:
                        tag(f"👁 {fmt_views(it['tgt_views'])}", "metric", "抓取时的观看量")
                    if it["tgt_created_at"]:
                        ui.label(f"发推于 {fmt_time(it['tgt_created_at'])}").classes("text-xs text-gray-400")
                preview_text(it["tgt_text"])
                if it["text_zh"]:
                    detail_text("中文翻译", it["text_zh"])
                tweet_link(it["author_handle"], it["tgt_tweet_id"])

        if it["llm_reason"]:
            conf = f"（置信度 {it['llm_confidence']:.2f}）" if it["llm_confidence"] is not None else ""
            with ui.expansion(f"生成说明 {conf}").classes("w-full"):
                ui.label(it["llm_reason"])

        editable = it["status"] == "pending"
        ta = ui.textarea(label="待发布文案" if editable else "发布文案", value=it["final_text"]).classes("w-full").props("outlined autogrow" + ("" if editable else " readonly"))
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
            elif it["status"] == "failed":
                with ui.column().classes("w-full items-start gap-3"):
                    ui.label("发送失败" + (f" · {it['error_msg']}" if it["error_msg"] else "")).classes("text-sm text-red-600 break-words w-full")
                    ui.button("捞回待审核", icon="restore", on_click=lambda: restore_failed_cb(it)).props("outline dense color=orange") \
                        .tooltip("放回待审核、重试次数清零，批准后按正常流程重新发；失败原因会留在条目上供参考")
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
