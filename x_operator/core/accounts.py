"""回复用哪个账号（多账号分摊）。

每条搜索规则 / 每个监控推主的「回复账号」有三种设法（reply_account_mode + reply_account_ids）：
- auto    自动轮流：在启用中的**非主号**里挑最闲的——先排除今天已到日回复上限的，再按
          「今天已回 + 队列里待审核/待发送的条数」最少、下次可发时间最早来选。这样一轮抓到的多条草稿也会均匀分到各小号。
          一个小号都没有 → 退回抓取用的那个账号（通常是主号）并在理由里写明。
- include 只用指定的账号：名单里 1 个 = 固定用它；多个 = 在它们之间按同样的规则轮流（主号选进名单也参与）。
          名单里的账号都被停用 / 删除了 → 退回自动轮流并说明。
- exclude 自动轮流，但排除名单里的账号：小号池去掉这些；去完一个不剩时退回主号，主号也被排除就再找别的启用账号，
          实在没有就不生成草稿（返回 None + 原因），绝不动用被排除的号。
旧数据只有 reply_account_id（单个指定账号）的，按「只用指定的账号、名单里就它一个」处理。

删除账号（delete_account）：
- 还有没发出去的队列条目、或进行中 / 暂停中的定时发帖计划用它 → 拒绝，并说清楚是哪几样、去哪处理；
- 发过东西（已发送 / 已跳过 / 已过期的队列记录、发送账本、已结束的计划）→ 软删除：打 deleted_at 标记、清空凭据、状态改暂停，
  各处列表 / 下拉 / 轮流 / 抓取账号池都不再出现它；记录留着，去重（同一条推文不再回）和作者冷却照常生效，历史里显示「@xxx（已删除）」；
- 什么都没有 → 真删。
两种删除都会顺手把搜索规则 / 监控推主里指向它的回复账号名单、推荐流账号去掉。重新添加同名账号会接上软删除的那条记录。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..db.database import get_conn, parse_iso, utcnow_iso
from .compliance import ComplianceGuard

AUTO_ROTATE = 0   # reply_account_id 存 NULL/0 都表示自动轮流
AUTO_ROTATE_LABEL = "自动轮流（小号分摊，主号不参与）"

# 回复账号的三种设法
REPLY_ACCOUNT_MODE_LABEL = {
    "auto": AUTO_ROTATE_LABEL,
    "include": "只用指定的账号（选 1 个 = 固定用它，选多个 = 在它们之间轮流）",
    "exclude": "自动轮流，但排除某些账号",
}


def _cfg_get(cfg, key: str, default):
    if cfg is None:
        return default
    try:
        v = cfg[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def account_options(with_auto: bool = True) -> dict:
    """UI 下拉用：{0: 自动轮流, id: @handle（主号/已暂停 标注）}。with_auto=False 时不带「自动轮流」那一项（多选名单用）。已删除的账号不列。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, handle, is_primary, status FROM accounts WHERE deleted_at IS NULL ORDER BY is_primary DESC, id").fetchall()
    opts = {AUTO_ROTATE: AUTO_ROTATE_LABEL} if with_auto else {}
    for a in rows:
        tag = " · 主号" if a["is_primary"] else ""
        tag += "" if a["status"] == "active" else f" · {'已暂停' if a['status'] == 'paused' else '凭据失效'}"
        opts[a["id"]] = f"@{a['handle']}{tag}"
    return opts


def parse_ids(raw) -> list[int]:
    """reply_account_ids 的 JSON 列表 → 去重保序的正整数列表；坏数据当空。"""
    try:
        vals = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except (TypeError, ValueError):
        return []
    out: list[int] = []
    for v in vals if isinstance(vals, list) else []:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in out:
            out.append(i)
    return out


def reply_account_setting(cfg) -> tuple[str, list[int]]:
    """规则 / 推主的回复账号设法 → (mode, 账号 id 列表)。兼容旧数据：只有 reply_account_id 的 = 只用它一个。"""
    mode = str(_cfg_get(cfg, "reply_account_mode", "auto") or "auto")
    ids = parse_ids(_cfg_get(cfg, "reply_account_ids", "[]"))
    if mode not in REPLY_ACCOUNT_MODE_LABEL or (mode != "auto" and not ids):
        mode, ids = "auto", []
    legacy = int(_cfg_get(cfg, "reply_account_id", 0) or 0)
    if mode == "auto" and legacy:
        return "include", [legacy]
    return mode, ids if mode != "auto" else []


def reply_account_summary(cfg, acc_opts: dict | None = None) -> str:
    """卡片标签用的一句：自动轮流 / @a / 轮流：@a、@b / 自动轮流，排除 @c。"""
    mode, ids = reply_account_setting(cfg)
    if mode == "auto":
        return "自动轮流"
    opts = acc_opts if acc_opts is not None else account_options(with_auto=False)
    names = [opts.get(i, "（已删除）").split(" · ")[0] for i in ids]
    if mode == "include":
        return names[0] if len(names) == 1 else "轮流：" + "、".join(names)
    return "自动轮流，排除 " + "、".join(names)


