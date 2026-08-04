import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional, Sequence


DDS_SPREADSHEET_ID = "1YCHamDIfI0TMCEuNOXLmNCnbQLThW8-woWOwE-7_cSw"
DDS_SHEET_NAME = "ДДС: месяц"
DDS_CHAT_IDS = frozenset({
    -1003806940668,
    -1003764038215,
})

CURRENCY_KGS = "KGS"
CURRENCY_RUB = "RUB"
CURRENCY_USD = "USD"

_NUMBER = r"(?:\d{1,3}(?:[ ,\u00a0.'’]\d{3})+(?:[,.]\d+)?|\d+(?:[,.]\d+)?)"
_CURRENCY = (
    r"(?:\$|usd|usdt|доллар(?:а|ов)?|"
    r"₽|rub|руб(?:\.|ля|лей)?|"
    r"kgs|kgz|сом(?:а|ов)?)"
)
_SIGN = r"(?P<sign>[+\-−–—]?)"
_AMOUNT_AT_START = re.compile(
    rf"^\s*{_SIGN}\s*(?:"
    rf"(?P<currency_before>{_CURRENCY})\s*(?P<number_before>{_NUMBER})"
    rf"|(?P<number_after>{_NUMBER})\s*(?P<currency_after>{_CURRENCY})"
    rf")(?![\d.,])(?P<tail>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_AMOUNT_ANYWHERE = re.compile(
    rf"{_SIGN}\s*(?:"
    rf"(?P<currency_before>{_CURRENCY})\s*(?P<number_before>{_NUMBER})"
    rf"|(?P<number_after>{_NUMBER})\s*(?P<currency_after>{_CURRENCY})"
    rf")(?![\d.,])",
    re.IGNORECASE,
)
_BALANCE_MARKER = re.compile(r"\bостат(?:ок|ка|ке|ки)\b", re.IGNORECASE)
_ONLY_SEPARATORS = re.compile(r"^[\s,.;:()\-–—]*$")


@dataclass(frozen=True)
class ParsedAmount:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PaymentCandidate:
    amount: Decimal
    currency: str
    description: str
    source_kind: str


@dataclass(frozen=True)
class ParseDecision:
    candidate: Optional[PaymentCandidate]
    reason: str

    @property
    def accepted(self):
        return self.candidate is not None


@dataclass(frozen=True)
class DdsRow:
    payment_date: date
    amount: Decimal
    wallet: str
    purpose: str

    def updates_for_row(self, row_number):
        if row_number < 4:
            raise ValueError("DDS data rows start at row 4")
        return {
            f"D{row_number}:F{row_number}": [[
                self.payment_date.strftime("%d.%m.%Y"),
                float(self.amount),
                self.wallet,
            ]],
            f"H{row_number}": [[self.purpose]],
        }


class MissingWalletMapping(ValueError):
    pass


def _normalize_number(value):
    compact = re.sub(r"[ \u00a0'’]", "", value)
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
    if normalized == "$" or normalized in {"usd", "usdt"} or normalized.startswith("доллар"):
        return CURRENCY_USD
    if normalized == "₽" or normalized == "rub" or normalized.startswith("руб"):
        return CURRENCY_RUB
    if normalized in {"kgs", "kgz"} or normalized.startswith("сом"):
        return CURRENCY_KGS
    raise ValueError(f"Unsupported currency: {value}")


def _parsed_amount_from_match(match, default_negative):
    number = match.group("number_before") or match.group("number_after")
    currency = match.group("currency_before") or match.group("currency_after")

    try:
        amount = Decimal(_normalize_number(number))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {number}") from exc

    sign = match.group("sign")
    if sign == "+":
        signed_amount = amount
    elif sign in {"-", "−", "–", "—"} or default_negative:
        signed_amount = -amount
    else:
        signed_amount = amount

    return ParsedAmount(signed_amount, _currency_code(currency))


def parse_amount_with_currency(value, default_negative=True):
    match = _AMOUNT_ANYWHERE.search(str(value or ""))
    if not match:
        raise ValueError("Amount with a supported currency was not found")
    return _parsed_amount_from_match(match, default_negative=default_negative)


def parse_number(value, default_negative=True):
    match = re.search(rf"{_SIGN}\s*(?P<number>{_NUMBER})(?![\d.,])", str(value or ""))
    if not match:
        raise ValueError("Amount was not found")

    try:
        amount = Decimal(_normalize_number(match.group("number")))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {match.group('number')}") from exc

    sign = match.group("sign")
    if sign == "+":
        return amount
    if sign in {"-", "−", "–", "—"} or default_negative:
        return -amount
    return amount


