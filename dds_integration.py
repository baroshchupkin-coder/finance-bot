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
_BARE_SIGN = r"(?P<sign>[+\-\u2212\u2013\u2014]?)"
_BARE_AMOUNT_AT_START = re.compile(
    rf"^\s*{_BARE_SIGN}\s*(?P<number>{_NUMBER})(?![\d.,])(?P<tail>.*)$",
    re.DOTALL,
)
_BARE_AMOUNT_AT_END = re.compile(
    rf"^(?P<head>.+?)(?:\s[-\u2013\u2014:]\s*){_BARE_SIGN}\s*"
    rf"(?P<number>{_NUMBER})(?![\d.,])\s*$",
    re.DOTALL,
)
_BALANCE_MARKER = re.compile(r"\bостат(?:ок|ка|ке|ки)\b", re.IGNORECASE)
_ONLY_SEPARATORS = re.compile(r"^[\s,.;:()\-–—]*$")
_THOUSAND_MARKER = re.compile(r"\d\s*[kк]\b", re.IGNORECASE)
_THOUSAND_CURRENCY = (
    r"(?:\$|₽|usdt\b|usd\b|rub\b|kgs\b|kgz\b|"
    r"доллар(?:а|ов)?\b|руб(?:ля|лей)?\b\.?|сом(?:а|ов)?\b)"
)
_THOUSAND_AMOUNT = re.compile(
    rf"(?<![\w.,])(?P<sign>[+\-−–—]?)\s*"
    rf"(?:(?P<before>{_THOUSAND_CURRENCY})\s*)?"
    rf"(?P<number>{_NUMBER})\s*[kк]\b"
    rf"(?:\s*(?P<after>{_THOUSAND_CURRENCY}))?",
    re.IGNORECASE,
)
_NON_PAYMENT_TEXT = re.compile(
    r"\?|\b(?:нужно|надо|давайте|завтра|оплатим|оплатить|сколько|когда|"
    r"будем|предстоит|планируем|планируется|обсудим|итого|всего|"
    r"просмотров|подписчиков|лайков)\b|общая\s+сумма|[=]",
    re.IGNORECASE,
)
_OTHER_CURRENCY = re.compile(
    r"€|\b(?:eur|евро|kzt|тенге|uzs|сум|cny|юан[ьи]|aed|дирхам\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedAmount:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PaymentCandidate:
    amount: Optional[Decimal]
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
class PaymentBatchDecision:
    candidates: tuple
    reason: str

    @property
    def accepted(self):
        return bool(self.candidates)


@dataclass(frozen=True)
class DdsRow:
    payment_date: date
    amount: Optional[Decimal]
    wallet: str
    purpose: str

    def updates_for_row(self, row_number):
        if row_number < 4:
            raise ValueError("DDS data rows start at row 4")
        return {
            f"D{row_number}:F{row_number}": [[
                self.payment_date.strftime("%d.%m.%Y"),
                float(self.amount) if self.amount is not None else "",
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


def _parse_bare_amount(match):
    try:
        amount = Decimal(_normalize_number(match.group("number")))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {match.group('number')}") from exc

    if match.group("sign") == "+":
        return amount
    return -amount


def parse_standalone_payment(text, has_media=False, default_currency=None):
    original = str(text or "").strip()
    if not original:
        return ParseDecision(None, "empty_message")

    if _BALANCE_MARKER.match(original):
        return ParseDecision(None, "balance_only")

    payment_text = original
    dated_prefix = re.match(r"^(\d{1,2}\.\d{1,2}\.\d{4})\s+[-–—:]\s*(.+)$", original, re.DOTALL)
    if dated_prefix:
        try:
            datetime.strptime(dated_prefix.group(1), "%d.%m.%Y")
        except ValueError:
            return ParseDecision(None, "invalid_payment_date_prefix")
        payment_text = dated_prefix.group(2)
        if _NON_PAYMENT_TEXT.search(payment_text):
            return ParseDecision(None, "dated_message_not_payment")

    match = _AMOUNT_AT_START.match(payment_text)
    if match:
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

    if has_media:
        # A receipt caption commonly puts the amount after the description,
        # for example: "Отель Ташкент (17 281,2 сом)". Restrict this relaxed
        # rule to media and to one explicit amount before any balance text.
        before_balance = _BALANCE_MARKER.split(original, maxsplit=1)[0]
        explicit_matches = list(_AMOUNT_ANYWHERE.finditer(before_balance))
        if len(explicit_matches) == 1:
            parsed = _parsed_amount_from_match(
                explicit_matches[0],
                default_negative=True,
            )
            return ParseDecision(
                PaymentCandidate(
                    amount=parsed.amount,
                    currency=parsed.currency,
                    description=original,
                    source_kind="standalone_chat_payment_from_media_caption",
                ),
                "accepted_from_media_caption",
            )

    # A text payment may state the charged amount and its accounting
    # equivalent on the final line: "81,54 $ = 7 134,75 сом". Use the charged
    # amount on the left, while keeping both values in the purpose text.
    explicit_matches = list(_AMOUNT_ANYWHERE.finditer(original))
    if len(explicit_matches) == 2:
        first_match, second_match = explicit_matches
        before_first = original[:first_match.start()]
        between = original[first_match.end():second_match.start()]
        after_second = original[second_match.end():]
        if (
            "\n" in original[:first_match.end()]
            and re.search(r"[^\W\d_]", before_first, re.UNICODE)
            and re.fullmatch(r"\s*=\s*", between)
            and not after_second.strip()
        ):
            parsed = _parsed_amount_from_match(
                first_match,
                default_negative=True,
            )
            return ParseDecision(
                PaymentCandidate(
                    amount=parsed.amount,
                    currency=parsed.currency,
                    description=original,
                    source_kind="standalone_chat_payment_with_conversion",
                ),
                "accepted_with_conversion",
            )

    if default_currency not in {CURRENCY_KGS, CURRENCY_RUB, CURRENCY_USD}:
        if _AMOUNT_ANYWHERE.search(original):
            return ParseDecision(None, "amount_not_at_start")
        return ParseDecision(None, "amount_without_currency")

    # Currency-free payments are accepted only when there is one unambiguous
    # monetary number at the start, or after a separator at the end.
    if len(re.findall(_NUMBER, original)) != 1:
        return ParseDecision(None, "ambiguous_currency_free_amount")

    bare_match = _BARE_AMOUNT_AT_START.match(original)
    if bare_match:
        tail = bare_match.group("tail")
        has_description = not _ONLY_SEPARATORS.fullmatch(tail or "")
        if not has_description and not has_media:
            return ParseDecision(None, "amount_without_description_or_media")
    else:
        bare_match = _BARE_AMOUNT_AT_END.match(original)
        if not bare_match:
            return ParseDecision(None, "amount_not_at_payment_boundary")

        head = bare_match.group("head")
        if not re.search(r"[^\W\d_]", head, re.UNICODE):
            return ParseDecision(None, "amount_without_description_or_media")

    return ParseDecision(
        PaymentCandidate(
            amount=_parse_bare_amount(bare_match),
            currency=default_currency,
            description=original,
            source_kind=f"standalone_chat_payment_inferred_{default_currency.lower()}",
        ),
        "accepted_with_inferred_currency",
    )


def parse_standalone_payments(text, has_media=False, default_currency=None):
    """Keep the existing single-payment rules; split only explicit k/к amounts."""
    original = str(text or "").strip()
    body = _BALANCE_MARKER.split(original, maxsplit=1)[0].strip()
    if not _THOUSAND_MARKER.search(body):
        decision = parse_standalone_payment(original, has_media, default_currency)
        return PaymentBatchDecision(
            (decision.candidate,) if decision.accepted else (), decision.reason,
        )

    if not body or _NON_PAYMENT_TEXT.search(body) or _OTHER_CURRENCY.search(body):
        return PaymentBatchDecision((), "ambiguous_thousands_message")
    matches = list(_THOUSAND_AMOUNT.finditer(body))
    if not matches or len(matches) != len(_THOUSAND_MARKER.findall(body)):
        return PaymentBatchDecision((), "unsupported_thousands_format")
    remainder = _THOUSAND_AMOUNT.sub(" ", body)
    if (
        re.search(r"\d", remainder)
        or re.search(_THOUSAND_CURRENCY, remainder, re.IGNORECASE)
        or re.search(r"\b(?:доллар\w*|рубл\w*)\b", remainder, re.IGNORECASE)
    ):
        return PaymentBatchDecision((), "ambiguous_additional_amount_or_currency")

    currencies = []
    for match in matches:
        before, after = match.group("before"), match.group("after")
        if before and after and _currency_code(before) != _currency_code(after):
            return PaymentBatchDecision((), "conflicting_thousands_currency")
        currencies.append(_currency_code(before or after) if before or after else "")
    if "" in currencies:
        if default_currency not in {CURRENCY_KGS, CURRENCY_RUB, CURRENCY_USD}:
            return PaymentBatchDecision((), "thousands_without_currency")
        if any(code and code != default_currency for code in currencies):
            return PaymentBatchDecision((), "ambiguous_mixed_thousands_currency")

    prefix = body[:matches[0].start()].strip(" \n\t,;:-–—")
    candidates = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        detail = body[match.end():end].strip(" \n\t,;:-–—")
        # Without per-amount descriptions this may be a range or a total.
        if len(matches) > 1 and (
            not re.search(r"[^\W\d_]", detail) or (index and match.group("sign") == "+")
        ):
            return PaymentBatchDecision((), "ambiguous_thousands_breakdown")
        purpose = ": ".join(part for part in (prefix, detail) if part)
        if not re.search(r"[^\W\d_]", purpose):
            return PaymentBatchDecision((), "thousands_without_description")
        amount = Decimal(_normalize_number(match.group("number"))) * 1000
        if amount <= 0:
            return PaymentBatchDecision((), "invalid_thousands_amount")
        if match.group("sign") != "+":
            amount = -amount
        currency = currencies[index] or default_currency
        candidates.append(PaymentCandidate(
            amount=amount,
            currency=currency,
            description=f"{decimal_for_sheets(amount)} {currency} - {purpose}",
            source_kind=(
                "standalone_chat_payment_thousands"
                + (f"_inferred_{currency.lower()}" if not currencies[index] else "")
            ),
        ))
    return PaymentBatchDecision(tuple(candidates), "accepted_thousands_payments")


def standalone_payment_event_key(chat_id, message_id, part_index=0):
    if part_index < 0:
        raise ValueError("Payment part index must be nonnegative")
    base_key = f"message:{event_key(chat_id, message_id)}"
    return base_key if part_index == 0 else f"{base_key}:part:{part_index + 1}"


_TOTAL_AMOUNT_MARKER = re.compile(
    r"\b(?:итого|итоговая\s+сумма|общая\s+сумма|сумма\s+к\s+оплате)\b",
    re.IGNORECASE,
)
_TECHNICAL_PAYMENT_MARKER = re.compile(
    r"(?:\b(?:оплатить|пополнить|перевести)\b|"
    r"\bоплата\s+(?:через|переводом|в\s+trc|с\s+(?:карты|расч[её]тного\s+сч[её]та)|"
    r"на\s+(?:карту|кошел[её]к)|по\s+(?:номеру|реквизитам))|"
    r"\bперевод(?:ом)?\s+(?:на|по)\b|"
    r"\b(?:номер|адрес)\s+(?:карты|телефона|кошел[её]ка|сч[её]та)|"
    r"\b(?:trc[- ]?20|erc[- ]?20|visa|mastercard|мбанк|mbank|банк)\b|"
    r"\b(?:реквизиты?|перед\s+(?:отправкой|оплатой))\b)",
    re.IGNORECASE,
)
_CREDENTIAL_ONLY_LINE = re.compile(
    r"^(?:https?://\S+|[A-Za-z0-9_-]{24,}|(?:\D*\d){10,}\D*)$",
    re.IGNORECASE,
)


def compact_invoice_description(request_id, target, comment):
    title = str(target or "").strip()
    header = f"Счет #{request_id}"
    if title:
        header += f" — {title}"

    detail_lines = []
    for raw_line in str(comment or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" -–—\t")
        if not line or _TOTAL_AMOUNT_MARKER.search(line):
            continue
        if _CREDENTIAL_ONLY_LINE.fullmatch(line):
            continue

        technical_match = _TECHNICAL_PAYMENT_MARKER.search(line)
        if technical_match:
            line = line[:technical_match.start()].rstrip(" .,:;(-–—")
        if (
            not line
            or not re.search(r"[^\W_]", line, re.UNICODE)
            or _CREDENTIAL_ONLY_LINE.fullmatch(line)
        ):
            continue
        if line not in detail_lines:
            detail_lines.append(line)

    return "\n".join([header, *detail_lines])


def build_bot_invoice_candidate(
    request_id,
    amount_text,
    target,
    comment,
    default_currency=None,
):
    amount = parse_number(amount_text, default_negative=True)
    try:
        currency = detect_currency(amount_text, comment, target)
    except ValueError:
        if default_currency not in {CURRENCY_KGS, CURRENCY_RUB, CURRENCY_USD}:
            raise
        currency = default_currency

    return PaymentCandidate(
        amount=amount,
        currency=currency,
        description=compact_invoice_description(request_id, target, comment),
        source_kind="bot_invoice",
    )


def telegram_message_link(chat_id, message_id, chat_username=""):
    username = str(chat_username or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{int(message_id)}"

    chat_id_text = str(int(chat_id))
    if chat_id_text.startswith("-100"):
        return f"https://t.me/c/{chat_id_text[4:]}/{int(message_id)}"
    return ""


def add_message_link(candidate, message_link):
    link = str(message_link or "").strip()
    if not link or link in candidate.description:
        return candidate
    return PaymentCandidate(
        amount=candidate.amount,
        currency=candidate.currency,
        description=f"{candidate.description.rstrip()}\n{link}",
        source_kind=candidate.source_kind,
    )


def build_media_reference_candidate(text, message_link, default_currency=None):
    description = str(text or "").strip()
    if not description:
        raise ValueError("Media reference requires a caption")
    currency = (
        default_currency
        if default_currency in {CURRENCY_KGS, CURRENCY_RUB, CURRENCY_USD}
        else ""
    )
    source_kind = "standalone_chat_media_reference"
    if currency:
        source_kind += f"_inferred_{currency.lower()}"
    return add_message_link(
        PaymentCandidate(
            amount=None,
            currency=currency,
            description=description,
            source_kind=source_kind,
        ),
        message_link,
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
    if not currency:
        unique_wallets = {
            str(value).strip()
            for value in user_wallets.values()
            if str(value).strip()
        }
        normalized_username = str(username or "").strip().lstrip("@").lower()
        username_wallets = wallets_by_username.get(normalized_username, {})
        unique_wallets.update(
            str(value).strip()
            for value in username_wallets.values()
            if str(value).strip()
        )
        if len(unique_wallets) == 1:
            return unique_wallets.pop()

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
    if value is None:
        return ""
    normalized = Decimal(value)
    return format(normalized, "f")
