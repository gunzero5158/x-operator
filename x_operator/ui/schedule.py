"""定时计划（design-v1.1 §8.6）：定时发帖计划的增删改、暂停/恢复、立即生成一次。

内容来源三种：固定一条素材 / 素材池轮流（按语言、标签圈一批发帖素材，每次挑用得最少、最近没发过的）/
AI 按主题创作（每次到点现写）。前两种可开「AI 改写变体」——同一条素材每次换个说法，避开 X 的重复判定。
"""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import run, ui

from ..core.schedule_calc import compute_next_run
from ..core.scheduler import POST_MODE_LABEL
from ..core.search import LANG_LABEL
from ..db.database import get_conn, to_iso, utcnow_iso
from .layout import confirm, fmt_time, shell
from .pickers import hint, template_controls

_TYPE_LABEL = {"once": "一次", "daily": "每天", "weekly": "每周", "cron": "cron"}
_STATUS_LABEL = {"active": "进行中", "paused": "已暂停", "done": "已完成", "missed": "已错过"}

HINTS = {
    "mode": "固定一条素材=每次都发这条（周期计划要配合「AI 改写变体」，否则第二次起 X 会判重复拒发）；"
            "素材池轮流=在符合语言/标签的启用中发帖素材里，每次挑用得最少、最近 30 天没发过的一条，最省心；"
            "AI 按主题创作=不用素材，按你写的主题要求每次现写一条（需 LLM）。",
    "pool": "语言留空=不限；标签留空=全部发帖素材，填了就只用标签匹配的（素材库里的场景标签，多个用逗号）。",
    "rewrite": "开=每次发之前让 AI 在素材基础上改写一个新变体（意思不变、保留链接和 @、换措辞），并避开最近 30 天发过的内容。"
               "周期计划推荐开；AI 出错时会退回素材原文并在说明里注明。",
    "brief": "写清楚主题/立场、必须带的链接或 @账号（写在这里会强制原样出现）、语气。AI 会参考最近发过的内容换角度写。",
    "expr": "一次性 → 2026-09-10T21:00 · 每天 → 21:00 · 每周 → mon,thu 21:00 · cron → 0 21 * * *（都按所选账号的时区）。",
    "auto": "开=到点直接进「待发送」由分发器发出，不经人工审核；关=先进待审核，你批准后才发。AI 生成的内容建议先关着看几次。",
}


def _post_langs() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT lang FROM materials WHERE kind='post' AND deleted_at IS NULL ORDER BY lang").fetchall()
    opts = {"": "不限"}
    for r in rows:
        opts[r["lang"]] = LANG_LABEL.get(r["lang"], r["lang"])
    return opts


def _write_langs() -> dict:
    return {k: v for k, v in LANG_LABEL.items()}


