"""读额度（设置 → 预算）的实际执行。

只统计**官方 API 通道**的读取（X 只对它按条计费）；小号 Cookie 通道的读取免费、不占额度，只在仪表盘另行显示。
- daily_read_budget：今天（UTC）最多从官方 API 读多少条。
- budget_reserve_reads：熔断保留——自动轮询在「剩余 < 保留数」时就停，把最后一点额度留给手动操作。
手动运行只在额度完全用完时拒绝；自动轮询更保守。
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..db.database import get_conn

# 官方 API 按量计费的读单价（美元/条），仅用于仪表盘估算；实际以开发者后台账单为准
OFFICIAL_READ_USD = 0.005
# 计入读额度的通道（x_mock 只在自动化测试里出现，当作官方通道算）
_BILLED_KINDS = "('x_official','x_mock')"


@dataclass(frozen=True)
class BudgetState:
    used_today: int          # 今天官方通道读取条数（计费、占额度）
    daily_budget: int
    reserve: int
    used_month: int          # 本月官方通道读取条数
    free_today: int = 0      # 今天小号通道读取条数（不计费、不占额度）

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
            f"SELECT COALESCE(SUM(reads_consumed),0) AS c FROM action_log WHERE api_kind IN {_BILLED_KINDS} "
            "AND created_at>=strftime('%Y-%m-%dT00:00:00Z','now')").fetchone()["c"]
        month = conn.execute(
            f"SELECT COALESCE(SUM(reads_consumed),0) AS c FROM action_log WHERE api_kind IN {_BILLED_KINDS} "
            "AND created_at>=strftime('%Y-%m-01T00:00:00Z','now')").fetchone()["c"]
        free = conn.execute(
            "SELECT COALESCE(SUM(reads_consumed),0) AS c FROM action_log WHERE api_kind='x_unofficial' "
            "AND created_at>=strftime('%Y-%m-%dT00:00:00Z','now')").fetchone()["c"]
    return BudgetState(used_today=int(today), daily_budget=config.get_int("daily_read_budget", 330),
                       reserve=max(0, config.get_int("budget_reserve_reads", 20)), used_month=int(month),
                       free_today=int(free))