def detect_currency(*values):
    for value in values:
        match = re.search(_CURRENCY, str(value or ""), re.IGNORECASE)
        if match:
            return _currency_code(match.group(0))
    raise ValueError("Supported currency was not found")


def parse_standalone_payment(text, has_media=False):
    original = str(text or "").strip()
    if not original:
        return ParseDecision(None, "empty_message")

    if _BALANCE_MARKER.match(original):
        return ParseDecision(None, "balance_only")

    match = _AMOUNT_AT_START.match(original)
    if not match:
        return ParseDecision(None, "amount_not_at_start")

    parsed = _parsed_amount_from_match(match, default_negative=True)
    tail_before_balance = _BALANCE_MARKER.split(match.group("tail"), maxsplit=1)[0]
    has_description = not _ONLY_SEPARATORS.fullmatch(tail_before_balance or "")

    if not has_description and not has_media:
        return ParseDecision(None, "amount_without_description_or_media")

    return ParseDecision(
        PaymentCandidate(
            amount=parsed.amount,
            currency=parsed.currency,
            description=original,
            source_kind="standalone_chat_payment",
        ),
        "accepted",
    )


def build_bot_invoice_candidate(request_id, amount_text, target, comment):
    amount = parse_number(amount_text, default_negative=True)
    currency = detect_currency(amount_text, comment, target)
    description_parts = [f"Счет #{request_id}"]
    if str(target or "").strip():
        description_parts.append(str(target).strip())
    if str(comment or "").strip():
        description_parts.append(str(comment).strip())

    return PaymentCandidate(
        amount=amount,
        currency=currency,
        description=": ".join(description_parts[:2]) + (
            f"\n{description_parts[2]}" if len(description_parts) > 2 else ""
        ),
        source_kind="bot_invoice",
    )


def resolve_wallet(user_id, currency, wallets_by_user):
    user_wallets = wallets_by_user.get(str(user_id), {})
    wallet = str(user_wallets.get(currency, "")).strip()
    if not wallet:
        raise MissingWalletMapping(
            f"No DDS wallet mapping for Telegram user {user_id} and {currency}"
        )
    return wallet


def resolve_wallet_for_payer(
    user_id,
    username,
    currency,
    wallets_by_user,
    wallets_by_username,
):
    user_wallets = wallets_by_user.get(str(user_id), {})
    wallet = str(user_wallets.get(currency, "")).strip()
    if wallet:
        return wallet

    normalized_username = str(username or "").strip().lstrip("@").lower()
    username_wallets = wallets_by_username.get(normalized_username, {})
    wallet = str(username_wallets.get(currency, "")).strip()
    if wallet:
        return wallet

    raise MissingWalletMapping(
        f"No DDS wallet mapping for Telegram user {user_id} "
        f"(@{normalized_username or 'unknown'}) and {currency}"
    )


def build_dds_row(candidate, payment_date, payer_user_id, wallets_by_user):
    return DdsRow(
        payment_date=payment_date,
        amount=candidate.amount,
        wallet=resolve_wallet(
            payer_user_id,
            candidate.currency,
            wallets_by_user,
        ),
        purpose=candidate.description,
    )


def event_key(chat_id, message_id):
    return f"{int(chat_id)}:{int(message_id)}"


def event_is_in_scope(
    chat_id,
    event_time,
    enabled,
    start_at,
    allowed_chat_ids=DDS_CHAT_IDS,
):
    if not enabled or int(chat_id) not in allowed_chat_ids:
        return False
    if not isinstance(event_time, datetime) or not isinstance(start_at, datetime):
        raise TypeError("event_time and start_at must be datetime values")
    if event_time.tzinfo is None or start_at.tzinfo is None:
        raise ValueError("event_time and start_at must be timezone-aware")
    return event_time >= start_at


def find_next_available_row(
    rows: Iterable[Sequence[object]],
    start_row: int,
):
    if start_row < 4:
        raise ValueError("DDS data rows start at row 4")

    for offset, row in enumerate(rows):
        values = list(row)
        if len(values) != 4:
            raise ValueError("Each row must contain D, E, F and H values")
        if not any(str(value or "").strip() for value in values):
            return start_row + offset

    raise ValueError("No empty DDS row was found in the inspected range")


def decimal_for_sheets(value):
    normalized = Decimal(value)
    return format(normalized, "f")
