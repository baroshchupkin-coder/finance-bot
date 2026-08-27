import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher


_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_INVOICE_NUMBER = re.compile(r"\b(?:сч[её]т|invoice)\s*#?\s*\d+\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<!\w)[+\-−–—]?\s*\d[\d\s.,'’]*", re.UNICODE)
_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")
_STOP_WORDS = frozenset({
    "в", "во", "для", "до", "за", "из", "к", "на", "от", "по", "с", "со",
    "и", "или", "а", "но", "не", "это", "оплата", "оплатить", "платеж",
    "перевод", "перевести", "пополнить", "сумма", "итого", "руб", "рублей",
    "сом", "сомов", "kgs", "kgz", "usd", "usdt", "долларов",
})


@dataclass(frozen=True)
class CategoryExample:
    description: str
    amount: object
    wallet: str
    article: str
    source: str = "history"


@dataclass(frozen=True)
class CategoryPrediction:
    article: str = ""
    confidence: float = 0.0
    status: str = "review"
    reason: str = "no_match"
    match: str = ""


def normalize_description(value):
    text = _URL.sub(" ", str(value or "").casefold().replace("ё", "е"))
    text = _INVOICE_NUMBER.sub(" ", text)
    text = _NUMBER.sub(" ", text)
    text = _PUNCTUATION.sub(" ", text)
    words = [word for word in _SPACE.split(text.strip()) if word and word not in _STOP_WORDS]
    return " ".join(words)


def amount_sign(value):
    if value in (None, ""):
        return "unknown"
    try:
        normalized = str(value).strip().replace("\u00a0", "").replace(" ", "")
        normalized = normalized.replace("−", "-").replace("–", "-").replace("—", "-")
        normalized = normalized.replace(",", ".")
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return "unknown"
    if amount < 0:
        return "expense"
    if amount > 0:
        return "income"
    return "zero"


def _article_choice(examples):
    normalized = Counter(str(item.article).strip().casefold() for item in examples)
    if not normalized:
        return "", 0
    winner, count = normalized.most_common(1)[0]
    if len(normalized) > 1:
        return "", count
    for item in examples:
        if str(item.article).strip().casefold() == winner:
            return item.article, count
    return "", count


def _token_similarity(left, right):
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left, right).ratio()
    return (0.55 * sequence) + (0.45 * jaccard)


class DdsCategoryClassifier:
    def __init__(self, examples=None):
        self.examples = []
        for example in examples or []:
            self.add_verified(example)

    def add_verified(self, example=None, *, description="", amount=None, wallet="", article="", source="learned"):
        if example is None:
            example = CategoryExample(
                description=description,
                amount=amount,
                wallet=wallet,
                article=article,
                source=source,
            )
        if not normalize_description(example.description) or not str(example.article).strip():
            return False
        self.examples.append(example)
        return True

    def predict(self, description, amount, wallet=""):
        normalized = normalize_description(description)
        if not normalized:
            return CategoryPrediction(reason="empty_description")

        sign = amount_sign(amount)
        wallet_key = str(wallet or "").strip().casefold()
        prepared = [
            (
                item,
                normalize_description(item.description),
                amount_sign(item.amount),
                str(item.wallet or "").strip().casefold(),
            )
            for item in self.examples
        ]

        exact_wallet = [
            item
            for item, text, item_sign, item_wallet in prepared
            if text == normalized and item_sign == sign and item_wallet == wallet_key
        ]
        article, count = _article_choice(exact_wallet)
        if article:
            return CategoryPrediction(
                article=article,
                confidence=0.99,
                status="auto",
                reason="exact_description_sign_wallet",
                match=normalize_description(exact_wallet[0].description),
            )
        if exact_wallet:
            return CategoryPrediction(
                confidence=0.0,
                reason="conflicting_exact_description_sign_wallet",
                match=normalized,
            )

        exact_any_wallet = [
            item
            for item, text, item_sign, _ in prepared
            if text == normalized and item_sign == sign
        ]
        article, count = _article_choice(exact_any_wallet)
        has_learned = any(item.source == "learned" for item in exact_any_wallet)
        if article and (count >= 2 or has_learned):
            return CategoryPrediction(
                article=article,
                confidence=0.96 if count >= 2 else 0.94,
                status="auto",
                reason="exact_description_sign",
                match=normalize_description(exact_any_wallet[0].description),
            )
        if exact_any_wallet and not article:
            return CategoryPrediction(
                confidence=0.0,
                reason="conflicting_exact_description_sign",
                match=normalized,
            )

        scored = []
        for item, text, item_sign, item_wallet in prepared:
            if not text:
                continue
            score = _token_similarity(normalized, text)
            if item_sign == sign:
                score += 0.05
            elif item_sign != "unknown" and sign != "unknown":
                score -= 0.12
            if wallet_key and item_wallet == wallet_key:
                score += 0.04
            scored.append((max(0.0, min(score, 1.0)), item, text))

        if not scored:
            return CategoryPrediction(reason="no_history")

        scored.sort(key=lambda value: value[0], reverse=True)
        best_score, best_item, best_text = scored[0]
        if best_score < 0.48:
            return CategoryPrediction(
                confidence=round(best_score, 3),
                reason="weak_similarity",
                match=best_text,
            )

        learned_support = [
            (score, item)
            for score, item, _ in scored[:5]
            if score >= 0.82
            and item.source == "learned"
            and str(item.article).strip().casefold()
            == str(best_item.article).strip().casefold()
        ]
        competing_score = max(
            (
                score
                for score, item, _ in scored
                if str(item.article).strip().casefold()
                != str(best_item.article).strip().casefold()
            ),
            default=0.0,
        )
        if (
            best_score >= 0.88
            and len(learned_support) >= 2
            and best_score - competing_score >= 0.1
        ):
            return CategoryPrediction(
                article=best_item.article,
                confidence=round(best_score, 3),
                status="auto",
                reason="repeated_learned_similarity",
                match=best_text,
            )

        return CategoryPrediction(
            article=best_item.article,
            confidence=round(best_score, 3),
            status="review",
            reason="similar_description",
            match=best_text,
        )
