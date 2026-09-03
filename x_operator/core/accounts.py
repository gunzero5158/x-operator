"""回复用哪个账号（多账号分摊）。

每条搜索规则 / 每个监控推主可以指定「回复账号」：
- 具体某个账号 → 就用它（它被停用/删除时退回自动轮流并说明）
- 空（自动轮流）→ 在启用中的**非主号**里挑最闲的：先排除今天已到日回复上限的，再按
  「今天已回 + 队列里待审核/待发送的条数」最少、下次可发时间最早来选。这样一轮抓到的多条草稿也会均匀分到各小号。
- 一个小号都没有 → 退回抓取用的那个账号（通常是主号）并在理由里写明。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..db.database import get_conn, parse_iso
from .compliance import ComplianceGuard

AUTO_ROTATE = 0   # reply_account_id 存 NULL/0 都表示自动轮流
AUTO_ROTATE_LABEL = "自动轮流（小号分摊，主号不参与）"


def _cfg_get(cfg, key: str, default):
    if cfg is None:
        return default
    try:
        v = cfg[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def account_options() -> dict:
    """UI 下拉用：{0: 自动轮流, id: @handle（主号/已暂停 标注）}。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, handle, is_primary, status FROM accounts ORDER BY is_primary DESC, id").fetchall()
    opts = {AUTO_ROTATE: AUTO_ROTATE_LABEL}
    for a in rows:
        tag = " · 主号" if a["is_primary"] else ""
        tag += "" if a["status"] == "active" else f" · {'已暂停' if a['status'] == 'paused' else '凭据失效'}"
        opts[a["id"]] = f"@{a['handle']}{tag}"
    return opts


def choose_reply_account(cfg, fallback: sqlite3.Row) -> tuple[sqlite3.Row, str]:
    """返回 (账号行, 一句中文说明)。fallback = 抓取用的账号，兜底用。"""
    wanted = int(_cfg_get(cfg, "reply_account_id", 0) or 0)
    note = ""
    with get_conn() as conn:
        if wanted:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (wanted,)).fetchone()
            if row is not None and row["status"] == "active":
                return row, f"由指定账号 @{row['handle']} 回复"
            note = "指定的回复账号已删除或未启用，改为自动轮流；"
        pool = conn.execute("SELECT * FROM accounts WHERE status='active' AND is_primary=0 ORDER BY id").fetchall()
        if not pool:
            return fallback, note + f"没有启用中的小号可轮流，由 @{fallback['handle']} 回复"
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
        full = load >= reply_lim
        na = parse_iso(a["next_allowed_at"])
        wait = max(0.0, (na - now).total_seconds()) if na else 0.0
        ranked.append((full, load, wait, a["id"], a, used))
    ranked.sort(key=lambda x: x[:4])
    full, load, _wait, _id, best, used = ranked[0]
    tail = "，注意：所有小号今天都已到回复上限，明天才会发" if full else ""
    return best, note + f"自动轮流 → @{best['handle']}（今日已回 {used}，待发 {load - used}）{tail}"