def fallback_reply_account() -> sqlite3.Row | None:
    """没有小号可轮流时兜底回复的账号：主号优先，其次任一启用账号。
    不看读取额度 / 15 分钟读取上限——回复和抓取是两回事，抓取账号池挑不出号不该让手动生成草稿失败。"""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM accounts WHERE status='active' "
                            "ORDER BY is_primary DESC, (access_type='official') DESC, id LIMIT 1").fetchone()


def choose_reply_account(cfg, fallback: sqlite3.Row) -> tuple[sqlite3.Row | None, str]:
    """返回 (账号行, 一句中文说明)。fallback = 抓取用的账号，兜底用。
    只有「排除某些账号」把所有启用账号都排除光了才返回 (None, 原因)——这时宁可不生成草稿，也不用被排除的号。"""
    mode, ids = reply_account_setting(cfg)
    note = ""
    with get_conn() as conn:
        active = conn.execute("SELECT * FROM accounts WHERE status='active' ORDER BY id").fetchall()
    if mode == "include":
        chosen = [a for a in active if a["id"] in ids]
        if chosen:
            if len(chosen) == 1:
                a = chosen[0]
                skipped = "（名单里其他账号未启用）" if len(ids) > 1 else ""
                return a, f"由指定账号 @{a['handle']} 回复{skipped}"
            best, used, load, full = _least_loaded(chosen)
            return best, f"在指定的 {len(chosen)} 个账号里轮流 → @{best['handle']}（今日已回 {used}，待发 {load - used}）" + (
                "，注意：这几个账号今天都已到回复上限，明天才会发" if full else "")
        note = "指定的回复账号已删除或未启用，改为自动轮流；"
        mode, ids = "auto", []
    excluded = set(ids) if mode == "exclude" else set()
    pool = [a for a in active if not a["is_primary"] and a["id"] not in excluded]
    ex_note = f"（已排除 {len(excluded)} 个账号）" if excluded else ""
    if not pool:
        if fallback is not None and fallback["id"] not in excluded:
            return fallback, note + f"没有启用中的小号可轮流{ex_note}，由 @{fallback['handle']} 回复"
        others = [a for a in active if a["id"] not in excluded]
        if others:
            a = sorted(others, key=lambda x: (-x["is_primary"], x["id"]))[0]
            return a, note + f"没有启用中的小号可轮流{ex_note}，由 @{a['handle']} 回复"
        return None, "回复账号设成了「自动轮流，但排除某些账号」，排除之后没有启用中的账号可用，这条没生成草稿。请到规则 / 推主里调整排除名单"
    best, used, load, full = _least_loaded(pool)
    tail = "，注意：所有小号今天都已到回复上限，明天才会发" if full else ""
    return best, note + f"自动轮流{ex_note} → @{best['handle']}（今日已回 {used}，待发 {load - used}）{tail}"


def _least_loaded(pool: list[sqlite3.Row]) -> tuple[sqlite3.Row, int, int, bool]:
    """在 pool 里挑最闲的：没到日上限的优先，再按「今天已回 + 待审核/待发送」最少、下次可发时间最早。
    返回 (账号, 今日已回, 今日已回+待发, 是否都已到上限)。"""
    with get_conn() as conn:
        queued = {r["account_id"]: r["c"] for r in conn.execute(
            "SELECT account_id, COUNT(*) AS c FROM review_queue WHERE action_type='reply' "
            "AND status IN ('pending','approved','sending') GROUP BY account_id")}
    guard = ComplianceGuard()
    now = datetime.now(timezone.utc)
    ranked = []
    for a in pool:
        _, reply_lim = guard.effective_limits(a)
        used = guard.daily_action_count(a["id"], "reply", now, a["timezone"])
        load = used + queued.get(a["id"], 0)
        na = parse_iso(a["next_allowed_at"])
        wait = max(0.0, (na - now).total_seconds()) if na else 0.0
        ranked.append((load >= reply_lim, load, wait, a["id"], a, used))
    ranked.sort(key=lambda x: x[:4])
    full, load, _wait, _id, best, used = ranked[0]
    return best, used, load, full


# ---------------- 删除账号 ----------------
UNSENT_QUEUE = ("pending", "approved", "sending", "failed")
LIVE_PLANS = ("active", "paused")


def account_references(account_id: int) -> dict:
    """这个账号被哪些记录引用着：unsent 没发出去的队列条目、queue_history 已发送 / 已跳过 / 已过期、
    interactions 发送账本、live_plans 进行中 / 暂停的定时计划、old_plans 已结束的计划。"""
    with get_conn() as conn:
        q = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM review_queue WHERE account_id=? GROUP BY status", (account_id,))}
        p = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM scheduled_posts WHERE account_id=? GROUP BY status", (account_id,))}
        inter = conn.execute("SELECT COUNT(*) c FROM interactions WHERE account_id=?", (account_id,)).fetchone()["c"]
    return {
        "unsent": sum(v for k, v in q.items() if k in UNSENT_QUEUE),
        "sent": q.get("sent", 0),
        "queue_history": sum(v for k, v in q.items() if k not in UNSENT_QUEUE),
        "interactions": inter,
        "live_plans": sum(v for k, v in p.items() if k in LIVE_PLANS),
        "old_plans": sum(v for k, v in p.items() if k not in LIVE_PLANS),
    }


