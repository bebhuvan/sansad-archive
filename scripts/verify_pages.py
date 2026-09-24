#!/usr/bin/env python3
"""Cross-model verification of extracted pages against an independent model.

Sends sampled page images to one or more OpenAI-compatible vision models and
records agreement metrics against the stored local extraction. This is evidence
about accuracy, never an automatic correction: it never mutates page artifacts,
adjudications, or canonical text.

OpenCode Go is the default provider because its MiMo-V2.6-Flash and
Space Bunny Free endpoints are free or included in the subscription. The key is
read from OPENCODE_API_KEY, falling back to the local OpenCode auth file.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sansad_pipeline.config import load_config  # noqa: E402
from sansad_pipeline.openrouter import BASE_PROMPT, PROMPT, render_page  # noqa: E402
from sansad_pipeline.storage import Store  # noqa: E402
from sansad_pipeline.validation import numbers, table_widths  # noqa: E402


DEFAULT_ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_key(env_name: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    auth_path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if auth_path.is_file():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        entry = auth.get("opencode-go") or {}
        return str(entry.get("key") or "").strip()
    return ""


def normalize(text: str, limit: int = 20000) -> str:
    # Markdown emphasis, headings and table pipes differ between models without
    # changing content; strip them so similarity measures text, not formatting.
    text = re.sub(r"[#*_>`|]", " ", text)
    return re.sub(r"\s+", " ", text)[:limit].strip()


def similarity(left: str, right: str) -> float:
    if not left.strip() or not right.strip():
        return 0.0
    return round(difflib.SequenceMatcher(None, normalize(left), normalize(right)).ratio(), 4)


def post_json(url: str, headers: dict, payload: dict, *, retries: int = 3) -> tuple[dict, float]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read().decode("utf-8")), time.monotonic() - started
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {error.code}: {detail[:500]}")
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt >= retries:
                raise last_error from error
            time.sleep(min(60.0, 2**attempt))
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt >= retries:
                raise
            time.sleep(min(60.0, 2**attempt))
    raise last_error or RuntimeError("request failed")


def build_tasks(store: Store, *, house: str, parliament: str, session: str,
                sample: int, seed: int, all_pages: bool) -> list[tuple[str, int]]:
    rows = store.db.all(
        """SELECT DISTINCT c.document_sha256
             FROM census_records c
             JOIN runs r ON r.id=(SELECT MAX(r2.id) FROM runs r2
                                  WHERE r2.document_sha256=c.document_sha256
                                    AND r2.status='complete')
            WHERE c.house=? AND COALESCE(c.parliament_number,'')=? AND c.session=?
            ORDER BY c.document_sha256""",
        (house, parliament, session),
    )
    review: list[tuple[str, int]] = []
    accepted: list[tuple[str, int]] = []
    for row in rows:
        digest = row["document_sha256"]
        run = store.db.one(
            "SELECT id FROM runs WHERE document_sha256=? AND status='complete' ORDER BY id DESC LIMIT 1",
            (digest,),
        )
        if run is None:
            continue
        for page in store.db.all(
            "SELECT page_number,validation_status FROM pages WHERE run_id=? ORDER BY page_number",
            (run["id"],),
        ):
            target = review if page["validation_status"] == "review" else accepted
            target.append((digest, int(page["page_number"])))
    candidates = review + accepted if not all_pages else review + accepted
    if sample and sample < len(candidates):
        candidates = sorted(random.Random(seed).sample(candidates, sample))
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("pipeline.toml"))
    parser.add_argument("--house", required=True)
    parser.add_argument("--parliament", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--sample", type=int, default=50, help="0 means every page")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--models", default="mimo-v2.6-flash")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--api-key-env", default="OPENCODE_API_KEY")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = load_config(args.config)
    store = Store(config)
    store.initialize()
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    if not models:
        raise SystemExit("--models must name at least one model")
    api_key = resolve_key(args.api_key_env)
    if not api_key:
        raise SystemExit(
            f"{args.api_key_env} is not set and no OpenCode auth file was found"
        )

    tasks = build_tasks(
        store,
        house=args.house,
        parliament=args.parliament,
        session=args.session,
        sample=args.sample,
        seed=args.seed,
        all_pages=False,
    )
    if not tasks:
        raise SystemExit("no processed pages found for this scope")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output = args.output or (config.project_root / "results" / "verification" / stamp)
    output.mkdir(parents=True, exist_ok=True)
    session_id = f"sansad-verify-{uuid.uuid4()}"
    results: list[dict] = []

    for digest, page_number in tasks:
        document = store.document(digest)
        run = store.db.one(
            "SELECT * FROM runs WHERE document_sha256=? AND status='complete' ORDER BY id DESC LIMIT 1",
            (digest,),
        )
        page_row = store.db.one(
            "SELECT * FROM pages WHERE run_id=? AND page_number=?",
            (run["id"], page_number),
        )
        if page_row is None:
            continue
        local = json.loads(Path(page_row["artifact_json"]).read_text(encoding="utf-8"))
        adjudication = store.db.one(
            """SELECT * FROM adjudications WHERE run_id=? AND page_number=?
               ORDER BY id DESC LIMIT 1""",
            (run["id"], page_number),
        )
        canonical = str(local["markdown"])
        canonical_source = f"local:{local['engine']}@{local['engine_version']}"
        if adjudication:
            candidate_path = Path(adjudication["response_path"]).with_name("adjudicated.md")
            if candidate_path.is_file():
                canonical = candidate_path.read_text(encoding="utf-8")
                canonical_source = f"{adjudication['provider']}:{adjudication['model']}"

        for model in models:
            model_dir = output / "pages" / f"{digest[:12]}-p{page_number:05d}" / re.sub(
                r"[^a-zA-Z0-9._-]+", "__", model
            )
            record = {
                "document_sha256": digest,
                "page_number": page_number,
                "model": model,
                "route": local["route"],
                "local_validation_status": local["validation_status"],
                "canonical_source": canonical_source,
                "created_at": utcnow(),
            }
            try:
                image = render_page(
                    Path(document["raw_path"]), page_number, model_dir, config.openrouter.image_dpi
                )
                mime = "image/png" if image.suffix.casefold() == ".png" else "image/jpeg"
                image_url = f"data:{mime};base64,{base64.b64encode(image.read_bytes()).decode('ascii')}"
                candidate = str(local.get("markdown") or "")
                prompt = PROMPT.format(candidate=candidate) if candidate.strip() else BASE_PROMPT
                payload: dict = {
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }],
                    "temperature": 0,
                    "max_tokens": config.openrouter.max_tokens,
                    "stream": False,
                }
                if "space-bunny" in model:
                    payload["reasoning_effort"] = config.openrouter.reasoning_effort or "low"
                    payload["reasoning"] = {"exclude": True}
                request_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                response, seconds = post_json(
                    args.endpoint,
                    {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "x-opencode-session": session_id,
                        "User-Agent": "sansad-verify/0.1",
                    },
                    payload,
                )
                message = (response.get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""
                usage = response.get("usage") or {}
                (model_dir / "response.json").write_text(
                    json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (model_dir / "adjudicated.md").write_text(message, encoding="utf-8")
                (model_dir / "provenance.json").write_text(
                    json.dumps(
                        {
                            "provider": "opencode-go",
                            "endpoint": args.endpoint,
                            "model": model,
                            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                            "session_id": session_id,
                            "image_dpi": config.openrouter.image_dpi,
                            "created_at": utcnow(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                local_numbers = numbers(local["text"])
                canonical_numbers = numbers(canonical)
                widths = table_widths(message)
                record.update(
                    {
                        "status": "ok",
                        "seconds": round(seconds, 1),
                        "empty_output": not message.strip(),
                        "local_numeric_disagreement": numbers(message) != local_numbers,
                        "canonical_numeric_disagreement": numbers(message) != canonical_numbers,
                        "local_similarity": similarity(message, local["markdown"]),
                        "canonical_similarity": similarity(message, canonical),
                        "table_width_consistent": len(set(widths)) <= 1 if widths else True,
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                    }
                )
            except Exception as error:  # keep verifying the rest of the sample
                record.update({"status": "error", "error": str(error)[:500]})
            results.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)

    successful = [item for item in results if item["status"] == "ok"]
    summary = {
        "scope": {"house": args.house, "parliament": args.parliament, "session": args.session},
        "endpoint": args.endpoint,
        "models": models,
        "tasks": len(results),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "created_at": utcnow(),
        "per_model": {},
    }
    for model in models:
        rows = [item for item in successful if item["model"] == model]
        if not rows:
            summary["per_model"][model] = {"pages": 0}
            continue
        summary["per_model"][model] = {
            "pages": len(rows),
            "empty_output_rate": round(sum(item["empty_output"] for item in rows) / len(rows), 4),
            "canonical_numeric_disagreement_rate": round(
                sum(item["canonical_numeric_disagreement"] for item in rows) / len(rows), 4
            ),
            "local_numeric_disagreement_rate": round(
                sum(item["local_numeric_disagreement"] for item in rows) / len(rows), 4
            ),
            "table_inconsistent_rate": round(
                sum(not item["table_width_consistent"] for item in rows) / len(rows), 4
            ),
            "mean_canonical_similarity": round(
                sum(item["canonical_similarity"] for item in rows) / len(rows), 4
            ),
            "mean_seconds": round(sum(item["seconds"] for item in rows) / len(rows), 1),
            "total_tokens": sum(
                (item["prompt_tokens"] or 0) + (item["completion_tokens"] or 0) for item in rows
            ),
        }
    (output / "report.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output / "tasks.jsonl").open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    lines = [
        "# Cross-model page verification",
        "",
        f"- Scope: `{args.house}` parliament `{args.parliament}` session `{args.session}`",
        f"- Endpoint: `{args.endpoint}`",
        f"- Pages sampled: {summary['tasks']} ({summary['successful']} succeeded)",
        "",
        "| Model | Pages | Empty | Numeric disagreement vs canonical | Table inconsistent | Mean similarity | Mean seconds |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model, stats in summary["per_model"].items():
        if not stats.get("pages"):
            lines.append(f"| `{model}` | 0 | - | - | - | - | - |")
            continue
        lines.append(
            f"| `{model}` | {stats['pages']} | {stats['empty_output_rate']:.1%} | "
            f"{stats['canonical_numeric_disagreement_rate']:.1%} | "
            f"{stats['table_inconsistent_rate']:.1%} | "
            f"{stats['mean_canonical_similarity']:.3f} | {stats['mean_seconds']} |"
        )
    lines += [
        "",
        "Numeric disagreement is evidence for human review, not an automatic error.",
        "Raw model responses and request hashes are stored per page under `pages/`.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": summary}, indent=2))
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
