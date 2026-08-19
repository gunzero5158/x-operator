"""定时计划下次运行时间计算（design-v1.1 §7.6，MVP 支持 once/daily/weekly）。

cron 类型 MVP 暂只支持 5 段里的简单 'M H * * *'（时分）解析，复杂表达式留待接 croniter。
返回 UTC datetime；无后续则 None。表达式非法抛 ValueError（中文）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


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
