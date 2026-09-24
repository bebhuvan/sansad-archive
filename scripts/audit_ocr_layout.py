#!/usr/bin/env python3
"""Sample OCR pages against a separate, free Tesseract reading-order pass.

This is an audit, not an automatic correction. It keeps the existing LiteParse
and Space Bunny artifacts unchanged and stores the independent transcript in
the run's verification report on Hugging Face.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sansad_pipeline.config import load_config  # noqa: E402
from sansad_pipeline.openrouter import render_page  # noqa: E402
from sansad_pipeline.storage import Store  # noqa: E402
from sansad_pipeline.validation import content_numbers, numbers  # noqa: E402


def separator_rows(markdown: str) -> int:
    return sum(
        line.lstrip().startswith("|")
        and "-" in line
        and set(line.strip()) <= {"|", "-", ":", " "}
        for line in markdown.splitlines()
    )


def similarity(left: str, right: str) -> float:
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[#*_>`|]", " ", value)).strip()

    if not left.strip() or not right.strip():
        return 0.0
    return round(difflib.SequenceMatcher(None, normalize(left), normalize(right)).ratio(), 4)


def numeric_difference(candidate: str, tesseract: str) -> dict[str, list[str]]:
    """Expose disagreements without treating either OCR output as ground truth."""
    candidate_numbers = numbers(candidate)
    tesseract_numbers = numbers(tesseract)
    return {
        "candidate_only": sorted((candidate_numbers - tesseract_numbers).elements()),
        "tesseract_only": sorted((tesseract_numbers - candidate_numbers).elements()),
    }


def sample_rows(rows: list[dict], count: int, seed: int) -> list[dict]:
    if count <= 0:
        raise ValueError("sample size must be positive")
    rng = random.Random(seed)
    selected: list[dict] = []
    chosen: set[tuple[str, int]] = set()
    quota = max(1, count // 3)
    for reason, candidates in (
        ("layout", [row for row in rows if row["separator_rows"] >= 3]),
        ("numeric", [row for row in rows if row.get("model_numeric_disagreement")]),
    ):
        remaining = [row for row in candidates
                     if (row["document_sha256"], row["page_number"]) not in chosen]
        for row in rng.sample(remaining, min(len(remaining), quota, count - len(selected))):
            selected.append({**row, "selection_stratum": reason})
            chosen.add((row["document_sha256"], row["page_number"]))
    remaining = [row for row in rows
                 if (row["document_sha256"], row["page_number"]) not in chosen]
    selected.extend({**row, "selection_stratum": "random"}
                    for row in rng.sample(remaining, min(len(remaining), count - len(selected))))
    return sorted(selected, key=lambda row: (row["document_sha256"], row["page_number"]))


def page_rows(store: Store, house: str, parliament: str, session: str) -> list[dict]:
    rows = store.db.all(
        """WITH scope_docs AS (
             SELECT DISTINCT c.document_sha256 FROM census_records c
             WHERE c.source_type='questions_answers' AND c.house=?
               AND c.parliament_number=? AND c.session=?
               AND c.document_sha256 IS NOT NULL
           )
           SELECT r.document_sha256,p.page_number,p.artifact_json,d.raw_path,r.id AS run_id,
                  (SELECT a.response_path FROM adjudications a
                    WHERE a.run_id=r.id AND a.page_number=p.page_number
                      AND a.provider='openrouter'
                    ORDER BY a.id DESC LIMIT 1) model_response_path
           FROM scope_docs s JOIN runs r ON r.document_sha256=s.document_sha256
           JOIN pages p ON p.run_id=r.id
           JOIN documents d ON d.sha256=r.document_sha256
           WHERE r.status='complete' AND p.route='ocr'
             AND r.id=(SELECT MAX(r2.id) FROM runs r2
                       WHERE r2.document_sha256=r.document_sha256
                         AND r2.status='complete')
           ORDER BY r.document_sha256,p.page_number""",
        (house, parliament, session),
    )
    candidates = []
    for row in rows:
        page = json.loads(Path(row["artifact_json"]).read_text(encoding="utf-8"))
        response = row["model_response_path"]
        model = (Path(response).with_name("adjudicated.md").read_text(encoding="utf-8")
                 if response else None)
        candidates.append({**dict(row), "separator_rows": separator_rows(page["markdown"]),
                           "local_markdown": page["markdown"], "model_markdown": model,
                           "model_numeric_disagreement": model is not None and
                           content_numbers(page["markdown"]) != content_numbers(model),
                           "model_raw_numeric_disagreement": model is not None and
                           numbers(page["markdown"]) != numbers(model)})
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("pipeline.toml"))
    parser.add_argument("--house", required=True)
    parser.add_argument("--parliament", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--sample", type=int, default=12)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.sample < 1:
        parser.error("--sample must be positive")
    executable = shutil.which("tesseract")
    if not executable:
        raise SystemExit("Tesseract executable is required for layout audit")
    version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                             check=True).stdout.splitlines()[0]
    config = load_config(args.config)
    store = Store(config)
    store.initialize()
    candidates = page_rows(store, args.house, args.parliament, args.session)
    selected = sample_rows(candidates, args.sample, args.seed)
    args.output.mkdir(parents=True, exist_ok=True)
    report = args.output / "tesseract-layout-audit.jsonl"
    failures = 0
    model_pages = 0
    with report.open("w", encoding="utf-8") as handle:
        for row in selected:
            model = row["model_markdown"]
            model_pages += model is not None
            record = {
                "document_sha256": row["document_sha256"],
                "page_number": row["page_number"],
                "liteparse_separator_rows": row["separator_rows"],
                "selection_stratum": row["selection_stratum"],
                "model_local_numeric_disagreement": row["model_numeric_disagreement"],
                "model_local_raw_numeric_disagreement": row["model_raw_numeric_disagreement"],
                "tesseract_version": version,
                "psm": 3,
                "language": "eng",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                with tempfile.TemporaryDirectory() as directory:
                    image = render_page(Path(row["raw_path"]), row["page_number"],
                                        Path(directory), config.liteparse.full_page_image_dpi)
                    result = subprocess.run(
                        [executable, str(image), "stdout", "-l", "eng", "--psm", "3"],
                        capture_output=True, text=True, timeout=180, check=True,
                    )
                transcript = result.stdout
                if not transcript.strip():
                    raise RuntimeError("Tesseract returned empty text")
                record.update({
                    "tesseract_text": transcript,
                    "tesseract_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
                    "local_similarity": similarity(transcript, row["local_markdown"]),
                    "local_numeric_agreement": numbers(transcript) == numbers(row["local_markdown"]),
                    "local_numeric_difference": numeric_difference(row["local_markdown"], transcript),
                    "model_similarity": similarity(transcript, model) if model is not None else None,
                    "model_numeric_agreement": numbers(transcript) == numbers(model)
                    if model is not None else None,
                    "model_numeric_difference": numeric_difference(model, transcript)
                    if model is not None else None,
                })
            except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                failures += 1
                record["error"] = f"{type(error).__name__}: {error}"
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary = {
        "scope": {"house": args.house, "parliament": args.parliament, "session": args.session},
        "ocr_candidates": len(candidates), "sampled": len(selected),
        "model_pages": model_pages, "failures": failures,
        "selection_strata": {reason: sum(row["selection_stratum"] == reason for row in selected)
                             for reason in ("layout", "numeric", "random")},
        "report": report.name, "tesseract_version": version,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
