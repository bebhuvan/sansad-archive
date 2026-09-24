#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "openrouter_qwen_trial"
WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*|\d[\d,]*(?:\.\d+)?")
NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")

RASTER_RUN = ROOT / "data/artifacts/89d68a3f9c9d0210521fc561aa4e7d1053668bdcca3b75b88842570ece167345/run-00000002"
NATIVE_RUN = ROOT / "data/artifacts/adbc7e0d8e65da4a9fb6f810706eb7806778b861245e09e03b12ed9952bb4750/run-00000003"
OLD_RUN = ROOT / "data/artifacts/d2f0f9b71ff3ba553ceb5a4c1ca7ebd845390eba861c922b02f8eba364d404ce/run-00000005"
MODELS = {
    "qwen/qwen3.7-flash": "qwen__qwen3.7-flash",
    "qwen/qwen3.5-9b": "qwen__qwen3.5-9b",
}


def counter(pattern: re.Pattern[str], text: str) -> Counter[str]:
    return Counter(token.casefold().replace(",", "") for token in pattern.findall(text))


def score(found: Counter[str], expected: Counter[str]) -> dict:
    overlap = sum((found & expected).values())
    precision = overlap / sum(found.values()) if found else 0.0
    recall = overlap / sum(expected.values()) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "exact_multiset": found == expected}


def table_rows(markdown: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in markdown.splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [re.sub(r"[*_`]", "", cell).strip() for cell in line[1:-1].split("|")]
        key = cells[0].casefold() if cells else ""
        if key.isdigit() or "total" in key or (not key and len(cells) > 1 and "total" in cells[1].casefold()):
            rows[key or "total"] = cells
    return rows


def table_score(candidate: str, reference: str) -> dict | None:
    expected = table_rows(reference)
    if not expected:
        return None
    found = table_rows(candidate)
    correct = 0
    cells = 0
    missing_rows = []
    for key, expected_cells in expected.items():
        actual_cells = found.get(key)
        if actual_cells is None:
            missing_rows.append(key)
            cells += len(expected_cells)
            continue
        cells += len(expected_cells)
        correct += sum(left == right for left, right in zip(actual_cells, expected_cells))
    return {
        "expected_rows": len(expected),
        "found_rows": len(found),
        "missing_rows": missing_rows,
        "exact_cells": correct,
        "expected_cells": cells,
        "cell_accuracy": correct / cells if cells else 0.0,
    }


def evaluate(candidate: str, reference: str) -> dict:
    return {
        "word": score(counter(WORD, candidate), counter(WORD, reference)),
        "numeric": score(counter(NUMBER, candidate), counter(NUMBER, reference)),
        "table": table_score(candidate, reference),
    }


def page_json(run: Path, page: int) -> dict:
    return json.loads((run / f"page-{page:05d}.json").read_text(encoding="utf-8"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cases = []
    model_totals: dict[str, dict] = defaultdict(
        lambda: {"successful_pages": 0, "failed_pages": 0, "prompt_tokens": 0,
                 "completion_tokens": 0, "total_tokens": 0, "reported_cost": 0.0}
    )

    gold = (ROOT / "samples/gold/india1985_p3.txt").read_text(encoding="utf-8")
    local_old = page_json(OLD_RUN, 3)
    cases.append({"sample": "india1985", "page": 3, "engine": "liteparse-ocr", **evaluate(local_old["text"], gold)})

    for model, model_dir in MODELS.items():
        path = OLD_RUN / "openrouter" / model_dir / "page-00003"
        text = (path / "adjudicated.md").read_text(encoding="utf-8")
        response = json.loads((path / "response.json").read_text(encoding="utf-8"))
        usage = response.get("usage") or {}
        cases.append({"sample": "india1985", "page": 3, "engine": model, **evaluate(text, gold)})
        totals = model_totals[model]
        totals["successful_pages"] += 1
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[field] += usage.get(field, 0) or 0
        totals["reported_cost"] += usage.get("cost", 0) or 0

    for page in (2, 3):
        reference = page_json(NATIVE_RUN, page)["markdown"]
        local = page_json(RASTER_RUN, page)
        cases.append({"sample": "controlled_table_scan", "page": page,
                      "engine": "liteparse-ocr", **evaluate(local["markdown"], reference)})
        for model, model_dir in MODELS.items():
            path = RASTER_RUN / "openrouter" / model_dir / f"page-{page:05d}"
            if not (path / "adjudicated.md").exists():
                cases.append({"sample": "controlled_table_scan", "page": page,
                              "engine": model, "error": "provider moderation rejection"})
                model_totals[model]["failed_pages"] += 1
                continue
            text = (path / "adjudicated.md").read_text(encoding="utf-8")
            response = json.loads((path / "response.json").read_text(encoding="utf-8"))
            usage = response.get("usage") or {}
            cases.append({"sample": "controlled_table_scan", "page": page,
                          "engine": model, **evaluate(text, reference)})
            totals = model_totals[model]
            totals["successful_pages"] += 1
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                totals[field] += usage.get(field, 0) or 0
            totals["reported_cost"] += usage.get("cost", 0) or 0

    payload = {"cases": cases, "model_totals": model_totals}
    (OUT / "evaluation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

