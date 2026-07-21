import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


TAXI_SUMMARY_DAYS = {5, 20}


def taxi_period_for_run_date(run_date):
    if run_date.day == 5:
        previous_month_last_day = run_date.replace(day=1) - timedelta(days=1)
        period_start = previous_month_last_day.replace(day=20)
        return period_start, run_date

    if run_date.day == 20:
        return run_date.replace(day=5), run_date

    return None


def is_taxi_summary_time(now, dispatch_hour=10, dispatch_minute=0):
    return (
        now.day in TAXI_SUMMARY_DAYS
        and (now.hour, now.minute) >= (dispatch_hour, dispatch_minute)
    )


def format_taxi_period(period_start, period_end_exclusive):
    period_end = period_end_exclusive - timedelta(days=1)
    return f"{period_start:%d.%m.%Y}-{period_end:%d.%m.%Y}"


def taxi_summary_key(period_start, period_end_exclusive, project, creator_chat_id):
    normalized_project = " ".join((project or "").strip().lower().split())
    return (
        f"taxi-summary|{period_start.isoformat()}|{period_end_exclusive.isoformat()}|"
        f"{normalized_project}|{creator_chat_id}"
    )


def parse_taxi_amount(value):
    text = str(value or "").replace("\u00a0", " ").strip()
    matches = re.findall(r"\d[\d\s.,]*", text)
    if len(matches) != 1:
        raise ValueError(f"Не удалось распознать сумму: {value}")

    token = matches[0].replace(" ", "")
    if "," in token and "." in token:
        decimal_separator = "," if token.rfind(",") > token.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        token = token.replace(thousands_separator, "")
        token = token.replace(decimal_separator, ".")
    elif "," in token or "." in token:
        separator = "," if "," in token else "."
        parts = token.split(separator)
        if len(parts) > 2 or len(parts[-1]) == 3:
            token = "".join(parts)
        else:
            token = ".".join(parts)

    try:
        amount = Decimal(token)
    except InvalidOperation as exc:
        raise ValueError(f"Не удалось распознать сумму: {value}") from exc

    if amount <= 0:
        raise ValueError("Сумма такси должна быть больше нуля.")

    return amount


def format_taxi_amount(amount):
    amount = Decimal(amount)
    if amount == amount.to_integral():
        integer = f"{int(amount):,}".replace(",", " ")
        return integer

    formatted = f"{amount:.2f}".rstrip("0").rstrip(".")
    integer, fraction = formatted.split(".", 1)
    integer = f"{int(integer):,}".replace(",", " ")
    return f"{integer},{fraction}"

def group_taxi_entries(entries):
    groups = {}
    for entry in entries:
        project = str(entry.get("project") or "").strip()
        creator_chat_id = str(entry.get("creator_chat_id") or "").strip()
        group_key = (" ".join(project.lower().split()), creator_chat_id)
        group = groups.setdefault(group_key, {
            "project": project,
            "creator_chat_id": creator_chat_id,
            "creator_username": entry.get("creator_username", ""),
            "creator_name": entry.get("creator_name", ""),
            "source_request_ids": [],
            "total": None,
            "invalid_amounts": [],
        })
        group["source_request_ids"].append(entry.get("request_id", ""))

        try:
            amount = parse_taxi_amount(entry.get("amount"))
        except ValueError:
            group["invalid_amounts"].append((
                entry.get("sheet_row_number"),
                entry.get("request_id", ""),
                entry.get("amount", ""),
            ))
            continue

        group["total"] = amount if group["total"] is None else group["total"] + amount

    return groups
