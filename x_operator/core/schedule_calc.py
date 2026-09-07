"""定时发帖下次运行时间计算（design-v1.1 §7.6）：once / daily / weekly / interval（每隔 N 小时或分钟）。

cron 类型只为兼容旧数据保留（只支持 'M H * * *'，等价于 daily），界面上不再提供。
返回 UTC datetime；无后续则 None。表达式非法抛 ValueError（中文）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MIN_INTERVAL_MINUTES = 10


def parse_interval(expr: str) -> tuple[int, str]:
    """「每隔」表达式：'6h' / '6 小时' / '6'（默认小时） / '90m' / '90 分钟'。返回 (数值, 'h'|'m')。"""
    s = (expr or "").strip().lower().replace(" ", "")
    m = re.match(r"^(\d+)(h|hour|hours|小时|m|min|minutes|分钟|分)?$", s)
    if not m:
        raise ValueError(f"「每隔」格式非法（例：6h = 每 6 小时，90m = 每 90 分钟）：{expr}")
    n = int(m.group(1)); unit = "m" if (m.group(2) or "h").startswith(("m", "分")) else "h"
    minutes = n if unit == "m" else n * 60
    if minutes < MIN_INTERVAL_MINUTES:
        raise ValueError(f"间隔太短：最少 {MIN_INTERVAL_MINUTES} 分钟")
    if minutes > 24 * 60 * 30:
        raise ValueError("间隔太长：最多 30 天，更长请用一次性计划")
    return n, unit


def describe_interval(expr: str) -> str:
    try:
        n, unit = parse_interval(expr)
    except ValueError:
        return expr
    return f"每隔 {n} {'小时' if unit == 'h' else '分钟'}"


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Tokyo")


def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        return h, m
    except Exception:
        raise ValueError(f"时间格式非法（应为 HH:MM）：{s}")


def compute_next_run(schedule_type: str, schedule_expr: str, after: datetime, tz: str) -> datetime | None:
    zone = _tz(tz)
    after_local = after.astimezone(zone)

    if schedule_type == "once":
        try:
            dt = datetime.strptime(schedule_expr.strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=zone)
        except ValueError:
            raise ValueError(f"一次性时间格式非法（应为 YYYY-MM-DDTHH:MM）：{schedule_expr}")
        return dt.astimezone(timezone.utc) if dt > after else None

    if schedule_type == "daily":
        h, m = _parse_hhmm(schedule_expr)
        cand = after_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= after_local:
            cand += timedelta(days=1)
        return cand.astimezone(timezone.utc)

    if schedule_type == "weekly":
        # 'mon,thu 21:00'
        try:
            days_part, time_part = schedule_expr.strip().split()
        except ValueError:
            raise ValueError(f"每周表达式非法（应为 'mon,thu 21:00'）：{schedule_expr}")
        h, m = _parse_hhmm(time_part)
        targets = []
        for d in days_part.split(","):
            key = d.strip().lower()[:3]
            if key not in _WEEKDAYS:
                raise ValueError(f"星期缩写非法：{d}")
            targets.append(_WEEKDAYS[key])
        # 找最近的目标星期
        best = None
        for add in range(0, 8):
            cand = (after_local + timedelta(days=add)).replace(hour=h, minute=m, second=0, microsecond=0)
            if cand.weekday() in targets and cand > after_local:
                best = cand
                break
        return best.astimezone(timezone.utc) if best else None

    if schedule_type == "interval":
        n, unit = parse_interval(schedule_expr)
        return (after + (timedelta(hours=n) if unit == "h" else timedelta(minutes=n))).astimezone(timezone.utc)

    if schedule_type == "cron":
        parts = schedule_expr.strip().split()
        if len(parts) != 5:
            raise ValueError("cron 表达式须为 5 段（MVP 仅支持 'M H * * *'）")
        minute, hour, dom, mon, dow = parts
        if dom != "*" or mon != "*" or dow != "*":
            raise ValueError("MVP 的 cron 仅支持 'M H * * *'（每日固定时分），复杂表达式待后续支持")
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            raise ValueError("cron 时分须为整数")
        return compute_next_run("daily", f"{h:02d}:{m:02d}", after, tz)

    raise ValueError(f"未知的计划类型：{schedule_type}")
