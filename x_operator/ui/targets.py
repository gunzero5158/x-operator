"""抓取记录：监控/搜索抓回来的每一条推文都在这里——打分、被过滤的原因、处理状态一目了然。

这一页回答「跑了监控/搜索之后到底抓到了什么、为什么没进任务队列」。
支持 URL 参数直达：/targets?source=search&rule=3&status=filtered
可对未匹配/已过滤的推文手动「重新匹配」，也可删除、拉黑作者、批量清理。
"""
from __future__ import annotations

from .i18n import Labels, t as _tr

from nicegui import run, ui

import json

from ..adapters.base import MEDIA_KIND_LABEL
from ..core.langdetect import lang_name
from ..core.matcher import BatchMatchResult, load_source_cfg
from ..db.database import get_conn, utcnow_iso
from .layout import page_title, detail_text, preview_text
from .layout import (TARGET_STATUS_LABEL, QUEUE_STATUS_LABEL, confirm, fmt_time, fmt_views, notify_long, tag, tag_legend,
                     run_job_with_progress, shell, source_label, tweet_link)
from .pickers import ai_write_dialog, pick_material_dialog

_LIMIT = 150


def _load(status: str, source: str, rule_id: int = 0):
    q = ("SELECT tt.*, sr.name AS rule_name, sr.min_llm_score AS rule_min, sr.source_kind AS rule_kind, wu.handle AS watched_handle, "
         "linked.id AS queue_id, linked.status AS queue_status, "
         "(SELECT COUNT(*) FROM review_queue rq WHERE rq.target_tweet_id=tt.id AND rq.status='expired') AS expired_queue_count "
         "FROM target_tweets tt "
         "LEFT JOIN review_queue linked ON linked.id=(SELECT rq.id FROM review_queue rq WHERE rq.target_tweet_id=tt.id "
         "ORDER BY CASE WHEN rq.status IN ('pending','approved','sending') THEN 0 WHEN rq.status='sent' THEN 1 "
         "WHEN rq.status IN ('failed','skipped') THEN 2 ELSE 3 END, rq.id DESC LIMIT 1) "
         "LEFT JOIN search_rules sr ON sr.id=tt.source_rule_id AND tt.source='search' "
         "LEFT JOIN watched_users wu ON wu.id=tt.source_rule_id AND tt.source='monitor' WHERE 1=1")
    args: list = []
    if status != "all":
        q += " AND tt.process_status=?"; args.append(status)
    if source != "all":
        q += " AND tt.source=?"; args.append(source)
    if rule_id:
        q += " AND tt.source='search' AND tt.source_rule_id=?"; args.append(rule_id)
    q += f" ORDER BY tt.fetched_at DESC, tt.id DESC LIMIT {_LIMIT}"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


def _counts(source: str = "all", rule_id: int = 0) -> dict[str, int]:
    q = "SELECT process_status, COUNT(*) AS c FROM target_tweets WHERE 1=1"
    args: list = []
    if source != "all":
        q += " AND source=?"; args.append(source)
    if rule_id:
        q += " AND source='search' AND source_rule_id=?"; args.append(rule_id)
    q += " GROUP BY process_status"
    with get_conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return {r["process_status"]: r["c"] for r in rows}


def _rule_options() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM search_rules ORDER BY id").fetchall()
    opts = {0: _tr('全部规则')}
    for r in rows:
        opts[r["id"]] = _tr('规则「{p0}」', p0=r['name'])
    return opts


def _delete(tid: int) -> str:
    with get_conn() as conn:
        ref = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE target_tweet_id=?", (tid,)).fetchone()["c"]
        if ref:
            return _tr('该推文已进入任务队列，请先在「任务队列」里删除对应条目')
        conn.execute("DELETE FROM target_tweets WHERE id=?", (tid,))
        conn.commit()
    return ""


def _delete_bulk(statuses: list[str]) -> tuple[int, int]:
    """删除指定状态的抓取记录（跳过被任务队列引用的）。返回 (删除数, 保留数)。"""
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
                     "ON CONFLICT(x_user_id) DO NOTHING", (author_id, handle or "", _tr('抓取记录页手动拉黑'), utcnow_iso()))
        conn.commit()


