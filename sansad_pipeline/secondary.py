from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

from .config import Config
from .storage import Store


NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> Counter[str]:
    return Counter(token.replace(",", "") for token in NUMBER.findall(text))


def f1(left: Counter[str], right: Counter[str]) -> float:
    overlap = sum((left & right).values())
    precision = overlap / sum(left.values()) if left else 0.0
    recall = overlap / sum(right.values()) if right else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def compare_pdf_inspector(config: Config, identifier: str) -> Path:
    try:
        import pdf_inspector  # type: ignore
    except ImportError as error:
        raise RuntimeError("pdf-inspector is not installed; run scripts/install_pdf_inspector.sh") from error
    store = Store(config)
    store.initialize()
    document = store.document(identifier)
    run = store.db.one(
        "SELECT * FROM runs WHERE document_sha256=? AND status='complete' ORDER BY id DESC",
        (document["sha256"],),
    )
    if not run:
        raise RuntimeError("process the document with LiteParse first")
    liteparse_pages = json.loads(
        (Path(run["artifact_dir"]) / "document.json").read_text(encoding="utf-8")
    )
    liteparse_text = "\n".join(page["text"] for page in liteparse_pages)
    started = time.monotonic()
    result = pdf_inspector.process_pdf(document["raw_path"])
    elapsed = time.monotonic() - started
    inspector_markdown = result.markdown or ""
    destination = Path(run["artifact_dir"]) / "secondary" / "pdf-inspector"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "document.md").write_text(inspector_markdown, encoding="utf-8")
    report = {
        "document_sha256": document["sha256"],
        "liteparse_run_id": run["id"],
        "engine": "pdf-inspector",
        "source_tag": "v0.7.0",
        "elapsed_seconds": elapsed,
        "pdf_type": result.pdf_type,
        "confidence": result.confidence,
        "page_count": result.page_count,
        "pages_needing_ocr": list(result.pages_needing_ocr),
        "is_complex_layout": result.is_complex_layout,
        "pages_with_tables": list(result.pages_with_tables),
        "pages_with_columns": list(result.pages_with_columns),
        "has_encoding_issues": result.has_encoding_issues,
        "liteparse_text_characters": len(liteparse_text),
        "pdf_inspector_markdown_characters": len(inspector_markdown),
        "numeric_token_f1_against_liteparse": f1(
            numbers(inspector_markdown), numbers(liteparse_text)
        ),
        "warning": "Agreement is not ground truth; inspect disagreements against the PDF image.",
    }
    report_path = destination / "comparison.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report_path

