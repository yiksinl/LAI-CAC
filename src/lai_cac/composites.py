from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

TRAINING_END = date(2026, 4, 6)
TARGET_HOURS_UTC = (15, 18, 21)
COMPOSITE_DAYS = 8
POST_CUTOFF_ANCHOR = date(2026, 4, 7)
ARCHIVE_PUBLICATION_DELAY = timedelta(hours=2)
HISTORY_DAYS = 365


def aligned_period(start: date) -> tuple[date, date]:
    """Return a fixed notebook-calendar 8-day period; reject off-grid starts."""
    if start <= TRAINING_END:
        raise ValueError(
            f"{start.isoformat()} is not after the model-development cutoff "
            f"{TRAINING_END.isoformat()}"
        )
    delta = (start - POST_CUTOFF_ANCHOR).days
    if delta % COMPOSITE_DAYS:
        raise ValueError(
            f"{start.isoformat()} is not aligned to the post-cutoff "
            f"{POST_CUTOFF_ANCHOR.isoformat()} 8-day calendar"
        )
    return start, start + timedelta(days=COMPOSITE_DAYS - 1)


def rolling_period(start: date) -> tuple[date, date]:
    """Return an eight-day rolling window whose observations are all post-training."""
    if start <= TRAINING_END:
        raise ValueError(
            f"{start.isoformat()} is not after the model-development cutoff "
            f"{TRAINING_END.isoformat()}"
        )
    return start, start + timedelta(days=COMPOSITE_DAYS - 1)


def archive_ready_after(end: date) -> datetime:
    """UTC instant after which the final selected observation should be published."""
    last_observation = datetime.combine(end, time(max(TARGET_HOURS_UTC)), timezone.utc)
    return last_observation + ARCHIVE_PUBLICATION_DELAY


def latest_completed_rolling_period(
    now: datetime | None = None,
) -> tuple[date, date]:
    """Newest rolling window after allowing the NOAA archive publication grace period."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("The current time must include a timezone")
    now = now.astimezone(timezone.utc)
    end = now.date()
    if now < archive_ready_after(end):
        end -= timedelta(days=1)
    start = end - timedelta(days=COMPOSITE_DAYS - 1)
    return rolling_period(start)


def rolling_periods_through(
    latest_start: date,
    history_days: int = HISTORY_DAYS,
) -> list[tuple[date, date]]:
    """Daily rolling windows for the available part of the requested history span."""
    if history_days <= 0:
        raise ValueError("History length must be positive")
    latest_start, _ = rolling_period(latest_start)
    earliest = max(
        POST_CUTOFF_ANCHOR,
        latest_start - timedelta(days=history_days - 1),
    )
    return [
        rolling_period(earliest + timedelta(days=offset))
        for offset in range((latest_start - earliest).days + 1)
    ]


def utc_window_boundaries(start: date) -> dict[str, str]:
    start, end = rolling_period(start)
    end_exclusive = end + timedelta(days=1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_utc": f"{start.isoformat()}T00:00:00Z",
        "end_exclusive_utc": f"{end_exclusive.isoformat()}T00:00:00Z",
        "archive_ready_after_utc": archive_ready_after(end).isoformat().replace("+00:00", "Z"),
    }


def periods_after_cutoff(today: date | None = None) -> list[tuple[date, date]]:
    today = today or date.today()
    first = POST_CUTOFF_ANCHOR
    result = []
    while first + timedelta(days=COMPOSITE_DAYS - 1) < today:
        result.append((first, first + timedelta(days=COMPOSITE_DAYS - 1)))
        first += timedelta(days=COMPOSITE_DAYS)
    return result