def register(jobs) -> None:
    @ui.page("/targets")
    def targets_page(status: str = "all", source: str = "all", rule: int = 0):
        if status not in TARGET_STATUS_LABEL:
            status = "all"
        if source not in ("monitor", "search"):
            source = "all"
        if rule:
            source = "search"
        with shell("/targets"):
            with ui.row().classes("xo-page-heading w-full"):
                page_title(_tr('抓取记录'), _tr('浏览发现的内容，处理未生成的回复'))
                with ui.row().classes("xo-toolbar w-full items-center gap-2"):
                    status_f = ui.select(_status_options(source, rule), value=status).props("dense outlined")
                    source_f = ui.select({"all": _tr('全部来源'), "monitor": _tr('监控推主'), "search": _tr('语义搜索')}, value=source).props("dense outlined")
                    rule_f = ui.select(_rule_options(), value=rule if rule in _rule_options() else 0).props("dense outlined")
                    ui.button(icon="refresh", on_click=lambda: render()).props("outline dense").tooltip(_tr('刷新列表与数量；在任务队列处理后可点此同步显示'))
                    ui.button(_tr('运行监控'), icon="visibility",
                              on_click=lambda: run_job_with_progress(lambda progress: jobs.monitor.run_once(progress=progress), _tr('监控'), render, task_key='monitor')).props("outline dense")
                    ui.button(_tr('运行所有搜索规则'), icon="manage_search",
                              on_click=lambda: run_job_with_progress(lambda progress: jobs.search.run_once(progress=progress), _tr('搜索'), render, task_key='search')).props("outline dense")
                    with ui.button(icon="delete_sweep").props("outline color=negative dense"):
                        with ui.menu():
                            ui.menu_item(_tr('清理已过滤 / 未匹配 / 已过期'), on_click=lambda: clear(["filtered", "no_match", "expired"]))
                            ui.menu_item(_tr('清理全部抓取记录'), on_click=lambda: clear(list(TARGET_STATUS_LABEL)))

            with ui.expansion(_tr('状态与处理说明'), icon="help_outline").classes("xo-help w-full text-sm"):
                tag_legend(["source", "ok", "wait", "attn", "off", "metric"])
                ui.markdown(
                    _tr('- **统计口径**：这里按抓取到的原推文计数，同一推文只记一次；任务队列按发布任务计数，同一推文可有多条历史草稿，还包含定时主帖。两边数量不要求相等。\n- **已进任务队列**：已生成回复任务，可能待审核、待发送、发送失败、已跳过或已经发送；不代表还在等审核。卡片上的关联任务显示具体状态。\n- **达标但未生成回复**：相关性够了，但素材不足、AI 撰写失败或账号不可用等原因导致没有生成草稿。「自动匹配」沿用来源规则；「素材库匹配」直接从已有回复素材中选择，保留素材原文和附件。\n- **未达标 / 被过滤**：下面几种情况之一，每条卡片上都写了具体原因——\n  ① 相关性打分低于规则的达标分（没配 LLM 时只是关键词粗估，普遍偏低）；\n  ② 推文语言不在规则选的语言内；\n  ③ 预检拦下：转推 / 自己账号的推文 / 早于规则或推主的「首次回溯」时间窗 / 作者在黑名单 / 该推文已回复过 / 作者在冷却期（设置 → 合规参数「作者冷却天数」）。\n- **待匹配**：抓到了还没来得及匹配（一般几秒内会变）。\n- **回复草稿已过期**：关联回复草稿超时，且没有其他待审核、待发送、发送中或已发送任务。原推文本身并未失效。重新生成草稿后，这条原推文回到「已进任务队列」，旧草稿仍保留在任务队列的「已过期」中。\n- **筛选与刷新**：这里按来源和规则筛选，任务队列按发送账号筛选。在队列处理后，点刷新可更新这里的状态与数量。\n- **手动处理**：只生成待审核草稿。发送前仍检查黑名单、重复回复、作者冷却和时效。')
                ).classes("text-xs text-gray-600")
            body = ui.column().classes("w-full gap-2")
            selected: set[int] = set()
            visible_ids: list[int] = []

            async def clear(statuses: list[str]):
                c = _counts()
                n = sum(c.get(s, 0) for s in statuses)
                if not n:
                    ui.notify(_tr('没有可清理的记录'), type="info"); return
                if await confirm(_tr('删除 {p0} 条抓取记录？', p0=n), _tr('已进入任务队列的记录会保留。'), ok_label=_tr('删除')):
                    done, kept = _delete_bulk(statuses)
                    ui.notify(_tr('已删除 {p0} 条', p0=done) + (_tr('，{p0} 条因在任务队列中而保留', p0=kept) if kept else ""), type="positive")
                    render()

            async def rematch(tid: int, *, material_only: bool = False):
                label = _tr('素材库匹配') if material_only else _tr('自动匹配')
                def work(progress):
                    progress(0, _tr('正在从素材库选择已有回复…') if material_only else _tr('正在按来源规则自动匹配…'))
                    outcome = jobs.match.rematch(tid, material_only=material_only)
                    result = BatchMatchResult(total=1)
                    setattr(result, outcome.status, 1)
                    result.details.append(outcome.reason)
                    return result
                await run_job_with_progress(work, f"{label} #{tid}", render,
                                            (_tr('查看待审核'), "/queue?status=pending"),
                                            task_key=f"rematch:{tid}:{material_only}")

            async def rematch_selected(*, material_only: bool = False):
                ids = [tid for tid in visible_ids if tid in selected]
                if not ids:
                    ui.notify(_tr('请先勾选要自动匹配的记录'), type="info")
                    return
                expected_status = status_f.value
                label = _tr('批量素材库匹配') if material_only else _tr('批量自动匹配')
                scope_label = TARGET_STATUS_LABEL[expected_status]
                # 两个状态列表可并行；同一列表的两种匹配方式仍互斥。
                task_key = f"batch-rematch:{expected_status}"
                await run_job_with_progress(lambda progress: jobs.match.rematch_many(
                                                ids, progress, expected_status=expected_status, material_only=material_only),
                                            _tr('{p0} · {p1}（{p2} 条）', p0=scope_label, p1=label, p2=len(ids)), render,
                                            (_tr('查看待审核'), "/queue?status=pending"), task_key=task_key)

            def delete_one(tid: int):
                err = _delete(tid)
                ui.notify(err or _tr('已删除'), type="negative" if err else "positive")
                render()

            def blacklist(author_id: str, handle: str):
                _blacklist(author_id, handle)
                ui.notify(_tr('已拉黑 @{p0}，之后不再对其回复', p0=handle), type="warning")
                render()

            async def pick(t):
                res = await pick_material_dialog(t["text"], t["lang"])
                if res is None:
                    return
                mid, text = res
                outcome = await run.io_bound(jobs.match.manual_match, t["id"], mid, text)
                notify_long((_tr('已进入待审核：') if outcome.status == "queued" else _tr('没能生成：')) + outcome.reason,
                            ok=outcome.status == "queued")
                render()

            async def write(t):
                cfg = load_source_cfg(t)
                default_brief = ""
                if cfg is not None:
                    try:
                        default_brief = cfg["ai_brief"] or ""
                    except (IndexError, KeyError):
                        default_brief = ""
                await ai_write_dialog(jobs, t["id"], t["text"], default_brief)
                render()

            def render():
                if body.is_deleted or body.client.is_deleted:
                    return
                body.clear()
                selected.clear()
                visible_ids.clear()
                rid = int(rule_f.value or 0)
                src = source_f.value
                if rid and src != "search":
                    src = "search"; source_f.value = "search"
                rule_f.set_visibility(src in ("search", "all"))
                status_f.set_options(_status_options(src, rid), value=status_f.value)
                rows = _load(status_f.value, src, rid)
                with body:
                    if not rows:
                        c = _counts(src, rid)
                        if sum(c.values()) == 0:
                            ui.label(_tr('这个范围内还没有抓取记录。点上方「运行监控」或「运行搜索」试试；如果刚运行过却没记录，看弹出的结果说明（可能是游标之后没新推文，或账号连不上）。')).classes("text-gray-400")
                        else:
                            ui.label(_tr('这个状态下没有记录，换个状态筛选看看。')).classes("text-gray-400")
                        return
                    ui.label(_tr('最近 {p0} 条', p0=len(rows)) + (_tr('（已达显示上限，可清理旧记录）') if len(rows) >= _LIMIT else "")).classes("text-xs text-gray-400")
                    selection_box = None
                    if status_f.value in ("filtered", "no_match"):
                        visible_ids.extend(t["id"] for t in rows)
                        checkboxes = {}
                        with ui.row().classes("xo-batch-bar w-full items-center gap-2"):
                            def set_all(value):
                                for cb in checkboxes.values():
                                    cb.set_value(value)

                            ui.button(_tr('全选当前列表'), on_click=lambda: set_all(True)).props("flat dense")
                            ui.button(_tr('取消全选'), on_click=lambda: set_all(False)).props("flat dense")
                            count_label = ui.label(_tr('已选 0 条')).classes("text-sm text-slate-600")
                            batch_btn = ui.button(_tr('批量自动匹配'), icon="autorenew", on_click=rematch_selected).props("outline color=primary")
                            batch_btn.tooltip(_tr('按各条记录的原规则处理；AI 创作规则仍会重新撰写'))
                            batch_btn.disable()
                            material_btn = ui.button(_tr('批量素材库匹配'), icon="inventory_2",
                                                     on_click=lambda: rematch_selected(material_only=True)).props("outline color=primary")
                            material_btn.tooltip(_tr('从启用的回复素材中自动选择，使用素材原文和附件，不调用 AI 撰写或润色'))
                            material_btn.disable()
                        detail_text(_tr('批量处理说明'), _tr('「自动匹配」沿用原规则；「素材库匹配」只选已有回复素材，保留原文和附件。两者均跳过打分和预检，成功后进入待审核。全选只包含当前显示的 {p0} 条；运行中可以继续使用其他功能。', p0=len(rows)))

                        def selection_box(tid):
                            def change(e):
                                if e.value:
                                    selected.add(tid)
                                else:
                                    selected.discard(tid)
                                count_label.set_text(_tr('已选 {p0} 条', p0=len(selected)))
                                batch_btn.set_enabled(bool(selected))
                                material_btn.set_enabled(bool(selected))
                            checkboxes[tid] = ui.checkbox(_tr('选择'), on_change=change).props("dense")

                    for t in rows:
                        _card(t, rematch, delete_one, blacklist, pick, write, selection_box)

            status_f.on("update:model-value", lambda e: render())
            source_f.on("update:model-value", lambda e: render())
            rule_f.on("update:model-value", lambda e: render())
            render()


