from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from liteparse import LiteParse
from PIL import Image

from .config import Config


PARSE_TIMEOUT_SECONDS = 300


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    markdown: str
    width: float
    height: float
    complexity: dict[str, Any]
    mean_confidence: float | None
    text_items: int
    vector_lines: int
    vector_shapes: int


def serializable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serializable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: serializable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


class LiteParseEngine:
    name = "liteparse"

    def __init__(self, config: Config):
        self.config = config
        installed = importlib.metadata.version("liteparse")
        if installed != config.liteparse.version:
            raise RuntimeError(
                f"LiteParse version mismatch: configured {config.liteparse.version}, installed {installed}"
            )
        self.version = installed

    def extract(
        self,
        pdf: Path,
        *,
        ocr: bool,
        target_pages: list[int] | None = None,
        rasterize: bool = False,
    ) -> list[ExtractedPage]:
        cfg = self.config.liteparse
        parse_input: Path | bytes = pdf
        page_number_map: dict[int, int] = {}
        parser_target_pages = target_pages
        effective_dpi = cfg.full_page_image_dpi if rasterize else cfg.dpi
        if ocr and target_pages and rasterize:
            # LiteParse intentionally keeps a sufficiently dense native text layer,
            # even when that layer is old OCR over a full-page scan. For archival
            # re-OCR, render only the routed pages and parse the image-only PDF.
            renderer = LiteParse(dpi=effective_dpi, quiet=True)
            screenshots = renderer.screenshot(pdf, page_numbers=target_pages)
            if not screenshots:
                raise RuntimeError("LiteParse returned no screenshots for routed OCR pages")
            images = [Image.open(BytesIO(item.image_bytes)).convert("RGB") for item in screenshots]
            raster_pdf = BytesIO()
            images[0].save(
                raster_pdf,
                format="PDF",
                save_all=True,
                append_images=images[1:],
                resolution=effective_dpi,
                quality=95,
                subsampling=0,
            )
            parse_input = raster_pdf.getvalue()
            page_number_map = {
                raster_page: screenshot.page_num
                for raster_page, screenshot in enumerate(screenshots, 1)
            }
            parser_target_pages = None
        parser = LiteParse(
            ocr_enabled=ocr,
            ocr_server_url=cfg.ocr_server_url or None,
            ocr_language=cfg.language,
            dpi=effective_dpi,
            target_pages=(
                ",".join(str(page) for page in parser_target_pages)
                if parser_target_pages
                else None
            ),
            output_format="markdown",
            preserve_very_small_text=cfg.preserve_small_text,
            num_workers=cfg.workers,
            max_pages=cfg.max_pages,
            image_mode="off",
            extract_links=False,
            keep_headers_footers=cfg.keep_headers_footers,
            include_complexity=True,
            extract_content_bounds=True,
            extract_text_metadata=True,
            extract_vector_graphics=True,
            pool_size=1,
            parse_timeout=PARSE_TIMEOUT_SECONDS,
            quiet=True,
        )
        try:
            result = parser.parse(parse_input)
        finally:
            parser.close()
        pages: list[ExtractedPage] = []
        for page in result.pages:
            confidences = [
                item.confidence
                for item in (page.text_items or [])
                if item.confidence is not None
            ]
            vectors = page.vector_graphics
            pages.append(
                ExtractedPage(
                    page_number=page_number_map.get(int(page.page_num), int(page.page_num)),
                    text=page.text or "",
                    markdown=page.markdown or page.text or "",
                    width=float(page.width),
                    height=float(page.height),
                    complexity=serializable(page.complexity) or {},
                    mean_confidence=sum(confidences) / len(confidences) if confidences else None,
                    text_items=len(page.text_items or []),
                    vector_lines=len(vectors.lines) if vectors else 0,
                    vector_shapes=len(vectors.shapes) if vectors else 0,
                )
            )
        expected = (
            sorted(set(target_pages)) if target_pages is not None
            else list(range(1, int(result.total_pages) + 1))
        )
        actual = sorted(page.page_number for page in pages)
        if actual != expected:
            raise RuntimeError(
                f"LiteParse returned pages {actual[:12]} ({len(actual)} total), "
                f"expected {expected[:12]} ({len(expected)} total)"
            )
        return pages
