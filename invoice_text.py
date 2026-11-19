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
    r"^(?P<label>" + "|".join(_LABELS) + r")(?P<separator>\s*:\s*|\s+[-–—]\s+)(?P<value>.*)$",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"(?:новый\s+)?сч[её]т(?:\s*[#№]\s*(\d+)(?:\s*[-–—]\s*.*)?)?", re.IGNORECASE,
)
_FOOTER = re.compile(r"(?:отправлено|согласовано|оплачено)\s*:.*", re.IGNORECASE)
_PARAGRAPHS = re.compile(r"\n[ \t]*\n\s*")
_COPY_OVERRIDE_FIELDS = {"project", "expense_category", "payment_due_date"}

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


def _trim_footer(lines):
    while lines and (not lines[-1].strip() or _FOOTER.fullmatch(lines[-1].strip())):
        lines.pop()
    return lines


def _record_field(fields, field, value):
    fields[field] = "" if field in fields and fields[field].casefold() != value.casefold() else value


def _copy_comment_and_overrides(comment):
    lines = _trim_footer(comment.splitlines())
    overrides = {}
    start = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if not line:
            continue
        match = _FIELD.fullmatch(line)
        if not match or ":" not in match["separator"]:
            break
        field = _LABELS.get(match["label"].casefold())
        if field not in _COPY_OVERRIDE_FIELDS:
            break
        _record_field(overrides, field, match["value"].strip())
        start = index

    before = lines[start - 1].strip() if start else ""
    if not overrides or (len(overrides) == 1 and before and not _FOOTER.fullmatch(before)):
        return "\n".join(lines), {}
    return "\n".join(_trim_footer(lines[:start])), overrides


def parse_invoice_text(text, *, partial=False):
    """Return recognized fields; use an empty value to flag conflicting labels."""
    lines = _trim_footer(text.strip().splitlines())
    if not lines:
        return None
    if re.fullmatch(r"@[a-zA-Z0-9_]+", lines[0].strip()) and any(
        _HEADER.fullmatch(line.strip()) or _FIELD.fullmatch(line.strip())
        for line in lines[1:4]
    ):
        lines.pop(0)

    fields = {}
    copied = False
    legacy_body = False
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
        field = _LABELS.get(match["label"].casefold()) if match else None
        if field:
            value = match["value"].strip()
            if field == "comment":
                fields[field] = "\n".join([value, *lines[index + 1:]]).strip()
                labels.add(field)
                break
            _record_field(fields, field, value)
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
                legacy_body = True
        break

    if "comment" in fields and (copied or legacy_body):
        fields["comment"], overrides = _copy_comment_and_overrides(fields["comment"])
        fields.update(overrides)
        labels.update(overrides)

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
