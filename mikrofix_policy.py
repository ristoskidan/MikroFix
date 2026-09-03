"""Poslovna pravila, ki morajo ostati enaka v programu in dokumentih."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime


WARRANTY_SCOPE_INSTALLED_PARTS = "INSTALLED_PARTS"
WARRANTY_SCOPE_NONE = "NONE"


def add_calendar_months(value: date | datetime, months: int) -> date:
    """Prišteje koledarske mesece in varno obravnava konec meseca."""
    if isinstance(value, datetime):
        value = value.date()
    months = int(months)
    if months < 0:
        raise ValueError("Število mesecev ne sme biti negativno.")
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def service_parts_warranty(
    completed_at: date | datetime,
    has_installed_parts: bool,
    installed_parts_months: int,
) -> tuple[str, int, str]:
    """Vrne obseg, število mesecev in datum prostovoljne garancije na dele."""
    if not has_installed_parts:
        return WARRANTY_SCOPE_NONE, 0, ""
    months = int(installed_parts_months)
    if months < 1:
        raise ValueError("Garancija na vgrajene dele mora biti vsaj en mesec.")
    valid_until = add_calendar_months(completed_at, months)
    return WARRANTY_SCOPE_INSTALLED_PARTS, months, valid_until.isoformat()
