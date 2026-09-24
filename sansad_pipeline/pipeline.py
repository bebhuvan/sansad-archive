from __future__ import annotations

import json
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import json_text
from .liteparse_engine import ExtractedPage, LiteParseEngine
from .routing import decide
from .storage import Store
from .validation import validate


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Pipeline:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config)
        self.store.initialize()
        self.engine = LiteParseEngine(config)

    def process(self, identifier: str, *, force: bool = False) -> int:
        document = self.store.document(identifier)
        digest = document["sha256"]
        config_snapshot = {
            "liteparse": asdict(self.config.liteparse),
            "routing": asdict(self.config.routing),
            "validation": asdict(self.config.validation),
        }
        config_json = json_text(config_snapshot)
        if not force:
            previous = self.store.db.one(
                """SELECT id FROM runs
                   WHERE document_sha256 = ? AND status = 'complete' AND config_json = ?
                   ORDER BY id DESC""",
                (digest, config_json),
            )
            if previous:
                return int(previous["id"])
        run_id = self.store.db.execute(
            """INSERT INTO runs(document_sha256, status, config_json, started_at)
               VALUES (?, 'running', ?, ?)""",
            (digest, config_json, utcnow()),
        )
        artifact_dir = self.config.data_root / "artifacts" / digest / f"run-{run_id:08d}"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        self.store.db.execute(
            "UPDATE runs SET artifact_dir = ? WHERE id = ?", (str(artifact_dir), run_id)
        )
        try:
            native_pages = self.engine.extract(Path(document["raw_path"]), ocr=False)
            native_by_number = {page.page_number: page for page in native_pages}
            decisions = {page.page_number: decide(page, self.config.routing) for page in native_pages}
            ocr_numbers = [number for number, decision in decisions.items() if decision.route == "ocr"]
            ocr_by_number: dict[int, ExtractedPage] = {}
            if ocr_numbers:
                fresh_numbers = [
                    number for number in ocr_numbers
                    if "full-page-image" in decisions[number].reasons
                ]
                standard_numbers = [number for number in ocr_numbers if number not in fresh_numbers]
                if standard_numbers:
                    ocr_pages = self.engine.extract(
                        Path(document["raw_path"]), ocr=True, target_pages=standard_numbers
                    )
                    ocr_by_number.update({page.page_number: page for page in ocr_pages})
                if fresh_numbers:
                    fresh_pages = self.engine.extract(
                        Path(document["raw_path"]),
                        ocr=True,
                        target_pages=fresh_numbers,
                        rasterize=True,
                    )
                    ocr_by_number.update({page.page_number: page for page in fresh_pages})

            document_markdown: list[str] = []
            document_json: list[dict] = []
            for page_number in sorted(native_by_number):
                native = native_by_number[page_number]
                decision = decisions[page_number]
                selected = ocr_by_number.get(page_number, native) if decision.route == "ocr" else native
                route = "ocr" if selected is not native else "native"
                validation = validate(
                    selected,
                    route=route,
                    native=native,
                    config=self.config.validation,
                )
                record = {
                    "document_sha256": digest,
                    "run_id": run_id,
                    "page_number": page_number,
                    "route": route,
                    "route_reasons": list(decision.reasons),
                    "engine": self.engine.name,
                    "engine_version": self.engine.version,
                    "text": selected.text,
                    "markdown": selected.markdown,
                    "width": selected.width,
                    "height": selected.height,
                    "mean_confidence": selected.mean_confidence,
                    "text_items": selected.text_items,
                    "vector_lines": selected.vector_lines,
                    "vector_shapes": selected.vector_shapes,
                    "complexity": selected.complexity,
                    "validation_status": validation.status,
                    "validation_flags": list(validation.flags),
                }
                page_path = artifact_dir / f"page-{page_number:05d}.json"
                page_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                self.store.db.execute(
                    """INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, page_number, route, self.engine.name, len(selected.text),
                        selected.mean_confidence, validation.status,
                        json_text(list(validation.flags)), str(page_path),
                    ),
                )
                document_markdown.append(
                    f"<!-- page:{page_number} route:{route} -->\n\n{selected.markdown}"
                )
                document_json.append(record)
            (artifact_dir / "document.md").write_text(
                "\n\n---\n\n".join(document_markdown), encoding="utf-8"
            )
            (artifact_dir / "document.json").write_text(
                json.dumps(document_json, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest = {
                "document_sha256": digest,
                "source_pdf": document["raw_path"],
                "run_id": run_id,
                "engine": self.engine.name,
                "engine_version": self.engine.version,
                "page_count": len(document_json),
                "native_pages": sum(row["route"] == "native" for row in document_json),
                "ocr_pages": sum(row["route"] == "ocr" for row in document_json),
                "review_pages": [
                    row["page_number"] for row in document_json
                    if row["validation_status"] == "review"
                ],
                "config": config_snapshot,
            }
            (artifact_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.store.db.execute(
                "UPDATE runs SET status = 'complete', finished_at = ? WHERE id = ?",
                (utcnow(), run_id),
            )
            return run_id
        except Exception as error:
            self.store.db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (utcnow(), f"{type(error).__name__}: {error}\n{traceback.format_exc()}", run_id),
            )
            raise

    def export_jsonl(
        self,
        output: Path,
        *,
        accepted_only: bool = False,
        with_adjudications: bool = False,
    ) -> int:
        if accepted_only and with_adjudications:
            condition = "AND (p.validation_status = 'accepted' OR a.id IS NOT NULL)"
        elif accepted_only:
            condition = "AND p.validation_status = 'accepted'"
        else:
            condition = ""
        rows = self.store.db.all(
            f"""SELECT p.artifact_json,a.response_path,a.model,a.reported_cost,
                        a.prompt_tokens,a.completion_tokens,a.total_tokens
                 FROM pages p
                 JOIN runs r ON r.id = p.run_id
                 LEFT JOIN adjudications a ON a.id = (
                     SELECT MAX(a2.id) FROM adjudications a2
                     WHERE a2.run_id=p.run_id AND a2.page_number=p.page_number
                 )
                 WHERE r.status = 'complete'
                   AND r.id = (SELECT MAX(r2.id) FROM runs r2
                               WHERE r2.document_sha256 = r.document_sha256
                                 AND r2.status = 'complete')
                   {condition}
                 ORDER BY r.document_sha256, p.page_number"""
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            for row in rows:
                payload = json.loads(Path(row["artifact_json"]).read_text(encoding="utf-8"))
                if with_adjudications and row["response_path"]:
                    response = json.loads(Path(row["response_path"]).read_text(encoding="utf-8"))
                    payload["canonical_markdown"] = response["choices"][0]["message"]["content"]
                    payload["canonical_source"] = {
                        "type": "openrouter_adjudication",
                        "model": row["model"],
                        "reported_cost": row["reported_cost"],
                        "prompt_tokens": row["prompt_tokens"],
                        "completion_tokens": row["completion_tokens"],
                        "total_tokens": row["total_tokens"],
                        "response_path": row["response_path"],
                    }
                elif with_adjudications:
                    payload["canonical_markdown"] = payload["markdown"]
                    payload["canonical_source"] = {
                        "type": "local_extraction",
                        "engine": payload["engine"],
                        "engine_version": payload["engine_version"],
                    }
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return len(rows)
