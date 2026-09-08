"""定时计划（design-v1.1 §8.6）：定时发帖计划的增删改、暂停/恢复、立即生成一次。

内容来源三种：固定一条素材 / 素材池轮流（按语言、标签圈一批发帖素材，每次挑用得最少、最近没发过的）/
AI 按主题创作（每次到点现写）。前两种可开「AI 改写变体」——同一条素材每次换个说法，避开 X 的重复判定。
"""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import run, ui

from ..core import media
from ..core.schedule_calc import compute_next_run, describe_interval
from ..core.scheduler import POST_MODE_LABEL
from ..core.search import LANG_LABEL
from ..db.database import get_conn, to_iso, utcnow_iso
from .layout import confirm, fmt_time, shell, tag
from .media_widget import MediaField, media_badge
from .pickers import hint, template_controls

_TYPE_LABEL = {"once": "一次", "daily": "每天", "weekly": "每周", "interval": "每隔", "cron": "每天(cron)"}
_TYPE_OPTIONS = {"daily": "每天固定时间", "weekly": "每周固定几天", "interval": "每隔 N 小时 / 分钟", "once": "只发一次"}
_EXPR_DEFAULT = {"daily": "21:00", "weekly": "mon,thu 21:00", "interval": "6h", "once": "", "cron": "0 21 * * *"}
_EXPR_LABEL = {"daily": "几点发（HH:MM）", "weekly": "星期几 几点（如 mon,thu 21:00）", "interval": "间隔（如 6h / 90m）",
               "once": "日期时间（YYYY-MM-DDTHH:MM）", "cron": "cron（M H * * *）"}


def _describe_when(sp) -> str:
    t = sp["schedule_type"]
    if t == "interval":
        return describe_interval(sp["schedule_expr"])
    return f"{_TYPE_LABEL.get(t, t)}: {sp['schedule_expr']}"
_STATUS_LABEL = {"active": "进行中", "paused": "已暂停", "done": "已完成", "missed": "已错过"}

MEDIA_MODE_LABEL = {"fixed": "固定：每次都带下面这几个", "pool": "素材池：每次从下面随机挑 1 个"}
MEDIA_FIELD_LABEL = {"fixed": "每次随帖一起发的配图 / 视频（选填）", "pool": "配图 / 视频素材池（选填，每次随机挑 1 个）"}
MEDIA_FIELD_NOTE = "固定素材 / 素材池轮流模式的附件跟着素材走，在素材库里给素材加。"