def _status_options(source: str = "all", rule_id: int = 0) -> dict:
    c = _counts(source, rule_id)
    opts = {"all": _tr('全部状态（{p0}）', p0=sum(c.values()))}
    for k, v in TARGET_STATUS_LABEL.items():
        opts[k] = f"{v}（{c.get(k, 0)}）"
    return opts


_STATUS_KIND = {"queued": "ok", "no_match": "attn", "filtered": "off", "new": "wait", "expired": "off"}
_MEDIA_ICON = {"photo": "🖼", "video": "🎬", "gif": "🎞"}


def media_tags(media_json: str | None) -> list[tuple[str, str]]:
    """抓取记录里的附件元信息 → [(标签文字, 提示)]，按类型合并计数，例：🖼 2 / 🎬 0:42。"""
    try:
        items = json.loads(media_json or "[]")
    except (TypeError, ValueError):
        return []
    out: list[tuple[str, str]] = []
    for kind in ("photo", "video", "gif"):
        hits = [m for m in items if isinstance(m, dict) and m.get("kind") == kind]
        if not hits:
            continue
        label = f"{_MEDIA_ICON[kind]} {len(hits)}" if kind != "video" else _MEDIA_ICON[kind]
        dur = hits[0].get("duration_ms") if kind == "video" else None
        if dur:
            label += f" {int(dur) // 60000}:{(int(dur) // 1000) % 60:02d}"
        tip = _tr('推文带 {p0} 个{p1}', p0=len(hits), p1=MEDIA_KIND_LABEL[kind]) + (_tr('；规则开了「读取附图打分」才会送给 AI') if kind != "video" else _tr('（AI 最多只能看封面图）'))
        out.append((label, tip))
    return out


