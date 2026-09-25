"""Conservative evidence that a rendered PDF page contains no visible ink."""
from __future__ import annotations

from pathlib import Path

from PIL import Image


DARK_PIXEL_CUTOFF = 250
MAX_BLANK_DARK_PIXELS = 25


def image_ink_metrics(image: Image.Image) -> dict[str, int | bool]:
    """Count dark pixels; only near-pure-white pages qualify as visually blank.

    The strict cutoff intentionally sends faint scans with uncertain content to
    review rather than treating them as empty. This is a visual heuristic, not
    evidence that a PDF contains no hidden text or annotations.
    """
    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    pixels = grayscale.width * grayscale.height
    dark_pixels = sum(histogram[:DARK_PIXEL_CUTOFF])
    return {
        "image_pixels": pixels,
        "image_dark_pixels": dark_pixels,
        "image_dark_pixel_cutoff": DARK_PIXEL_CUTOFF,
        "visually_blank": dark_pixels <= MAX_BLANK_DARK_PIXELS,
    }


def rendered_ink_metrics(path: Path) -> dict[str, int | bool]:
    with Image.open(path) as image:
        return image_ink_metrics(image)


def valid_image_metrics(metrics: object) -> bool:
    """Reject partial or internally contradictory stored image measurements."""
    return bool(
        isinstance(metrics, dict)
        and type(metrics.get("visually_blank")) is bool
        and metrics.get("image_dark_pixel_cutoff") == DARK_PIXEL_CUTOFF
        and type(metrics.get("image_dark_pixels")) is int
        and 0 <= metrics["image_dark_pixels"]
        and type(metrics.get("image_pixels")) is int
        and metrics["image_pixels"] > 0
        and metrics["image_dark_pixels"] <= metrics["image_pixels"]
        and metrics["visually_blank"] == (
            metrics["image_dark_pixels"] <= MAX_BLANK_DARK_PIXELS
        )
    )


def valid_blank_image_evidence(metrics: object) -> bool:
    """Check the stored metrics before accepting an empty OCR/model layer."""
    return valid_image_metrics(metrics) and metrics["visually_blank"] is True


def verified_blank_response(provenance: object) -> bool:
    return bool(
        isinstance(provenance, dict)
        and provenance.get("blank_response_verified") is True
        and valid_blank_image_evidence(provenance.get("visual_quality"))
    )
