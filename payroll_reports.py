import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation


CURRENCY_KGS = "KGS"
CURRENCY_USD = "USD"
PROJECT_VL = "VL"
PROJECT_OR = "OR"
REPORT_PROJECTS = (PROJECT_VL, PROJECT_OR)

_NUMBER = r"(?:\d{1,3}(?:[ .,'’]\d{3})+(?:[,.]\d+)?|\d+(?:[,.]\d+)?)"
_KGS = r"(?:kgs|kgz|som(?:s)?|s|с|сом(?:а|ов)?)"
_USD = r"(?:\$|usd|usdt|доллар(?:а|ов)?)"
_CURRENCY = rf"(?:{_KGS}|{_USD})"
_AMOUNT = re.compile(
    rf"^\s*[+\-−–—]?\s*(?:"
    rf"(?P<currency_before>{_CURRENCY})\s*(?P<number_before>{_NUMBER})"
    rf"|(?P<number_after>{_NUMBER})\s*(?P<currency_after>{_CURRENCY})"
    rf")\s*\.?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReportAmount:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PayrollReport:
    project: str
    due_date: date
    kgs_total: Decimal
    usd_total: Decimal
    unrecognized: tuple
    invoice_count: int


def _normalize_number(value):
    compact = re.sub(r"[ '’]", "", value)
    separators = [char for char in compact if char in ".,"]
    if not separators:
        return compact

    last_separator = max(compact.rfind("."), compact.rfind(","))
    digits_after = len(compact) - last_separator - 1
    if digits_after in (1, 2):
        integer = re.sub(r"[.,]", "", compact[:last_separator])
        fraction = compact[last_separator + 1:]
        return f"{integer}.{fraction}"
    return re.sub(r"[.,]", "", compact)


def _currency_code(value):
    normalized = value.strip().lower().replace(".", "")
    if re.fullmatch(_USD, normalized, re.IGNORECASE):
        return CURRENCY_USD
    if re.fullmatch(_KGS, normalized, re.IGNORECASE):
        return CURRENCY_KGS
    raise ValueError(f"Unsupported payroll currency: {value}")


def parse_report_amount(value):
    match = _AMOUNT.fullmatch(str(value or ""))
    if not match:
        return None

    number = match.group("number_before") or match.group("number_after")
    currency = match.group("currency_before") or match.group("currency_after")
    try:
        amount = abs(Decimal(_normalize_number(number)))
    except InvalidOperation:
        return None
    if amount <= 0:
        return None
    return ReportAmount(amount=amount, currency=_currency_code(currency))


def normalize_project_group(value):
    normalized = re.sub(r"[\s_\-]+", "", str(value or "")).upper()
    if normalized in {"VL", "ВЛ"}:
        return PROJECT_VL
    if normalized in {"OR", "ОР", "ORKG", "ОРКГ"}:
        return PROJECT_OR
    return None


def payroll_report_date(payment_due_date):
    current = payment_due_date
    working_days = 0
    while working_days < 2:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            working_days += 1
    return current


def due_dates_for_payroll_report(now, report_hour=12, report_minute=0):
    due_dates = []
    for offset in range(0, 36):
        candidate = now.date() + timedelta(days=offset)
        if candidate.day not in {10, 25}:
            continue

        report_date = payroll_report_date(candidate)
        if report_date > now.date():
            continue
        if report_date == now.date() and now.time() < time(report_hour, report_minute):
            continue
        due_dates.append(candidate)
    return tuple(due_dates)


def build_payroll_report(invoices, project, due_date):
    kgs_total = Decimal("0")
    usd_total = Decimal("0")
    unrecognized = []
    invoice_count = 0

    for invoice in invoices:
        if normalize_project_group(invoice.get("project")) != project:
            continue
        if invoice.get("due_date") != due_date:
            continue

        invoice_count += 1
        parsed = parse_report_amount(invoice.get("amount"))
        if parsed is None:
            payee = str(invoice.get("payee") or "Кому платим не указано").strip()
            raw_amount = str(invoice.get("amount") or "сумма не указана").strip()
            unrecognized.append((payee, raw_amount))
        elif parsed.currency == CURRENCY_KGS:
            kgs_total += parsed.amount
        else:
            usd_total += parsed.amount

    return PayrollReport(
        project=project,
        due_date=due_date,
        kgs_total=kgs_total,
        usd_total=usd_total,
        unrecognized=tuple(unrecognized),
        invoice_count=invoice_count,
    )


def format_report_number(value):
    rendered = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return rendered.rstrip("0").rstrip(",")


def format_payroll_report(report):
    lines = [
        f"Проект {report.project} — к оплате {report.due_date.strftime('%d.%m.%Y')}",
        "",
        f"В сомах: {format_report_number(report.kgs_total)} сом",
        f"В долларах: {format_report_number(report.usd_total)} $",
        "",
        "Нераспознанные счета:",
    ]
    if report.unrecognized:
        lines.extend(f"{payee} — {amount}" for payee, amount in report.unrecognized)
    else:
        lines.append("Нет")
    return "\n".join(lines)


def payroll_report_key(due_date, project):
    return f"payroll-report|{due_date.isoformat()}|{project}"


def collect_sent_report_keys(rows):
    keys = set()
    for row in rows[1:]:
        for column in (0, 7):
            if len(row) <= column:
                continue
            value = str(row[column] or "").strip()
            if value.startswith("payroll-report|"):
                keys.add(value)
    return keys


def next_report_log_row(rows):
    for row_number, row in enumerate(rows[1:], start=2):
        if not row or not str(row[0] or "").strip():
            return row_number
    return len(rows) + 1
