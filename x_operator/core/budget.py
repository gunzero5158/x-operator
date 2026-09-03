"""读额度（设置 → 预算）的实际执行。

- daily_read_budget：今天（UTC）最多从 X 读多少条。
- budget_reserve_reads：熔断保留——自动轮询在「剩余 < 保留数」时就停，把最后一点额度留给手动操作。
手动运行只在额度完全用完时拒绝；自动轮询更保守。
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..db.database import get_conn


@dataclass(frozen=True)
class BudgetState:
    used_today: int
    daily_budget: int
    reserve: int
    used_month: int

    @property
    def remaining(self) -> int:
        return max(0, self.daily_budget - self.used_today)

    def allow(self, auto: bool) -> str:
        """返回空串=可以跑；否则是中文拒绝原因。daily_budget<=0 表示不限。"""
        if self.daily_budget <= 0:
            return ""
        if auto and self.remaining <= self.reserve:
            return (f"今日读额度只剩 {self.remaining}/{self.daily_budget} 条（≤ 熔断保留 {self.reserve}），"
                    "自动轮询已暂停，手动运行仍可用；可到「设置 → 预算」调整")
        if not auto and self.remaining <= 0:
            return (f"今日读额度已用完（{self.used_today}/{self.daily_budget} 条），明天自动恢复；"
                    "可到「设置 → 预算」调高「每日读额度」")
        return ""


def current() -> BudgetState:
    with get_conn() as conn:
        today = conn.execute(
            "SELECT COALESCE(SUM(reads_consumed),0) AS c FROM action_log "
            "WHERE created_at>=strftime('%Y-%m-%dT00:00:00Z','now')").fetchone()["c"]
        month = conn.execute(
            "SELECT COALESCE(SUM(reads_consumed),0) AS c FROM action_log "
            "WHERE created_at>=strftime('%Y-%m-01T00:00:00Z','now')").fetchone()["c"]
    return BudgetState(used_today=int(today), daily_budget=config.get_int("daily_read_budget", 330),
                       reserve=max(0, config.get_int("budget_reserve_reads", 20)), used_month=int(month))
