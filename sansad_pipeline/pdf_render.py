"""Bounded PDF page rendering shared by OCR and model transcription."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


RENDER_TIMEOUT_SECONDS = 180


def render_page(pdf: Path, page_number: int, output_dir: Path, dpi: int) -> Path:
    lit = Path(sys.executable).with_name("lit")
    if not lit.exists():
        found = shutil.which("lit")
        if not found:
            raise RuntimeError("LiteParse `lit` executable was not found")
        lit = Path(found)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                str(lit), "screenshot", str(pdf), "--output-dir", str(output_dir),
                "--target-pages", str(page_number), "--dpi", str(dpi), "--quiet",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=RENDER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"LiteParse screenshot timed out after {RENDER_TIMEOUT_SECONDS}s "
            f"for page {page_number} of {pdf}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = str(exc.stderr or "").strip()[-500:]
        raise RuntimeError(
            f"LiteParse screenshot failed for page {page_number} of {pdf}: {detail}"
        ) from exc
    images = sorted(output_dir.glob("*.png")) + sorted(output_dir.glob("*.jpg"))
    if len(images) != 1:
        raise RuntimeError(f"expected one rendered page, found {len(images)} in {output_dir}")
    return images[0]
