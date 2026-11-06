import re
from dataclasses import replace

from dds_integration import (
    CURRENCY_KGS,
    CURRENCY_USD,
    CURRENCY_USDT,
    add_message_link,
    parse_shared_dds_expenses,
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
_BALANCE_VALUE = re.compile(r"([+\-−]?)\s*([U$]?)\s*(\d[\d\s,.]*)", re.IGNORECASE)


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


def build_shared_paid_invoice(request_id, amount_text, target, comment, chat_id, username, link):
    decision = parse_shared_dds_expenses(f"{amount_text.strip()} оплата")
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
    unit = {"U": "USDT", "$": "$", "": "сом"}[marker.upper()]
    return f"{sign}{number.strip()} {unit}"


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
    ])
