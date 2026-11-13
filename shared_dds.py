import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from dds_integration import (
    CURRENCY_KGS,
    CURRENCY_USD,
    CURRENCY_USDT,
    add_message_link,
    parse_accountable_topup,
    parse_shared_dds_payments,
)


SHARED_DDS_SPREADSHEET_ID = "1U8fsHqDhQl69fwXAVHlAhwzFQLpF1pWpEjzUV6351Lw"
SHARED_DDS_CHAT_ID = -1003721730980
SHARED_DDS_OFFICE_CHAT_ID = -1003764038215
SHARED_DDS_CHAT_IDS = frozenset({SHARED_DDS_CHAT_ID, SHARED_DDS_OFFICE_CHAT_ID})

_ALEXANDER_WALLETS = {
    CURRENCY_KGS: "Александр С",
    CURRENCY_USD: "Александр D",
    CURRENCY_USDT: "Александр U",
}
_KONSTANTIN_WALLETS = {
    CURRENCY_KGS: "Константин С",
    CURRENCY_USDT: "Константин U",
}
_OFFICE_WALLET = "Офис подотчет"
_OFFICE_DATE_PREFIX = re.compile(
    r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?[ \t]+[-–—:][ \t]+"
)
_ACCOUNT_WALLETS = {
    "ор": "р/c ОР", "or": "р/c ОР",
    "ст": "р/c СТ", "st": "р/c СТ",
    "вл": "р/c ВЛ", "vl": "р/c ВЛ",
    "скп": "р/c СКП", "skp": "р/c СКП",
}
_ACCOUNT_MARKER = re.compile(
    r"(?<!\w)(?:р\s*[/\.]\s*[сc]\.?|рс|r\.?\s*s\.?|"
    r"расч[её]тн\w*\s+сч[её]т\w*)(?!\w)",
    re.IGNORECASE,
)
_ACCOUNT_CODE = re.compile(r"[\s:–—-]*([а-яёa-z]+)(?!\w)", re.IGNORECASE)

_PERSONAL_BALANCE_CELLS = (
    ("Александр U", 0, 3),
    ("Александр С", 1, 3),
    ("Александр D", 1, 2),
    ("Константин U", 0, 5),
    ("Константин С", 1, 5),
)
_ACCOUNT_BALANCE_WALLETS = tuple(dict.fromkeys(_ACCOUNT_WALLETS.values())) + (_OFFICE_WALLET,)
_BALANCE_VALUE = re.compile(
    r"([+\-−]?)\s*([U$]?)\s*(\d+(?:[ \u00a0\u202f]\d{3})*(?:[,.]\d+)?)",
    re.IGNORECASE,
)
_BALANCE_UNITS = {"U": "USDT", "$": "$", "": "сом"}
_BALANCE_TOTALS = (
    ("сом", (_ALEXANDER_WALLETS[CURRENCY_KGS], _KONSTANTIN_WALLETS[CURRENCY_KGS], *_ACCOUNT_BALANCE_WALLETS)),
    ("$", (_ALEXANDER_WALLETS[CURRENCY_USD],)),
    ("USDT", (_ALEXANDER_WALLETS[CURRENCY_USDT], _KONSTANTIN_WALLETS[CURRENCY_USDT])),
)


def resolve_shared_wallet(chat_id, username, currency, purpose):
    if chat_id not in SHARED_DDS_CHAT_IDS:
        return ""
    hints = list(_ACCOUNT_MARKER.finditer(purpose))
    if hints:
        if len(hints) != 1 or currency != CURRENCY_KGS:
            return ""
        account = _ACCOUNT_CODE.match(purpose, hints[0].end())
        return _ACCOUNT_WALLETS.get(account.group(1).casefold(), "") if account else ""
    if chat_id == SHARED_DDS_OFFICE_CHAT_ID:
        return _OFFICE_WALLET if currency == CURRENCY_KGS else ""
    username = str(username or "").strip().lstrip("@").casefold()
    if username in {"n0visad", "roshchupkin_egor"}:
        return _ALEXANDER_WALLETS.get(currency, "")
    if username == "ba_roshchupkin":
        return _KONSTANTIN_WALLETS.get(currency, "")
    return ""


def with_shared_wallet(candidate, chat_id, username):
    return replace(candidate, wallet=resolve_shared_wallet(
        chat_id, username, candidate.currency, candidate.description,
    ))


def parse_shared_office_topup(text):
    original = str(text or "").strip()
    body = original
    prefix = _OFFICE_DATE_PREFIX.match(body)
    if prefix:
        day, month, year = prefix.groups()
        try:
            date(int(year or 2000), int(month), int(day))
        except ValueError:
            pass
        else:
            body = body[prefix.end():]

    decision = parse_accountable_topup(
        body, {CURRENCY_KGS: _ALEXANDER_WALLETS[CURRENCY_KGS]}, reject_negative=True,
    )
    if not decision.accepted:
        return decision
    expense, income = decision.candidates
    return replace(decision, candidates=(
        replace(expense, description=original),
        replace(income, description=original, wallet=_OFFICE_WALLET),
    ))


def build_shared_paid_invoice(request_id, amount_text, target, comment, chat_id, username, link):
    decision = parse_shared_dds_payments(f"{amount_text.strip()} оплата", allow_income=False)
    if len(decision.candidates) != 1:
        raise ValueError(f"Unrecognized amount for shared DDS invoice {request_id}")
    purpose = "\n".join(part for part in (f"Счет #{request_id}", target, comment) if part)
    candidate = replace(decision.candidates[0], description=purpose, source_kind="shared_dds_bot_invoice")
    return add_message_link(with_shared_wallet(candidate, chat_id, username), link)


def _balance_cell(rows, row, column):
    return str(rows[row][column]).strip() if row < len(rows) and column < len(rows[row]) else ""


def format_shared_balance(value):
    match = _BALANCE_VALUE.fullmatch(value)
    if not match:
        return "нет данных"
    sign, marker, number = match.groups()
    unit = _BALANCE_UNITS[marker.upper()]
    return f"{sign}{number.strip()} {unit}"


def _format_balance_total(values, wallets, unit):
    total = Decimal(0)
    for wallet in wallets:
        match = _BALANCE_VALUE.fullmatch(values.get(wallet, ""))
        if not match or _BALANCE_UNITS[match.group(2).upper()] != unit:
            return f"{unit}: нет данных"
        sign, _, number = match.groups()
        amount = Decimal("".join(number.split()).replace(",", "."))
        total += -amount if sign in {"-", "−"} else amount
    formatted = f"{total:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} {unit}"


def format_shared_balance_report(rows, report_date):
    values = {
        name: _balance_cell(rows, row, column)
        for name, row, column in _PERSONAL_BALANCE_CELLS
    }
    for row in range(2):
        for column in range(6, 16, 2):
            name = _balance_cell(rows, row, column)
            if name in _ACCOUNT_BALANCE_WALLETS:
                values.setdefault(name, _balance_cell(rows, row, column + 1))
    names = [name for name, _, _ in _PERSONAL_BALANCE_CELLS] + list(_ACCOUNT_BALANCE_WALLETS)
    return "\n".join([
        f"Остатки на {report_date:%d.%m.%Y}",
        "",
        *(f"{name}: {format_shared_balance(values.get(name, ''))}" for name in names),
        "",
        "Итого:",
        *(_format_balance_total(values, wallets, unit) for unit, wallets in _BALANCE_TOTALS),
    ])
