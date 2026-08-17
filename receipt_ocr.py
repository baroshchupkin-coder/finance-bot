import io
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

from dds_integration import PaymentCandidate, parse_amount_with_currency, parse_number


TOTAL_MARKERS = (
    "итого",
    "итоговая сумма",
    "сумма платежа",
    "сумма операции",
    "amount",
    "total",
)
SUCCESS_MARKERS = (
    "платеж выполнен",
    "платеж успешно",
    "транзакция успешно",
    "оплачено",
    "перевод выполнен",
)
IGNORE_MARKERS = (
    "комиссия",
    "остаток",
    "баланс",
    "кэшбэк",
    "cashback",
)


class OcrUnavailable(RuntimeError):
    pass


class OcrFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrTextResult:
    text: str
    duration_seconds: float
    width: int
    height: int


@dataclass(frozen=True)
class OcrDecision:
    candidate: PaymentCandidate | None
    reason: str
    confidence: str


def tesseract_available(command="tesseract"):
    return bool(shutil.which(command))


def _prepare_image(image_bytes, max_pixels):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = max(max_pixels * 4, 20_000_000)
    with Image.open(io.BytesIO(image_bytes)) as source:
        if source.width * source.height > max_pixels * 4:
            raise OcrFailed("source image is too large")
        source.load()
        image = ImageOps.exif_transpose(source).convert("L")
        width, height = image.size
        pixel_count = width * height
        if pixel_count > max_pixels:
            scale = math.sqrt(max_pixels / pixel_count)
            width = max(1, int(width * scale))
            height = max(1, int(height * scale))
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        image = ImageOps.autocontrast(image)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue(), width, height


def extract_text(
    image_bytes,
    timeout_seconds=8,
    max_pixels=4_000_000,
    languages="rus+eng",
    command="tesseract",
):
    executable = shutil.which(command)
    if not executable:
        raise OcrUnavailable("tesseract executable was not found")
    if not image_bytes:
        raise OcrFailed("empty image")

    try:
        prepared, width, height = _prepare_image(image_bytes, max_pixels)
    except OcrFailed:
        raise
    except Exception as exc:
        raise OcrFailed("image could not be decoded") from exc
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="finance-bot-ocr-") as temp_dir:
        image_path = os.path.join(temp_dir, "receipt.png")
        with open(image_path, "wb") as image_file:
            image_file.write(prepared)

        command_parts = [
            executable,
            image_path,
            "stdout",
            "-l",
            languages,
            "--oem",
            "1",
            "--psm",
            "6",
        ]
        nice = shutil.which("nice")
        if nice:
            command_parts = [nice, "-n", "10", *command_parts]

        try:
            result = subprocess.run(
                command_parts,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrFailed(f"tesseract timed out after {timeout_seconds}s") from exc

    if result.returncode != 0:
        raise OcrFailed(result.stderr.strip() or f"tesseract exited with {result.returncode}")
    return OcrTextResult(
        text=result.stdout.strip(),
        duration_seconds=time.monotonic() - started,
        width=width,
        height=height,
    )


def _score_line(line, line_index):
    normalized = line.casefold()
    if any(marker in normalized for marker in IGNORE_MARKERS):
        return -20
    score = 1 if line_index < 6 else 0
    if any(marker in normalized for marker in TOTAL_MARKERS):
        score += 8
    if any(marker in normalized for marker in SUCCESS_MARKERS):
        score += 5
    return score


def choose_payment_candidate(caption, ocr_text, default_currency=None):
    lines = [line.strip() for line in str(ocr_text or "").splitlines() if line.strip()]
    candidates = []
    for index, line in enumerate(lines):
        try:
            parsed = parse_amount_with_currency(line, default_negative=True)
        except ValueError:
            if not default_currency:
                continue
            normalized = line.casefold()
            if not any(marker in normalized for marker in TOTAL_MARKERS + SUCCESS_MARKERS):
                continue
            try:
                amount = parse_number(line, default_negative=True)
            except ValueError:
                continue
            parsed_amount = amount
            currency = default_currency
        else:
            parsed_amount = parsed.amount
            currency = parsed.currency
        candidates.append((_score_line(line, index), parsed_amount, currency, line))

    if not candidates:
        return OcrDecision(None, "amount_not_found", "none")

    unique = {}
    for score, amount, currency, line in candidates:
        key = (amount, currency)
        current = unique.get(key)
        if current is None or score > current[0]:
            unique[key] = (score, amount, currency, line)
    ranked = sorted(unique.values(), key=lambda value: value[0], reverse=True)

    if len(ranked) == 1:
        selected = ranked[0]
        confidence = "high"
        reason = "single_amount"
    else:
        selected = ranked[0]
        runner_up = ranked[1]
        if selected[0] < 5 or selected[0] - runner_up[0] < 3:
            return OcrDecision(None, "ambiguous_amounts", "low")
        confidence = "high"
        reason = "keyword_ranked_amount"

    _, amount, currency, _ = selected
    description = str(caption or "").strip() or "Чек без подписи"
    return OcrDecision(
        PaymentCandidate(
            amount=amount,
            currency=currency,
            description=description,
            source_kind="standalone_chat_payment_from_ocr",
        ),
        reason,
        confidence,
    )