def _card(t, rematch, delete_one, blacklist, pick, write, selection_box=None):
    with ui.card().classes("xo-target-card w-full"):
        with ui.row().classes("xo-record-meta items-center gap-2 w-full"):
            if selection_box is not None:
                selection_box(t["id"])
            tag(source_label(t["source"], t["rule_name"], t["rule_kind"], t["watched_handle"]), "source",
                _tr('这条推文是哪条规则 / 哪个推主抓来的'))
            tag(TARGET_STATUS_LABEL.get(t["process_status"], t["process_status"]), _STATUS_KIND.get(t["process_status"], "off"),
                _tr('处理状态（页顶「各状态是什么意思」有解释）'))
            if t["llm_relevance_score"] is not None:
                sc = t["llm_relevance_score"]
                thr = t["rule_min"] if t["rule_min"] is not None else 7
                tag(_tr('相关性 {p0}/10', p0=sc) + (_tr('（达标线 {p0}）', p0=thr) if t["source"] == "search" else ""),
                    "metric_ok" if sc >= thr else "metric_bad", _tr('AI 给的相关性分；绿 = 达到规则的达标分，红 = 没达到'))
            if t["lang"]:
                tag(lang_name(t["lang"]), "metric", _tr('推文语言'))
            if t["view_count"] is not None:
                tag(f"👁 {fmt_views(t['view_count'])}", "metric", _tr('抓取时的观看量') + (_tr('（复查时会更新）') if t["views_recheck_until"] else ""))
            if t["views_recheck_until"] and t["process_status"] == "filtered":
                tag(_tr('观看量复查中（至 {p0}）', p0=fmt_time(t['views_recheck_until'])), "warn",
                    _tr('观看量还没到这个推主设的下限：复查期内每次监控都会重新看，涨上来就自动处理'))
            for label, tip in media_tags(t["media"]):
                tag(label, "metric", tip)
            ui.label(_tr('抓取于 {p0} · 发推于 {p1}', p0=fmt_time(t['fetched_at']), p1=fmt_time(t['tweet_created_at']))).classes("text-xs text-gray-400")
            ui.space()
            ui.button(icon="delete", on_click=lambda: delete_one(t["id"])).props("flat dense round color=negative").tooltip(_tr('删除此记录'))
        ui.label(f"@{t['author_handle'] or t['author_id']}").classes("text-xs text-gray-500")
        preview_text(t["text"])
        if t["text_zh"]:
            detail_text(_tr('中文翻译'), t["text_zh"])
        if t["llm_relevance_reason"]:
            if t["process_status"] in ("filtered", "no_match", "expired"):
                detail_text(_tr('草稿过期说明') if t["process_status"] == "expired" else _tr('未进队列'), t["llm_relevance_reason"], warning=True)
            else:
                detail_text(_tr('打分理由'), t["llm_relevance_reason"])
        tweet_link(t["author_handle"], t["tweet_id"])
        with ui.row().classes("xo-actions gap-2 items-center flex-wrap"):
            if t["queue_id"]:
                label = QUEUE_STATUS_LABEL.get(t["queue_status"], t["queue_status"])
                ui.link(_tr('关联任务 #{p0} · {p1} →', p0=t['queue_id'], p1=label), f"/queue?status={t['queue_status']}").classes("text-xs")
                if t["expired_queue_count"] and t["queue_status"] != "expired":
                    ui.label(_tr('另有 {p0} 条历史过期草稿', p0=t['expired_queue_count'])).classes("text-xs text-gray-500")
            if t["process_status"] in ("no_match", "filtered", "expired", "new"):
                ui.button(_tr('自动匹配'), icon="autorenew", on_click=lambda: rematch(t["id"])).props("dense outline color=primary") \
                    .tooltip(_tr('跳过打分和预检，按来源规则的回复方式自动生成一次草稿，进待审核'))
                ui.button(_tr('素材库匹配'), icon="inventory_2", on_click=lambda: rematch(t["id"], material_only=True)) \
                    .props("dense outline color=primary") \
                    .tooltip(_tr('从启用的回复素材中自动选择，保留原文和附件，不再 AI 撰写或润色；成功后进入待审核'))
            with ui.button(_tr('更多操作'), icon="more_horiz").props("flat dense"):
                with ui.menu():
                    if t["process_status"] in ("no_match", "filtered", "expired", "new"):
                        ui.menu_item(_tr('手动选素材'), on_click=lambda: pick(t))
                        ui.menu_item(_tr('AI 撰写'), on_click=lambda: write(t))
                    if t["author_id"]:
                        ui.menu_item(_tr('拉黑作者'), on_click=lambda: blacklist(t["author_id"], t["author_handle"])).classes("text-negative")


# Resolve display labels per client; keep core dictionaries and stored values unchanged.
MEDIA_KIND_LABEL = Labels(MEDIA_KIND_LABEL)
TARGET_STATUS_LABEL = Labels(TARGET_STATUS_LABEL)
QUEUE_STATUS_LABEL = Labels(QUEUE_STATUS_LABEL)
