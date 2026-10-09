"""Shared helper for reading the portal's own self-reported "day" label.

The cloud portal's day rollover instant is a fixed real-world instant
(whatever timezone its backend clock runs on) that is not guaranteed to
line up with this Home Assistant instance's local midnight. Rather than
asking "did my local date change?" - which can be wrong in either
direction depending on the timezone offset between the portal and this
install - anything that needs to reason about day boundaries should ask
"what day does the portal itself think this reading belongs to?" by
reading the date portion straight out of its own ``lastReportTime``
string. Used by both daily_energy_monitor.py (diagnostics) and
lifetime_energy.py (synthesized lifetime counters).
"""
from __future__ import annotations

from datetime import datetime


def day_string_from_report_time(raw_last_report_time: str | None) -> str | None:
    """Extract a ``yyyyMMdd`` day string from the portal's own ``lastReportTime``.

    ``raw_last_report_time`` is the raw ``"yyyy-MM-dd HH:mm:ss"`` string as
    returned by the portal (see models.parse_control_info). Returns ``None``
    if missing/unparseable.
    """
    if not raw_last_report_time or len(raw_last_report_time) < 10:
        return None
    date_part = raw_last_report_time[:10]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return None
    return date_part.replace("-", "")