HINTS = {
    "mode": "固定一条素材=每次都发这条（周期计划要配合「AI 改写变体」，否则第二次起 X 会判重复拒发）；"
            "素材池轮流=在符合语言/标签的启用中发帖素材里，每次挑用得最少、最近 30 天没发过的一条，最省心；"
            "AI 按主题创作=不用素材，按你写的主题要求每次现写一条（需 LLM）。",
    "pool": "语言留空=不限；标签留空=全部发帖素材，填了就只用标签匹配的（素材库里的场景标签，多个用逗号）。",
    "rewrite": "开=每次发之前让 AI 在素材基础上改写一个新变体（意思不变、保留链接和 @、换措辞），并避开最近 30 天发过的内容。"
               "周期计划推荐开；AI 出错时会退回素材原文并在说明里注明。",
    "brief": "写清楚主题/立场、必须带的链接或 @账号（写在这里会强制原样出现）、语气。AI 会参考最近发过的内容换角度写。",
    "media_mode": "固定=下面放的几个附件每次都一起发（最多 4 个）；素材池=下面放一批图片 / 视频（最多 30 个），"
                  "每次发帖随机挑 1 个，优先挑最近没发过的，不会连着两次用同一个。",
    "expr": "每天 → 21:00 · 每周 → mon,thu 21:00 · 每隔 → 6h（每 6 小时一条，从保存时算起；也可 90m）· 只发一次 → 2026-09-10T21:00。时间按所选账号的时区。",
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
                ui.label("定时发帖").classes("text-2xl font-bold")
                ui.button("新建发帖计划", icon="add", on_click=lambda: _edit(None, render)).props("color=primary")
            ui.label("用自己的账号按计划发主贴（不是回复别人）。到点后按计划的「内容来源」产出一条推文进审核队列（勾了自动批准则直接进待发送），"
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
                        "SELECT sp.*, a.handle AS acc_handle, m.text AS mat_text, m.deleted_at AS mat_deleted, m.media_files AS mat_media "
                        "FROM scheduled_posts sp JOIN accounts a ON a.id=sp.account_id "
                        "LEFT JOIN materials m ON m.id=sp.material_id ORDER BY sp.id").fetchall()
                with body:
                    if not rows:
                        ui.label("暂无定时发帖计划：点右上「新建发帖计划」，让账号按时间自动发主贴").classes("text-gray-400")
                        return
                    for sp in rows:
                        mode = sp["content_mode"] or "fixed"
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                tag(f"@{sp['acc_handle']}", "account", "用哪个账号发")
                                tag(_describe_when(sp), "metric", "什么时候发")
                                tag(_STATUS_LABEL.get(sp["status"], sp["status"]),
                                    {"active": "ok", "paused": "off", "done": "off", "missed": "attn"}.get(sp["status"], "off"), "计划状态")
                                tag("内容：" + POST_MODE_LABEL.get(mode, mode), "ai" if mode == "ai_topic" else "mode", "每次发什么")
                                if sp["ai_rewrite"]:
                                    tag("AI 改写变体", "ai", "每次发之前让 AI 换个说法")
                                if sp["auto_approve"]:
                                    tag("自动批准", "warn", "到点直接进待发送，不经人工审核")
                                if mode == "fixed" and sp["mat_deleted"]:
                                    tag("素材已在回收站", "warn", "到点会暂停；请编辑计划换素材或恢复素材")
                                if mode == "ai_topic" and sp["media_mode"] == "pool" and media.parse_files(sp["media_files"]):
                                    pool_files = media.parse_files(sp["media_files"])
                                    lost = media.missing(pool_files)
                                    tag(f"🎲 附件素材池 {len(pool_files)} 个" + ("（有文件丢失）" if lost else ""), "media" if not lost else "bad",
                                        "每次发帖从这批图片 / 视频里随机挑 1 个" if not lost else "素材池里有文件在 data/media 找不到了，挑到它会发送失败")
                                else:
                                    media_badge(media.parse_files(sp["mat_media"] if mode == "fixed" else (sp["media_files"] if mode == "ai_topic" else None)))
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
            ui.label("编辑定时发帖计划" if sp else "新建定时发帖计划").classes("text-lg font-bold")
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
                hint(HINTS["pool"], after_row=True)
            rewrite = ui.switch("AI 改写变体（每次换个说法再发，需 LLM）", value=bool(sp["ai_rewrite"]) if sp else True)
            rw_hint = ui.label(HINTS["rewrite"]).classes("text-xs text-gray-400 -mt-2 mb-1")
            # AI 主题
            ai_box = ui.column().classes("w-full gap-1")
            with ai_box:
                write_lang = ui.select(_write_langs(), value=(sp["pool_lang"] if sp and sp["pool_lang"] in _write_langs() else "ja"),
                                       label="推文语言").classes("w-60").props("outlined")
                brief = ui.textarea("主题要求", value=sp["ai_brief"] if sp else "").classes("w-full").props("outlined autogrow")
                hint(HINTS["brief"], after_row=True)   # ai_box 是 gap-1 的紧凑列，负边距会压到文本框上
                template_controls(brief)
                media_mode = ui.select(MEDIA_MODE_LABEL, value=(sp["media_mode"] if sp and sp["media_mode"] in MEDIA_MODE_LABEL else "fixed"),
                                       label="配图 / 视频怎么带").classes("w-full").props("outlined")
                hint(HINTS["media_mode"], after_row=True)
                mf = MediaField(media.parse_files(sp["media_files"]) if sp else [], label=MEDIA_FIELD_LABEL["fixed"],
                                note=MEDIA_FIELD_NOTE)

                def sync_media_mode():
                    pool = media_mode.value == "pool"
                    mf.set_limit(media.POOL_MAX_ITEMS if pool else media.MAX_ITEMS, MEDIA_FIELD_NOTE, label=MEDIA_FIELD_LABEL[media_mode.value])
                media_mode.on("update:model-value", lambda e: sync_media_mode()); sync_media_mode()

            def sync():
                m = mode.value
                mat_box.set_visibility(m == "fixed"); pool_box.set_visibility(m == "pool"); ai_box.set_visibility(m == "ai_topic")
                rewrite.set_visibility(m in ("fixed", "pool")); rw_hint.set_visibility(m in ("fixed", "pool"))
            mode.on("update:model-value", lambda e: sync()); sync()

            ui.separator()
            ui.label("什么时候发").classes("font-semibold text-sm")
            type_opts = dict(_TYPE_OPTIONS)
            if sp and sp["schedule_type"] == "cron":
                type_opts["cron"] = "每天固定时间（旧 cron 写法）"
            stype = ui.select(type_opts, value=sp["schedule_type"] if sp else "daily", label="节奏").classes("w-full").props("outlined")
            expr = ui.input(_EXPR_LABEL.get(stype.value, "表达式"), value=sp["schedule_expr"] if sp else "21:00").classes("w-full").props("outlined")
            hint(HINTS["expr"])

            def sync_type():
                expr.props(f'label="{_EXPR_LABEL.get(stype.value, "表达式")}"')
                # 换了节奏类型、原表达式是别的类型的默认值或空 → 换成新类型的默认值
                if (expr.value or "").strip() in ("", *_EXPR_DEFAULT.values()):
                    expr.value = _EXPR_DEFAULT.get(stype.value, "")
            stype.on("update:model-value", lambda e: sync_type())
            auto = ui.switch("自动批准（到点直接进待发送，不经人工审核）", value=bool(sp["auto_approve"]) if sp else False)
            hint(HINTS["auto"])

            def do_save():
                m = mode.value
                if m == "fixed" and not mat.value:
                    ui.notify("固定素材模式要选一条发帖素材", type="negative"); return
                if m == "ai_topic" and not (brief.value or "").strip():
                    ui.notify("AI 按主题创作要填主题要求", type="negative"); return
                media_max = media.POOL_MAX_ITEMS if media_mode.value == "pool" else media.MAX_ITEMS
                if m == "ai_topic" and media.check_set(mf.files, media_max):
                    ui.notify(media.check_set(mf.files, media_max) + ("（想放更多请把「配图 / 视频怎么带」改成素材池）" if media_mode.value != "pool" else ""),
                              type="negative", multi_line=True); return
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
                            media_files=media.dump_files(mf.files if m == "ai_topic" else []),
                            media_mode=media_mode.value if m == "ai_topic" else "fixed",
                            schedule_type=stype.value, schedule_expr=expr.value.strip(), next_run_at=nxt_s,
                            auto_approve=1 if auto.value else 0)
                with get_conn() as conn:
                    if sp:
                        conn.execute(
                            "UPDATE scheduled_posts SET account_id=:account_id, material_id=:material_id, content_mode=:content_mode, "
                            "pool_lang=:pool_lang, pool_tags=:pool_tags, ai_rewrite=:ai_rewrite, ai_brief=:ai_brief, media_files=:media_files, media_mode=:media_mode, "
                            "schedule_type=:schedule_type, schedule_expr=:schedule_expr, next_run_at=:next_run_at, "
                            "auto_approve=:auto_approve, status='active', last_error=NULL WHERE id=:id", {**data, "id": sp["id"]})
                    else:
                        conn.execute(
                            "INSERT INTO scheduled_posts(account_id, material_id, content_mode, pool_lang, pool_tags, ai_rewrite, ai_brief, media_files, media_mode, "
                            "schedule_type, schedule_expr, next_run_at, auto_approve, status, created_at) "
                            "VALUES (:account_id, :material_id, :content_mode, :pool_lang, :pool_tags, :ai_rewrite, :ai_brief, :media_files, :media_mode, "
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
