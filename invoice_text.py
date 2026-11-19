"""Read a single-message invoice without guessing its approval route."""

import re
from dataclasses import dataclass


TELEGRAM_INVOICE_KEY_PREFIX = "telegram-invoice|"

_LABELS = {
    "проект": "project",
    "статья": "expense_category",
    "статья расхода": "expense_category",
    "кому платим": "target",
    "получатель": "target",
    "фио": "target",
    "fio": "target",
    "сумма": "amount",
    "дата оплаты": "payment_due_date",
    "дата": "payment_due_date",
    "комментарий": "comment",
    "назначение платежа": "comment",
}
_FIELD = re.compile(
    r"^(" + "|".join(_LABELS) + r")(?:\s*:\s*|\s+[-–—]\s+)(.*)$",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"(?:новый\s+)?сч[её]т(?:\s*[#№]\s*(\d+)(?:\s*[-–—]\s*.*)?)?", re.IGNORECASE,
)
_FOOTER = re.compile(r"(?:отправлено|согласовано|оплачено)\s*:.*", re.IGNORECASE)
_PARAGRAPHS = re.compile(r"\n[ \t]*\n\s*")

_NUMBER = r"(?:[0-9]{1,3}(?:[ .,\u00a0\u202f'’][0-9]{3})+|[0-9]+)(?:[,.][0-9]+)?"
_CURRENCY = r"(?:\$|usd[t]?|kgs|kgz|som[s]?|сом(?:ов|а)?|руб(?:лей|ля|\.)?|rub|₽|доллар(?:ов|а)?|eur|евро|€)"
_WITH_CURRENCY = rf"(?:{_NUMBER}\s*{_CURRENCY}|{_CURRENCY}\s*{_NUMBER})"
_AMOUNT = re.compile(rf"\+?\s*(?:{_WITH_CURRENCY}|{_NUMBER}(?:\s*[sс])?)", re.IGNORECASE)
_MONEY_MENTION = re.compile(rf"(?<![\w.,]){_WITH_CURRENCY}(?!\w|[.,][0-9])", re.IGNORECASE)
_TOTAL_LABEL = r"(?:итого|всего|итоговая сумма(?: к оплате)?|сумма к оплате)"
_TOTAL_BEFORE = re.compile(rf"^{_TOTAL_LABEL}(?:\s*:\s*|\s+(?:[-–—]\s+)?)(.+)$", re.IGNORECASE)
_TOTAL_AFTER = re.compile(rf"^(.+?)\s+[-–—]\s+{_TOTAL_LABEL}$", re.IGNORECASE)


@dataclass(frozen=True)
class InvoiceText:
    fields: dict
    copied_request_id: str = ""


def parse_invoice_text(text, *, partial=False):
    """Return recognized fields; use an empty value to flag conflicting labels."""
    lines = text.strip().splitlines()
    while lines and (not lines[-1].strip() or _FOOTER.fullmatch(lines[-1].strip())):
        lines.pop()
    if not lines:
        return None
    if re.fullmatch(r"@[a-zA-Z0-9_]+", lines[0].strip()) and any(
        _HEADER.fullmatch(line.strip()) or _FIELD.fullmatch(line.strip())
        for line in lines[1:4]
    ):
        lines.pop(0)

    fields = {}
    copied = False
    copied_request_id = ""
    labels = set()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if header := _HEADER.fullmatch(line):
            copied = True
            copied_request_id = header[1] or copied_request_id
            continue
        match = _FIELD.fullmatch(line)
        if match:
            field = _LABELS[match[1].casefold()]
            value = match[2].strip()
            if field == "comment":
                fields[field] = "\n".join([value, *lines[index + 1:]]).strip()
                labels.add(field)
                break
            if field in labels and fields[field].casefold() != value.casefold():
                value = ""
            fields[field] = value
            labels.add(field)
            continue

        tail = "\n".join(lines[index:]).strip()
        if "target" in fields:
            fields["comment"] = tail
        elif copied or "payment_due_date" in labels:
            parts = _PARAGRAPHS.split(tail, maxsplit=1)
            if copied and len(parts) == 1:
                parts = tail.split("\n", 1)
            if len(parts) == 2:
                fields["target"], fields["comment"] = (part.strip() for part in parts)
        break

    if len(labels) >= (1 if partial else 2) or (
        fields.get("target") and fields.get("comment") and (copied or "payment_due_date" in labels)
    ):
        return InvoiceText(fields, copied_request_id)
    return None


def is_invoice_amount(value):
    return bool(_AMOUNT.fullmatch(value.strip()) and re.search(r"[1-9]", value))


def amount_from_invoice_comment(comment):
    """Recover an explicit total or a single amount with currency on its own line."""
    totals = []
    mentions = []
    for raw in comment.splitlines():
        line = raw.strip()
        total = _TOTAL_BEFORE.fullmatch(line) or _TOTAL_AFTER.fullmatch(line)
        if total:
            totals.append(total[1].strip())
        for match in _MONEY_MENTION.finditer(line):
            remainder = line[match.end():].lstrip()
            standalone = match.start() == 0 and (not remainder or remainder.startswith(("-", "–", "—")))
            mentions.append((match[0], standalone))

    if totals:
        unique = set(totals)
        return totals[0] if len(unique) == 1 and is_invoice_amount(totals[0]) else ""
    if len(mentions) == 1 and mentions[0][1] and is_invoice_amount(mentions[0][0]):
        return mentions[0][0]
    return ""