def delete_blockers(refs: dict) -> list[str]:
    """删除前必须先处理掉的东西（中文，一样一句）。空列表 = 可以删。"""
    out = []
    if refs["unsent"]:
        out.append(f"任务队列里还有 {refs['unsent']} 条没发出去的条目（待审核 / 待发送 / 发送中 / 失败）："
                   "到任务队列转给其他账号或删除（发送中的等它发完）")
    if refs["live_plans"]:
        out.append(f"有 {refs['live_plans']} 个进行中或暂停中的定时发帖计划用它发帖：到定时发帖把计划改用其他账号，或删掉计划")
    return out


def _drop_rule_refs(conn: sqlite3.Connection, account_id: int) -> int:
    """把搜索规则 / 监控推主里指向这个账号的设置去掉：回复账号名单里移除（名单空了改回自动轮流）、旧的单个指定账号、推荐流账号。
    返回改了几条规则 / 推主。"""
    changed = 0
    for table in ("search_rules", "watched_users"):
        extra = ", feed_account_id" if table == "search_rules" else ""
        for r in conn.execute(f"SELECT id, reply_account_mode, reply_account_ids, reply_account_id{extra} FROM {table}").fetchall():
            ids = parse_ids(r["reply_account_ids"])
            touched = False
            mode = r["reply_account_mode"]
            if account_id in ids:
                ids = [i for i in ids if i != account_id]
                if not ids:
                    mode = "auto"
                touched = True
            legacy = r["reply_account_id"]
            if legacy == account_id:
                legacy, touched = None, True
            feed = r["feed_account_id"] if extra else None
            if extra and feed == account_id:
                feed, touched = None, True
            if touched:
                if extra:
                    conn.execute(f"UPDATE {table} SET reply_account_mode=?, reply_account_ids=?, reply_account_id=?, feed_account_id=? WHERE id=?",
                                 (mode, json.dumps(ids), legacy, feed, r["id"]))
                else:
                    conn.execute(f"UPDATE {table} SET reply_account_mode=?, reply_account_ids=?, reply_account_id=? WHERE id=?",
                                 (mode, json.dumps(ids), legacy, r["id"]))
                changed += 1
    return changed


def delete_account(account_id: int) -> tuple[str, str]:
    """删除账号。返回 (结果, 中文说明)：blocked 拒绝 / retired 软删除（保留记录）/ deleted 彻底删除。调用方负责清客户端缓存。"""
    with get_conn() as conn:
        row = conn.execute("SELECT handle, deleted_at FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None or row["deleted_at"]:
        return "blocked", "账号不存在或已经删除"
    handle = row["handle"]
    refs = account_references(account_id)
    blockers = delete_blockers(refs)
    if blockers:
        return "blocked", f"@{handle} 暂时不能删除：" + "；".join(blockers)
    keep = refs["queue_history"] or refs["interactions"] or refs["old_plans"]
    with get_conn() as conn:
        n_rules = _drop_rule_refs(conn, account_id)
        if keep:
            conn.execute("UPDATE accounts SET deleted_at=?, status='paused', is_primary=0, credentials='{}', read_paused_until=NULL WHERE id=?",
                         (utcnow_iso(), account_id))
        else:
            conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        conn.commit()
    tail = f"；{n_rules} 条搜索规则 / 监控推主里指向它的回复账号或推荐流账号设置已去掉" if n_rules else ""
    if not keep:
        return "deleted", f"已删除 @{handle}（它没有任何发送记录，已彻底删除）{tail}"
    kept = []
    if refs["sent"]:
        kept.append(f"{refs['sent']} 条已发送记录")
    if refs["queue_history"] - refs["sent"]:
        kept.append(f"{refs['queue_history'] - refs['sent']} 条已跳过 / 已过期记录")
    if refs["interactions"]:
        kept.append(f"{refs['interactions']} 条发送账本")
    if refs["old_plans"]:
        kept.append(f"{refs['old_plans']} 个已结束的定时计划")
    return "retired", (f"已删除 @{handle}：凭据已清空，各处不再出现它。保留了" + "、".join(kept) +
                       "——防止别的账号重复回复同一条推文、作者冷却照常计算，历史里显示为「@" + handle + "（已删除）」。"
                       "以后重新添加同名账号会接上这些记录" + tail)


def deleted_account_id(handle: str) -> int | None:
    """同名的软删除账号 id（重新添加时复用那条记录，接上历史）。"""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE handle=? AND deleted_at IS NOT NULL", (handle,)).fetchone()
    return row["id"] if row else None
