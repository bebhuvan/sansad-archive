#!/usr/bin/env python3
"""Run a reproducible LiteParse configuration sweep over the Sansad samples."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from liteparse import LiteParse


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "raw"
DEFAULT_OUT = ROOT / "results" / "liteparse_2.10.1"


@dataclass(frozen=True)
class Case:
    case_id: str
    pdf: str
    ocr: bool
    dpi: int = 150
    pages: str | None = None
    preserve_small: bool = False
    workers: int = 4
    keep_headers_footers: bool = False
    reference: str | None = None
    reference_kind: str | None = None
    gold: str | None = None


CASES = [
    Case("q0560_native", "ls2026_q0560.pdf", False),
    Case("q0560_ocr150", "ls2026_q0560.pdf", True, 150, reference="q0560_native", reference_kind="native_text"),
    Case("q0560_ocr300", "ls2026_q0560.pdf", True, 300, reference="q0560_native", reference_kind="native_text"),
    Case("q0560_ocr300_small", "ls2026_q0560.pdf", True, 300, preserve_small=True, reference="q0560_native", reference_kind="native_text"),
    Case("q1948_native", "ls2026_q1948.pdf", False),
    Case("q1948_ocr150", "ls2026_q1948.pdf", True, 150, reference="q1948_native", reference_kind="native_text"),
    Case("q1948_ocr300", "ls2026_q1948.pdf", True, 300, reference="q1948_native", reference_kind="native_text"),
    Case("q1948_scan100", "ls2026_q1948_raster180_q65.pdf", True, 100, reference="q1948_native", reference_kind="same_document_native_text"),
    Case("q1948_scan150", "ls2026_q1948_raster180_q65.pdf", True, 150, reference="q1948_native", reference_kind="same_document_native_text"),
    Case("q1948_scan150_w1", "ls2026_q1948_raster180_q65.pdf", True, 150, workers=1, reference="q1948_native", reference_kind="same_document_native_text"),
    Case("q1948_scan150_w8", "ls2026_q1948_raster180_q65.pdf", True, 150, workers=8, reference="q1948_native", reference_kind="same_document_native_text"),
    Case("q1948_scan200", "ls2026_q1948_raster180_q65.pdf", True, 200, reference="q1948_native", reference_kind="same_document_native_text"),
    Case("q1948_scan300", "ls2026_q1948_raster180_q65.pdf", True, 300, reference="q1948_native", reference_kind="same_document_native_text"),
    Case("q1948_scan300_small", "ls2026_q1948_raster180_q65.pdf", True, 300, preserve_small=True, reference="q1948_native", reference_kind="same_document_native_text"),
    Case("q4519_native", "ls2026_q4519.pdf", False, pages="1,2,40,41"),
    Case("q4519_native_headers", "ls2026_q4519.pdf", False, pages="1,2,40,41", keep_headers_footers=True, reference="q4519_native", reference_kind="native_text_without_headers_footers"),
    Case("q4519_ocr150", "ls2026_q4519.pdf", True, 150, pages="1,2,40,41", reference="q4519_native", reference_kind="native_text"),
    Case("q4519_ocr300_small", "ls2026_q4519.pdf", True, 300, pages="1,2,40,41", preserve_small=True, reference="q4519_native", reference_kind="native_text"),
    Case("india1985_no_ocr", "india1985_scan_proxy.pdf", False, pages="3", gold="india1985_p3.txt", reference_kind="manual_transcription"),
    Case("india1985_ocr100", "india1985_scan_proxy.pdf", True, 100, pages="3", gold="india1985_p3.txt", reference_kind="manual_transcription"),
    Case("india1985_ocr150", "india1985_scan_proxy.pdf", True, 150, pages="3", gold="india1985_p3.txt", reference_kind="manual_transcription"),
    Case("india1985_ocr200", "india1985_scan_proxy.pdf", True, 200, pages="3", gold="india1985_p3.txt", reference_kind="manual_transcription"),
    Case("india1985_ocr300", "india1985_scan_proxy.pdf", True, 300, pages="3", gold="india1985_p3.txt", reference_kind="manual_transcription"),
    Case("india1985_ocr300_small", "india1985_scan_proxy.pdf", True, 300, pages="3", preserve_small=True, gold="india1985_p3.txt", reference_kind="manual_transcription"),
]


WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*|\d[\d,]*(?:\.\d+)?")
NUM_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def token_counter(text: str) -> Counter[str]:
    return Counter(t.casefold().replace(",", "") for t in WORD_RE.findall(text))


def number_counter(text: str) -> Counter[str]:
    return Counter(t.replace(",", "") for t in NUM_RE.findall(text))


def prf(found: Counter[str], expected: Counter[str]) -> tuple[float, float, float]:
    overlap = sum((found & expected).values())
    precision = overlap / sum(found.values()) if found else 0.0
    recall = overlap / sum(expected.values()) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def serialize_rect(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "__dict__"):
        return {k: serialize_rect(v) for k, v in value.__dict__.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_rect(v) for v in value]
    return value


def run_case(case: Case, out: Path) -> dict[str, Any]:
    source = SAMPLES / case.pdf
    if not source.exists():
        raise FileNotFoundError(source)
    parser = LiteParse(
        ocr_enabled=case.ocr,
        ocr_language="eng",
        dpi=case.dpi,
        target_pages=case.pages,
        output_format="markdown",
        preserve_very_small_text=case.preserve_small,
        num_workers=case.workers,
        keep_headers_footers=case.keep_headers_footers,
        image_mode="off",
        extract_links=False,
        include_complexity=True,
        extract_content_bounds=True,
        extract_text_metadata=True,
        extract_vector_graphics=True,
        quiet=True,
    )
    started = time.monotonic()
    result = parser.parse(source)
    elapsed = time.monotonic() - started

    text = "\n\f\n".join(page.text or "" for page in result.pages)
    markdown = "\n\n<!-- PAGE BREAK -->\n\n".join(
        page.markdown or page.text or "" for page in result.pages
    )
    (out / f"{case.case_id}.txt").write_text(text, encoding="utf-8")
    (out / f"{case.case_id}.md").write_text(markdown, encoding="utf-8")

    page_rows = []
    for page in result.pages:
        comp = serialize_rect(page.complexity)
        page_rows.append(
            {
                "page": page.page_num,
                "width": page.width,
                "height": page.height,
                "text_chars": len(page.text or ""),
                "markdown_chars": len(page.markdown or ""),
                "text_items": len(page.text_items or []),
                "mean_confidence": (
                    sum(i.confidence for i in page.text_items if i.confidence is not None)
                    / sum(1 for i in page.text_items if i.confidence is not None)
                    if any(i.confidence is not None for i in page.text_items)
                    else None
                ),
                "complexity": comp,
                "content_bounds": serialize_rect(page.content_bounds),
                "vector_line_count": len(page.vector_graphics.lines) if page.vector_graphics else 0,
                "vector_shape_count": len(page.vector_graphics.shapes) if page.vector_graphics else 0,
            }
        )

    artifact = {
        "case": asdict(case),
        "liteparse_version": "2.10.1",
        "elapsed_seconds": elapsed,
        "pages_returned": len(result.pages),
        "text_chars": len(text),
        "markdown_chars": len(markdown),
        "word_tokens": sum(token_counter(text).values()),
        "numeric_tokens": sum(number_counter(text).values()),
        "markdown_table_rows": sum(1 for line in markdown.splitlines() if line.strip().startswith("|")),
        "pages": page_rows,
    }
    (out / f"{case.case_id}.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return artifact


def add_comparisons(rows: dict[str, dict[str, Any]], out: Path) -> None:
    for case in CASES:
        if case.case_id not in rows:
            continue
        actual = (out / f"{case.case_id}.txt").read_text(encoding="utf-8")
        if case.gold:
            reference_label = f"gold/{case.gold}"
            reference = (ROOT / "samples" / "gold" / case.gold).read_text(encoding="utf-8")
        elif case.reference and case.reference in rows:
            reference_label = case.reference
            reference = (out / f"{case.reference}.txt").read_text(encoding="utf-8")
        else:
            continue
        word_p, word_r, word_f1 = prf(token_counter(actual), token_counter(reference))
        num_p, num_r, num_f1 = prf(number_counter(actual), number_counter(reference))
        rows[case.case_id]["comparison"] = {
            "reference": reference_label,
            "reference_kind": case.reference_kind,
            "character_sequence_similarity": SequenceMatcher(
                None, normalize(actual), normalize(reference), autojunk=False
            ).ratio(),
            "word_precision": word_p,
            "word_recall": word_r,
            "word_f1": word_f1,
            "numeric_precision": num_p,
            "numeric_recall": num_r,
            "numeric_f1": num_f1,
        }
        (out / f"{case.case_id}.json").write_text(
            json.dumps(rows[case.case_id], indent=2, ensure_ascii=False), encoding="utf-8"
        )


def write_summary(rows: dict[str, dict[str, Any]], out: Path) -> None:
    ordered = [rows[c.case_id] for c in CASES if c.case_id in rows]
    (out / "summary.json").write_text(
        json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    fields = [
        "case_id", "pdf", "ocr", "dpi", "pages", "preserve_small", "workers",
        "keep_headers_footers",
        "elapsed_seconds", "pages_returned", "text_chars", "word_tokens",
        "numeric_tokens", "markdown_table_rows", "reference", "reference_kind",
        "character_sequence_similarity", "word_f1", "numeric_f1",
    ]
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in ordered:
            case = row["case"]
            comp = row.get("comparison", {})
            writer.writerow(
                {
                    "case_id": case["case_id"], "pdf": case["pdf"],
                    "ocr": case["ocr"], "dpi": case["dpi"], "pages": case["pages"],
                    "preserve_small": case["preserve_small"],
                    "workers": case.get("workers", 4),
                    "keep_headers_footers": case.get("keep_headers_footers", False),
                    "elapsed_seconds": f'{row["elapsed_seconds"]:.4f}',
                    "pages_returned": row["pages_returned"], "text_chars": row["text_chars"],
                    "word_tokens": row["word_tokens"], "numeric_tokens": row["numeric_tokens"],
                    "markdown_table_rows": row["markdown_table_rows"],
                    "reference": comp.get("reference"), "reference_kind": comp.get("reference_kind"),
                    "character_sequence_similarity": comp.get("character_sequence_similarity"),
                    "word_f1": comp.get("word_f1"), "numeric_f1": comp.get("numeric_f1"),
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--case", action="append", help="Run only the named case; repeatable")
    parser.add_argument("--resume", action="store_true", help="Reuse existing case JSON artifacts")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    selected = [c for c in CASES if not args.case or c.case_id in set(args.case)]
    unknown = set(args.case or []) - {c.case_id for c in CASES}
    if unknown:
        raise SystemExit(f"unknown case(s): {', '.join(sorted(unknown))}")

    rows: dict[str, dict[str, Any]] = {}
    for index, case in enumerate(selected, 1):
        artifact_path = args.out / f"{case.case_id}.json"
        if args.resume and artifact_path.exists():
            rows[case.case_id] = json.loads(artifact_path.read_text(encoding="utf-8"))
            print(f"[{index}/{len(selected)}] reuse {case.case_id}", flush=True)
            continue
        print(f"[{index}/{len(selected)}] run {case.case_id}", flush=True)
        try:
            rows[case.case_id] = run_case(case, args.out)
        except Exception as exc:  # keep the sweep moving and preserve the failure
            rows[case.case_id] = {
                "case": asdict(case), "liteparse_version": "2.10.1",
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"  ERROR: {rows[case.case_id]['error']}", file=sys.stderr, flush=True)

    # Load references that may not have been selected in a partial run.
    for case in CASES:
        path = args.out / f"{case.case_id}.json"
        if case.case_id not in rows and path.exists():
            rows[case.case_id] = json.loads(path.read_text(encoding="utf-8"))
    add_comparisons(rows, args.out)
    write_summary(rows, args.out)


if __name__ == "__main__":
    main()
