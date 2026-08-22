from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from .domain import Alert, parse_timestamp


SEOUL = ZoneInfo("Asia/Seoul")
HISTORY_DAYS = 7


def history_utc_range(now: datetime | None = None) -> tuple[date, date, datetime, datetime]:
    current = now or datetime.now(UTC)
    local_today = current.astimezone(SEOUL).date()
    start_date = local_today - timedelta(days=HISTORY_DAYS - 1)
    end_date = local_today
    start_local = datetime.combine(start_date, time.min, tzinfo=SEOUL)
    end_exclusive_local = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=SEOUL)
    return start_date, end_date, start_local.astimezone(UTC), end_exclusive_local.astimezone(UTC)


def group_history(alerts: Iterable[Alert], now: datetime | None = None) -> dict:
    start_date, end_date, _, _ = history_utc_range(now)
    day_map: dict[str, list[dict]] = {
        (end_date - timedelta(days=offset)).isoformat(): []
        for offset in range(HISTORY_DAYS)
    }
    total_count = 0
    ordered = sorted(alerts, key=lambda item: parse_timestamp(item.timestamp), reverse=True)
    for alert in ordered:
        local_timestamp = parse_timestamp(alert.timestamp).astimezone(SEOUL)
        key = local_timestamp.date().isoformat()
        if key not in day_map:
            continue
        payload = alert.public()
        payload["local_time"] = local_timestamp.isoformat()
        day_map[key].append(payload)
        total_count += 1
    days = []
    for offset, (day, values) in enumerate(day_map.items()):
        label = "오늘" if offset == 0 else "어제" if offset == 1 else None
        days.append({"date": day, "display_label": label, "alarms": values})
    return {
        "timezone": "Asia/Seoul",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_count": total_count,
        "days": days,
    }
