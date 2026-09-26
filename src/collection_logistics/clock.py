"""可注入的 UTC 时间源。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        if self.current.tzinfo is None:
            raise ValueError("冻结时钟必须带时区")
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str, field: str = "时间") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def load_timezone(value: str, field: str = "timezone") -> ZoneInfo | timezone:
    """把 IANA 名称解析为具体 tzinfo，并在该时区真正支持 DST/历史偏移时才接受。"""
    if value == "UTC":
        return timezone.utc
    try:
        zone = ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{field} 不是受支持的 IANA 时区") from exc
    # /usr/share/zoneinfo 缺失时 ZoneInfo 可能返回无偏移占位，必须显式拒绝。
    if zone.utcoffset(datetime(2026, 1, 1)) is None:
        raise ValueError(f"{field} 不是受支持的 IANA 时区")
    return zone


def add_minutes(value: datetime, minutes: int) -> datetime:
    """从带时区的时刻加上整数分钟。

    datetime 与 timedelta 的算术直接落在 UTC 时间线上，因此自动正确处理
    跨日（包括本地日历回卷/前进）与夏令时跳变；本地显示时刻按 tzinfo
    重新换算，不会凭空多出或丢失一小时。
    """
    if value.tzinfo is None:
        raise ValueError("出发时刻必须带时区")
    return value + timedelta(minutes=minutes)
