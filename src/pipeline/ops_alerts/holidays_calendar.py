"""Argentina public-holiday calendar for the daily reminder.

Self-contained: a fixed-date table plus Easter-derived movable feasts (Carnaval,
Viernes Santo). ``find_tomorrow_holiday`` checks the current and next year so a
December run can match a January holiday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Holiday:
    name: str
    date: date
    population: float  # % of population celebrating (business-impact hint)
    impact: str        # "High" / "Middle" / "Low"


def get_easter_date(year: int) -> date:
    """Western (Gregorian) Easter Sunday — Anonymous Gregorian Algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def get_holidays(year: int) -> list[Holiday]:
    """All Argentina holidays for ``year`` (fixed + Easter-derived)."""
    easter = get_easter_date(year)
    return [
        Holiday("Año Nuevo", date(year, 1, 1), 92.5, "High"),
        Holiday("Carnaval (Lunes)", easter - timedelta(days=48), 75, "High"),
        Holiday("Carnaval (Martes)", easter - timedelta(days=47), 75, "High"),
        Holiday("Día de la Memoria", date(year, 3, 24), 80, "High"),
        Holiday("Día del Veterano (Malvinas)", date(year, 4, 2), 78, "High"),
        Holiday("Viernes Santo", easter - timedelta(days=2), 75, "Low"),
        Holiday("Día del Trabajador", date(year, 5, 1), 85, "High"),
        Holiday("Revolución de Mayo", date(year, 5, 25), 80, "High"),
        Holiday("Belgrano", date(year, 6, 20), 75, "Middle"),
        Holiday("Día de la Independencia", date(year, 7, 9), 96.5, "High"),
        Holiday("Día del Amigo", date(year, 7, 20), 75, "High"),
        Holiday("Día de la Niñez", date(year, 8, 16), 75, "Low"),
        Holiday("San Martín", date(year, 8, 17), 75, "High"),
        Holiday("Diversidad Cultural", date(year, 10, 12), 72, "Middle"),
        Holiday("Soberanía Nacional", date(year, 11, 20), 72, "Middle"),
        Holiday("Inmaculada Concepción", date(year, 12, 8), 72.5, "Middle"),
        Holiday("Navidad", date(year, 12, 25), 92.5, "Middle"),
        Holiday("Nochevieja", date(year, 12, 31), 92.5, "High"),
    ]


def find_tomorrow_holiday(today: date) -> Holiday | None:
    """Return the holiday falling on ``today + 1 day``, or None.

    Searches this year + next so a 31 Dec run can match a 1 Jan holiday.
    """
    tomorrow = today + timedelta(days=1)
    for h in get_holidays(today.year) + get_holidays(today.year + 1):
        if h.date == tomorrow:
            return h
    return None
