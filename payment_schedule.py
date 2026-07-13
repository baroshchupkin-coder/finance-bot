import re
from datetime import date, datetime, time, timedelta


def _next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1


def parse_payment_date(value, today=None):
    today = today or date.today()
    value = str(value or "").strip()

    if not value:
        raise ValueError("Укажите дату оплаты.")

    for date_format in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            result = datetime.strptime(value, date_format).date()
        except ValueError:
            continue

        if result < today:
            raise ValueError("Дата оплаты не может быть в прошлом.")
        return result

    short_match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", value)
    if short_match:
        day, month = map(int, short_match.groups())
        for year in range(today.year, today.year + 9):
            try:
                result = date(year, month, day)
            except ValueError:
                continue
            if result >= today:
                return result
        raise ValueError("Введите корректную дату оплаты.")

    day_match = re.fullmatch(r"\d{1,2}", value)
    if day_match:
        day = int(value)
        year, month = today.year, today.month

        for _ in range(24):
            try:
                result = date(year, month, day)
            except ValueError:
                year, month = _next_month(year, month)
                continue

            if result >= today:
                return result
            year, month = _next_month(year, month)

        raise ValueError("Введите корректный день оплаты.")

    raise ValueError("Введите дату в формате ДД.ММ, ДД.ММ.ГГГГ или выберите ее в календаре.")


def payment_dispatch_date(payment_due_date):
    if payment_due_date.weekday() == 5:
        return payment_due_date - timedelta(days=1)
    if payment_due_date.weekday() == 6:
        return payment_due_date - timedelta(days=2)
    return payment_due_date


def should_dispatch_payment(payment_due_date, now, dispatch_hour=10, dispatch_minute=0):
    dispatch_date = payment_dispatch_date(payment_due_date)
    if dispatch_date < now.date():
        return True
    if dispatch_date > now.date():
        return False
    return now.time() >= time(hour=dispatch_hour, minute=dispatch_minute)


def format_payment_date(value):
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return value.strftime("%d.%m.%Y")
