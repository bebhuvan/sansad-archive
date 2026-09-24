from __future__ import annotations

from dataclasses import dataclass

from .config import RoutingConfig
from .liteparse_engine import ExtractedPage


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reasons: tuple[str, ...]


def alphanumeric_ratio(text: str) -> float:
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return 0.0
    return sum(character.isalnum() for character in visible) / len(visible)


def decide(page: ExtractedPage, config: RoutingConfig) -> RouteDecision:
    text = page.text.strip()
    complexity_reasons = set(page.complexity.get("reasons") or [])
    reasons: list[str] = []
    if config.force_ocr_full_page_images and page.complexity.get("full_page_image"):
        reasons.append("full-page-image")
    if not text:
        reasons.append("empty-native-text")
    elif len(text) < config.minimum_native_characters:
        reasons.append("too-little-native-text")
    if text and alphanumeric_ratio(text) < config.minimum_alphanumeric_ratio:
        reasons.append("low-alphanumeric-ratio")
    reasons.extend(sorted(complexity_reasons.intersection(config.ocr_reasons)))

    # `sparse-text` is deliberately insufficient: ruled Sansad tables often
    # trigger it even when their native text layer and vector geometry are good.
    return RouteDecision("ocr" if reasons else "native", tuple(dict.fromkeys(reasons)))
