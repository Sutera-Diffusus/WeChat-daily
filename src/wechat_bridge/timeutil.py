"""Timezone helpers with bundled IANA data on Windows."""

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"


def get_timezone(name: str = DEFAULT_TIMEZONE) -> tzinfo:
    value = str(name or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(value)
    except Exception as exc:
        raise ValueError("无法加载时区数据: %s" % value) from exc


def now_in_timezone(name: str = DEFAULT_TIMEZONE) -> datetime:
    return datetime.now(get_timezone(name))


def as_timezone(value: datetime, name: str = DEFAULT_TIMEZONE) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(get_timezone(name))
