"""Extract the stated office balance and recognize amount-only edits."""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256


OFFICE_REPORTER_ID = 1525565778
CENT = Decimal("0.01")

_NUMBER = r"(?:[0-9]{1,3}(?:[ .,\u00a0\u202f][0-9]{3})+|[0-9]+)(?:[.,][0-9]{1,2})?"
_KGS = r"(?:сом(?:а|ов)?|kgs|kgz)"
_SIGN = r"[+\-−–—]"
_BALANCE_MARKER = re.compile(r"\bостаток\b", re.IGNORECASE)
_BALANCE_TAIL = re.compile(
    rf"(?:на\s+(?:карте|кошельке)\s*)?(?:[:=]\s*|[-–—]\s+)?(?P<sign>{_SIGN})?\s*"
    rf"(?P<number>{_NUMBER})\s*(?:{_KGS}\s*)?\.?$",
    re.IGNORECASE,
)
_DATE_PREFIX = r"[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{4})?[ \t]+[-–—:][ \t]+"
_MOVEMENT = re.compile(
    rf"^(?:{_DATE_PREFIX})?(?P<sign>{_SIGN})[ \t]*(?P<number>{_NUMBER})"
    rf"(?![\d.,])(?=[\s\-–—]|{_KGS}\b|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OfficeReport:
    balance: Decimal | None
    amount: Decimal | None
    shape: str


@dataclass(frozen=True)
class OfficeAmountEdit:
    day: date
    previous: Decimal
    revised: Decimal


def _read_number(match):
    number = "".join(match["number"].split())
    split_at = max(number.rfind("."), number.rfind(","))
    if 0 <= split_at and len(number) - split_at - 1 <= 2:
        whole = re.sub(r"[.,]", "", number[:split_at])
        amount = Decimal(f"{whole}.{number[split_at + 1:]}")
    else:
        amount = Decimal(re.sub(r"[.,]", "", number))
    if amount >= Decimal("1000000000000"):
        return None
    return -amount if match["sign"] in {"-", "−", "–", "—"} else amount


def report_fingerprint(text):
    return sha256(text.strip().encode("utf-8")).hexdigest()


def read_office_report(text):
    text = text.strip()
    markers = list(_BALANCE_MARKER.finditer(text))
    balance = None
    operation = text
    if len(markers) == 1:
        marker = markers[0]
        operation = text[:marker.start()].rstrip(" \t\r\n,;")
        if trailing := _BALANCE_TAIL.fullmatch(text[marker.end():].strip()):
            balance = _read_number(trailing)

    amount = None
    if movement := _MOVEMENT.match(operation):
        amount = _read_number(movement)
        if amount:
            start, end = movement.span("number")
            operation = operation[:start] + "{amount}" + operation[end:]
    shape = " ".join(operation.split()).casefold()
    return OfficeReport(balance, amount, report_fingerprint(shape))


def sheet_money(value):
    if type(value) not in (int, float):
        return None
    amount = Decimal(str(value))
    return amount if amount.is_finite() else None


def format_som(value):
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " сом"
