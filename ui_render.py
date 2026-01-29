from __future__ import annotations

from datetime import datetime


def fmt_price(price) -> str:
    """Цена без Markdown/HTML."""
    try:
        if price is None:
            return "Бесплатно"
        p = float(price)
        if p <= 0:
            return "Бесплатно"
        return f"{int(p)} ₽"
    except Exception:
        return "Бесплатно"


def _seats_word_ru(n: int) -> str:
    n = abs(int(n))
    if 11 <= (n % 100) <= 14:
        return "мест"
    last = n % 10
    if last == 1:
        return "место"
    if 2 <= last <= 4:
        return "места"
    return "мест"


def fmt_datetime(date: datetime | None, time_str: str = "") -> str:
    if not date:
        return "—"
    d = date.strftime("%d.%m.%Y")
    t = time_str.strip()
    if t:
        return f"{d} • {t}"
    return d


def render_trip_card(
    *,
    title: str = "🚗 Поездка",
    date: datetime | None,
    time_str: str,
    departure: str,
    destination: str,
    seats_available: int,
    price,
    action_hint: str | None = None,
    status: str | None = None,
    show_driver: str | None = None,
) -> str:
    dt_line = fmt_datetime(date, time_str)
    price_line = fmt_price(price)
    seats_line = f"{int(seats_available)} {_seats_word_ru(int(seats_available))}"

    lines: list[str] = [title]
    if status:
        lines.append(status)
    lines.append(f"🟢 {dt_line}")
    lines.append(f"📍 {departure} → {destination}")
    lines.append(f"👥 {seats_line} • 💰 {price_line}")
    if show_driver:
        lines.append(f"🧑‍✈️ {show_driver}")
    return "\n".join(lines)


def render_booking_card(
    *,
    title: str = "🎫 Бронирование",
    date: datetime | None,
    time_str: str,
    departure: str,
    destination: str,
    seats_booked: int,
    price,
    status: str,
    driver_name: str | None = None,
    driver_username: str | None = None,
    action_hint: str | None = None,
) -> str:
    dt_line = fmt_datetime(date, time_str)
    price_line = fmt_price(price)
    seats_line = f"{int(seats_booked)} {_seats_word_ru(int(seats_booked))}"

    lines: list[str] = [title]
    lines.append(f"📍 {departure} → {destination}")
    lines.append(f"🟢 {dt_line}")
    lines.append(f"👥 {seats_line} • 💰 {price_line}")
    if driver_name:
        if driver_username:
            lines.append(f"🧑‍✈️ {driver_name} (@{driver_username})")
        else:
            lines.append(f"🧑‍✈️ {driver_name}")
    lines.append(f"📌 {status}")
    return "\n".join(lines)
