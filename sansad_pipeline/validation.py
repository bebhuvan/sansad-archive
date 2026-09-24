from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .config import ValidationConfig
from .liteparse_engine import ExtractedPage


NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class ValidationResult:
    status: str
    flags: tuple[str, ...]


def numbers(text: str) -> Counter[str]:
    return Counter(token.replace(",", "") for token in NUMBER.findall(text))


def table_widths(markdown: str) -> list[int]:
    widths = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell for cell in stripped[1:-1].split("|")]
            if not all(set(cell.strip()) <= {"-", ":"} for cell in cells):
                widths.append(len(cells))
    return widths


def text_flags(
    text: str,
    *,
    reference: str | None,
    config: ValidationConfig,
) -> tuple[str, ...]:
    """Flags for candidate text without parser geometry, such as model output."""
    flags: list[str] = []
    if not text.strip():
        flags.append("empty-text")
    if text.count("�") > config.maximum_replacement_characters:
        flags.append("replacement-characters")
    widths = table_widths(text)
    if config.flag_inconsistent_table_width and widths and len(set(widths)) > 1:
        flags.append("inconsistent-table-width")
    if (
        config.flag_numeric_disagreement
        and reference is not None
        and reference.strip()
        and numbers(text) != numbers(reference)
    ):
        flags.append("candidate-numeric-disagreement")
    return tuple(flags)


def validate(
    selected: ExtractedPage,
    *,
    route: str,
    native: ExtractedPage | None,
    config: ValidationConfig,
) -> ValidationResult:
    flags: list[str] = []
    if not selected.text.strip():
        flags.append("empty-text")
    if selected.text.count("�") > config.maximum_replacement_characters:
        flags.append("replacement-characters")
    if route == "ocr" and selected.mean_confidence is not None:
        if selected.mean_confidence < config.minimum_ocr_confidence:
            flags.append("low-ocr-confidence")
    widths = table_widths(selected.markdown)
    if config.flag_inconsistent_table_width and widths and len(set(widths)) > 1:
        flags.append("inconsistent-table-width")
    if (
        config.flag_numeric_disagreement
        and route == "ocr"
        and native is not None
        and native.text.strip()
        and numbers(selected.text) != numbers(native.text)
    ):
        flags.append("native-ocr-numeric-disagreement")
    return ValidationResult("review" if flags else "accepted", tuple(flags))