def register(jobs) -> None:
    @ui.page("/schedule")
    def schedule_page():
        with shell("/schedule"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("定时计划").classes("text-2xl font-bold")
                ui.button("新建计划", icon="add", on_click=lambda: _edit(None, render)).props("color=primary")
            ui.label("到点后按计划的「内容来源」产出一条推文进审核队列（勾了自动批准则直接进待发送），"
                     "再由发送分发按账号活跃时段/间隔发出。后台每分钟检查一次到点计划。"
                     "周期性计划请用「素材池轮流」或开「AI 改写变体」，否则每天发同一段文字会被 X 判重复。").classes("text-xs text-gray-400")

            body = ui.column().classes("w-full gap-2")

            async def delete(sp):
                if await confirm("删除这个定时计划？"):
                    _delete(sp["id"]); ui.notify("已删除", type="positive"); render()

            async def fire_now(sp):
                ui.notify("正在生成…（AI 模式要几秒）", type="info")
                ok, msg = await run.io_bound(jobs.fire_plan_now, sp["id"])
                ui.notify(("已生成一条到审核队列：" if ok else "生成失败：") + msg, type="positive" if ok else "negative",
                          multi_line=True, close_button=True, timeout=12000)
                render()

            def render():
                body.clear()
                with get_conn() as conn:
                    rows = conn.execute(
                        "SELECT sp.*, a.handle AS acc_handle, m.text AS mat_text, m.deleted_at AS mat_deleted "
                        "FROM scheduled_posts sp JOIN accounts a ON a.id=sp.account_id "
                        "LEFT JOIN materials m ON m.id=sp.material_id ORDER BY sp.id").fetchall()
                with body:
                    if not rows:
                        ui.label("暂无定时计划").classes("text-gray-400")
                        return
                    for sp in rows:
                        mode = sp["content_mode"] or "fixed"
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                ui.badge(f"@{sp['acc_handle']}").classes("bg-slate-600")
                                ui.badge(f"{_TYPE_LABEL.get(sp['schedule_type'], sp['schedule_type'])}: {sp['schedule_expr']}").classes("bg-blue-600")
                                ui.badge(_STATUS_LABEL.get(sp["status"], sp["status"])).classes("bg-green-600" if sp["status"] == "active" else "bg-gray-500")
                                ui.badge(POST_MODE_LABEL.get(mode, mode)).classes("bg-purple-600" if mode == "ai_topic" else "bg-teal-600")
                                if sp["ai_rewrite"]:
                                    ui.badge("AI 改写变体").classes("bg-purple-500")
                                if sp["auto_approve"]:
                                    ui.badge("自动批准").classes("bg-red-600")
                                if mode == "fixed" and sp["mat_deleted"]:
                                    ui.badge("素材已在回收站").classes("bg-amber-500")
                                ui.label(f"下次 {fmt_time(sp['next_run_at']) if sp['next_run_at'] else '—'}"
                                         + (f" · 上次 {fmt_time(sp['last_run_at'])}" if sp["last_run_at"] else "")).classes("text-xs text-gray-400")
                            if mode == "fixed":
                                ui.label((sp["mat_text"] or "（素材不存在）")[:140]).classes("text-sm")
                            elif mode == "pool":
                                ui.label("素材池：" + ("语言 " + LANG_LABEL.get(sp["pool_lang"], sp["pool_lang"]) if sp["pool_lang"] else "语言不限")
                                         + ("，标签 " + sp["pool_tags"] if sp["pool_tags"] else "，全部发帖素材")).classes("text-sm")
                            else:
                                ui.label("主题要求：" + (sp["ai_brief"] or "（未填！）")[:140]).classes("text-sm " + ("" if sp["ai_brief"] else "text-red-500"))
                            if sp["last_error"]:
                                ui.label("上次生成失败：" + sp["last_error"]).classes("text-xs text-red-600 whitespace-pre-wrap")
                            with ui.row().classes("gap-2"):
                                ui.button("编辑", on_click=lambda s=sp: _edit(s, render)).props("flat dense")
                                if sp["status"] == "active":
                                    ui.button("暂停", on_click=lambda s=sp: (_set_status(s["id"], "paused"), render())).props("flat dense")
                                elif sp["status"] in ("paused", "done", "missed"):
                                    ui.button("恢复/重新启用", on_click=lambda s=sp: (_reactivate(s), render())).props("flat dense")
                                ui.button("立即生成一次", icon="bolt", on_click=lambda s=sp: fire_now(s)).props("flat dense").tooltip("不等到点，现在就按内容来源生成一条到审核队列（不改下次运行时间）")
                                ui.button("删除", icon="delete", on_click=lambda s=sp: delete(s)).props("flat dense color=negative")

            render()

    def _edit(sp, refresh):
        with get_conn() as conn:
            accounts = conn.execute("SELECT id, handle FROM accounts ORDER BY id").fetchall()
            mats = conn.execute("SELECT id, text FROM materials WHERE kind='post' AND status='active' AND deleted_at IS NULL ORDER BY id").fetchall()
            cur_mat = conn.execute("SELECT id, text, status, deleted_at FROM materials WHERE id=?", (sp["material_id"],)).fetchone() \
                if (sp and sp["material_id"]) else None
        if not accounts:
            ui.notify("先到「设置 → 账号」添加一个账号", type="negative"); return

        with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-[95vw] max-h-[92vh] overflow-auto"):
            ui.label("编辑定时计划" if sp else "新建定时计划").classes("text-lg font-bold")
            acc = ui.select({a["id"]: a["handle"] for a in accounts},
                            value=sp["account_id"] if sp else accounts[0]["id"], label="发帖账号").classes("w-full").props("outlined")
            ui.separator()
            ui.label("内容来源").classes("font-semibold text-sm")
            mode = ui.select(POST_MODE_LABEL, value=(sp["content_mode"] if sp else "pool"), label="每次发什么").classes("w-full").props("outlined")
            hint(HINTS["mode"])
            # 固定素材
            mat_opts = {m["id"]: m["text"][:40] for m in mats}
            if cur_mat is not None and cur_mat["id"] not in mat_opts:
                tag = "已在回收站" if cur_mat["deleted_at"] else f"状态：{cur_mat['status']}"
                mat_opts = {cur_mat["id"]: f"⚠ {tag}｜{cur_mat['text'][:36]}", **mat_opts}
            mat_box = ui.column().classes("w-full gap-1")
            with mat_box:
                mat = ui.select(mat_opts, value=(sp["material_id"] if sp and sp["material_id"] in mat_opts else (mats[0]["id"] if mats else None)),
                                label="发帖素材").classes("w-full").props("outlined")
                if not mats:
                    ui.label("还没有启用的发帖素材：素材库 → 新建 → 类型选「发帖」并启用").classes("text-xs text-orange-600")
            # 素材池
            pool_box = ui.column().classes("w-full gap-1")
            with pool_box:
                with ui.row().classes("w-full gap-2 no-wrap"):
                    pool_lang = ui.select(_post_langs(), value=(sp["pool_lang"] if sp and sp["pool_lang"] in _post_langs() else ""),
                                          label="素材语言").classes("flex-1").props("outlined")
                    pool_tags = ui.input("场景标签（选填，逗号隔开）", value=sp["pool_tags"] if sp else "").classes("flex-1").props("outlined")
                hint(HINTS["pool"])
            rewrite = ui.switch("AI 改写变体（每次换个说法再发，需 LLM）", value=bool(sp["ai_rewrite"]) if sp else True)
            rw_hint = ui.label(HINTS["rewrite"]).classes("text-xs text-gray-400 -mt-2 mb-1")
            # AI 主题
            ai_box = ui.column().classes("w-full gap-1")
            with ai_box:
                write_lang = ui.select(_write_langs(), value=(sp["pool_lang"] if sp and sp["pool_lang"] in _write_langs() else "ja"),
                                       label="推文语言").classes("w-60").props("outlined")
                brief = ui.textarea("主题要求", value=sp["ai_brief"] if sp else "").classes("w-full").props("outlined autogrow")
                hint(HINTS["brief"])
                template_controls(brief)

            def sync():
                m = mode.value
                mat_box.set_visibility(m == "fixed"); pool_box.set_visibility(m == "pool"); ai_box.set_visibility(m == "ai_topic")
                rewrite.set_visibility(m in ("fixed", "pool")); rw_hint.set_visibility(m in ("fixed", "pool"))
            mode.on("update:model-value", lambda e: sync()); sync()

            ui.separator()
            ui.label("什么时候发").classes("font-semibold text-sm")
            stype = ui.select({"once": "一次性", "daily": "每天", "weekly": "每周", "cron": "cron(M H * * *)"},
                              value=sp["schedule_type"] if sp else "daily", label="类型").classes("w-full").props("outlined")
            expr = ui.input("表达式", value=sp["schedule_expr"] if sp else "21:00").classes("w-full").props("outlined")
            hint(HINTS["expr"])
            auto = ui.switch("自动批准（到点直接进待发送，不经人工审核）", value=bool(sp["auto_approve"]) if sp else False)
            hint(HINTS["auto"])

            def do_save():
                m = mode.value
                if m == "fixed" and not mat.value:
                    ui.notify("固定素材模式要选一条发帖素材", type="negative"); return
                if m == "ai_topic" and not (brief.value or "").strip():
                    ui.notify("AI 按主题创作要填主题要求", type="negative"); return
                if (m == "ai_topic" or (rewrite.value and m in ("fixed", "pool"))) and not jobs.llm.configured:
                    ui.notify("AI 创作 / AI 改写需要先到「设置 → LLM」配置网关（或先关掉「AI 改写变体」）", type="negative", multi_line=True); return
                if m == "pool":
                    with get_conn() as conn:
                        q = "SELECT scenario_tags FROM materials WHERE kind='post' AND status='active' AND deleted_at IS NULL"
                        args = []
                        if pool_lang.value:
                            q += " AND lang=?"; args.append(pool_lang.value)
                        rows = conn.execute(q, args).fetchall()
                    tags = [t.strip() for t in (pool_tags.value or "").replace("，", ",").split(",") if t.strip()]
                    if tags:
                        rows = [r for r in rows if set(x.strip() for x in (r["scenario_tags"] or "").split(",")) & set(tags)]
                    if not rows:
                        ui.notify("按这个语言/标签在素材库里找不到启用的发帖素材，先去素材库加几条", type="negative", multi_line=True); return
                with get_conn() as conn:
                    acc_row = conn.execute("SELECT timezone FROM accounts WHERE id=?", (acc.value,)).fetchone()
                try:
                    nxt = compute_next_run(stype.value, expr.value.strip(), datetime.now(timezone.utc), acc_row["timezone"])
                except ValueError as e:
                    ui.notify(str(e), type="negative"); return
                if nxt is None:
                    ui.notify("这个时间已经过去了，请填一个将来的时间", type="negative"); return
                nxt_s = to_iso(nxt)
                if m == "fixed":
                    with get_conn() as conn:
                        ok = conn.execute("SELECT 1 FROM materials WHERE id=? AND status='active' AND deleted_at IS NULL", (mat.value,)).fetchone()
                    if ok is None:
                        ui.notify("所选素材不是「启用」状态或已在回收站，请换一条（或先到素材库恢复/启用它）", type="negative"); return
                if m == "pool":
                    lang_val = pool_lang.value or ""
                elif m == "ai_topic":
                    lang_val = write_lang.value or "ja"
                else:
                    lang_val = ""
                data = dict(account_id=acc.value, material_id=(mat.value if m == "fixed" else None), content_mode=m,
                            pool_lang=lang_val,
                            pool_tags=(pool_tags.value or "").strip() if m == "pool" else "",
                            ai_rewrite=1 if (rewrite.value and m in ("fixed", "pool")) else 0,
                            ai_brief=(brief.value or "").strip() if m == "ai_topic" else "",
                            schedule_type=stype.value, schedule_expr=expr.value.strip(), next_run_at=nxt_s,
                            auto_approve=1 if auto.value else 0)
                with get_conn() as conn:
                    if sp:
                        conn.execute(
                            "UPDATE scheduled_posts SET account_id=:account_id, material_id=:material_id, content_mode=:content_mode, "
                            "pool_lang=:pool_lang, pool_tags=:pool_tags, ai_rewrite=:ai_rewrite, ai_brief=:ai_brief, "
                            "schedule_type=:schedule_type, schedule_expr=:schedule_expr, next_run_at=:next_run_at, "
                            "auto_approve=:auto_approve, status='active', last_error=NULL WHERE id=:id", {**data, "id": sp["id"]})
                    else:
                        conn.execute(
                            "INSERT INTO scheduled_posts(account_id, material_id, content_mode, pool_lang, pool_tags, ai_rewrite, ai_brief, "
                            "schedule_type, schedule_expr, next_run_at, auto_approve, status, created_at) "
                            "VALUES (:account_id, :material_id, :content_mode, :pool_lang, :pool_tags, :ai_rewrite, :ai_brief, "
                            ":schedule_type, :schedule_expr, :next_run_at, :auto_approve, 'active', :created_at)",
                            {**data, "created_at": utcnow_iso()})
                    conn.commit()
                dialog.close(); refresh(); ui.notify("已保存，下次运行 " + fmt_time(nxt_s), type="positive")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("保存", on_click=do_save).props("color=primary")
        dialog.open()


def _set_status(sid: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE scheduled_posts SET status=? WHERE id=?", (status, sid))
        conn.commit()


def _reactivate(sp) -> None:
    """恢复计划并重算下次时间；一次性且时间已过则提示。"""
    with get_conn() as conn:
        tz = conn.execute("SELECT timezone FROM accounts WHERE id=?", (sp["account_id"],)).fetchone()["timezone"]
    try:
        nxt = compute_next_run(sp["schedule_type"], sp["schedule_expr"], datetime.now(timezone.utc), tz)
    except ValueError as e:
        ui.notify(str(e), type="negative"); return
    if nxt is None:
        ui.notify("一次性计划的时间已过去，请「编辑」改成将来的时间", type="warning"); return
    with get_conn() as conn:
        conn.execute("UPDATE scheduled_posts SET status='active', next_run_at=?, last_error=NULL WHERE id=?", (to_iso(nxt), sp["id"]))
        conn.commit()
    ui.notify("已恢复，下次运行 " + fmt_time(to_iso(nxt)), type="positive")


def _delete(sid: int):
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET scheduled_post_id=NULL WHERE scheduled_post_id=?", (sid,))
        conn.execute("DELETE FROM scheduled_posts WHERE id=?", (sid,))
        conn.commit()
