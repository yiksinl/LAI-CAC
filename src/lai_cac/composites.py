from __future__ import annotations

from datetime import date, timedelta

TRAINING_END = date(2026, 4, 6)
TARGET_HOURS_UTC = (15, 18, 21)
COMPOSITE_DAYS = 8
POST_CUTOFF_ANCHOR = date(2026, 4, 7)


def aligned_period(start: date) -> tuple[date, date]:
    """Return a fixed notebook-calendar 8-day period; reject off-grid starts."""
    delta = (start - POST_CUTOFF_ANCHOR).days
    if delta % COMPOSITE_DAYS:
        raise ValueError(
            f"{start.isoformat()} is not aligned to the post-cutoff "
            f"{POST_CUTOFF_ANCHOR.isoformat()} 8-day calendar"
        )
    return start, start + timedelta(days=COMPOSITE_DAYS - 1)


def periods_after_cutoff(today: date | None = None) -> list[tuple[date, date]]:
    today = today or date.today()
    first = POST_CUTOFF_ANCHOR
    result = []
    while first + timedelta(days=COMPOSITE_DAYS - 1) < today:
        result.append((first, first + timedelta(days=COMPOSITE_DAYS - 1)))
        first += timedelta(days=COMPOSITE_DAYS)
    return result
