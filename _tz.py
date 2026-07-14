"""
Single source of truth for Stockholm local time (CEST/CET).

zoneinfo resolves the correct UTC offset for any exact instant from the
official IANA tzdata — correct across the DST transition day itself, unlike
a date-only offset table (which cannot represent "the offset changes
partway through this day").
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

STHLM = ZoneInfo("Europe/Stockholm")


def local_now() -> datetime:
    return datetime.now(STHLM)


def local_today() -> date:
    return local_now().date()


def local_day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """UTC-aware (start, end) for local midnight..23:59:59 of date d.
    Safe across the DST transition day itself — Sweden's switch never
    happens at midnight, so local midnight is always unambiguous."""
    start_local = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=STHLM)
    end_local   = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=STHLM)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.astimezone(STHLM)


def is_past_local_day(date_str: str) -> bool:
    return date.fromisoformat(date_str) < local_today()
