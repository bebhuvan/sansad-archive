from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .storage import Store
from .validation import text_flags


FORMAT_VERSION = "1.2"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, max_length: int = 60) -> str:
    text = SLUG.sub("-", value.casefold()).strip("-")
    if len(text) > max_length:
        text = text[:max_length].rstrip("-")
    return text or "document"


def document_slug(record: dict, digest: str) -> str:
    date = str(record.get("document_date") or "undated").strip().replace(" ", "") or "undated"
    number = str(record.get("document_number") or "").strip()
    if not number:
        number = str(record.get("record_id") or "").split("_")[-1]
    title = str(record.get("title") or record.get("document_subtype") or "document")
    parts = [date]
    if number:
        prefix = "Q" if record.get("source_type") == "questions_answers" else "doc"
        parts.append(f"{prefix}{number}")
    parts.append(slugify(title, max_length=70))
    return f"{'_'.join(parts)}__{digest[:8]}"


def code_commit(project_root: Path) -> str | None:
    for key in ("GITHUB_SHA", "PIPELINE_COMMIT"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(payload))


def _tar_file(archive: tarfile.TarFile, name: str, path: Path) -> None:
    info = tarfile.TarInfo(name)
    info.size = path.stat().st_size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    with path.open("rb") as handle:
        archive.addfile(info, handle)


@dataclass(frozen=True)
class Scope:
    house: str
    parliament_number: str
    session: str
    source_type: str = "questions_answers"

    @property
    def slug(self) -> str:
        parliament = self.parliament_number or "na"
        return f"{self.house}/parliament-{parliament}/session-{self.session}"


class PublicationBuilder:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config)
        self.store.initialize()

    def _records(self, scope: Scope, limit: int | None) -> list[dict]:
        params: list[object] = [
            scope.source_type,
            scope.house,
            scope.parliament_number,
            scope.session,
        ]
        limit_sql = ""
        if limit is not None:
            if limit < 1:
                raise ValueError("publication limit must be positive")
            limit_sql = " LIMIT ?"
            params.append(limit)
        rows = self.store.db.all(
            """SELECT c.*,d.size_bytes,d.media_type,d.raw_path,
                      (SELECT MAX(r.id) FROM runs r
                       WHERE r.document_sha256=c.document_sha256
                         AND r.status='complete') AS run_id
                 FROM census_records c
                 JOIN documents d ON d.sha256=c.document_sha256
                WHERE c.source_type=? AND c.house=?
                  AND COALESCE(c.parliament_number,'')=? AND COALESCE(c.session,'')=?
                  AND EXISTS (SELECT 1 FROM runs r2
                              WHERE r2.document_sha256=c.document_sha256
                                AND r2.status='complete')
                ORDER BY c.document_date,c.document_number,c.record_id"""
            + limit_sql,
            tuple(params),
        )
        return [dict(row) for row in rows]

    def _optimized_pdf(
        self, source: Path, destination: Path, minimum_saving_percent: float
    ) -> dict | None:
        qpdf = shutil.which("qpdf")
        if not qpdf:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                qpdf,
                "--object-streams=generate",
                "--recompress-flate",
                "--compression-level=9",
                str(source),
                str(destination),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            [qpdf, "--check", str(destination)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        original_size = source.stat().st_size
        optimized_size = destination.stat().st_size
        saving = 100.0 * (original_size - optimized_size) / original_size
        if saving < minimum_saving_percent:
            destination.unlink()
            return None
        return {
            "size_bytes": optimized_size,
            "sha256": sha256_file(destination),
            "saving_percent": saving,
            "method": "qpdf lossless stream/object compression",
        }

    def build(
        self,
        scope: Scope,
        output: Path,
        *,
        limit: int | None = None,
        include_raw: bool = True,
        minimum_pdf_saving_percent: float = 5.0,
        canonical_policy: str = "local",
        compact: bool = False,
        complete_session: bool = False,
    ) -> dict:
        if canonical_policy not in {"local", "model"}:
            raise ValueError("canonical_policy must be 'local' or 'model'")
        try:
            import duckdb
            import pyarrow as pa
            import pyarrow.parquet as pq
            import zstandard
        except ImportError as error:
            raise RuntimeError(
                "publication dependencies are missing; install the `publication` extra"
            ) from error

        records = self._records(scope, limit)
        if not records:
            raise RuntimeError(f"no processed documents found for {scope.slug}")
        output = output.resolve()
        if output.exists() and any(output.iterdir()):
            raise RuntimeError(f"publication output is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        (output / "webdataset").mkdir()

        grouped: dict[str, list[dict]] = {}
        for record in records:
            grouped.setdefault(record["document_sha256"], []).append(record)

        document_rows: list[dict] = []
        page_rows: list[dict] = []
        optimization_rows: list[dict] = []
        manifest_rows: list[dict] = []
        tar_path = output / "webdataset" / "shard-00000.tar"
        with tempfile.TemporaryDirectory(dir=self.config.data_root / "tmp") as temp_dir:
            temp_root = Path(temp_dir)
            with tarfile.open(tar_path, "w") as archive:
                for digest, source_records in sorted(grouped.items()):
                    primary = source_records[0]
                    run = self.store.db.one("SELECT * FROM runs WHERE id=?", (primary["run_id"],))
                    if run is None or not run["artifact_dir"]:
                        raise RuntimeError(f"complete run artifacts missing for {digest}")
                    artifact_dir = Path(run["artifact_dir"])
                    document_json = json.loads(
                        (artifact_dir / "document.json").read_text(encoding="utf-8")
                    )
                    local_markdown = (artifact_dir / "document.md").read_text(encoding="utf-8")
                    publication_pages = []
                    for page in document_json:
                        page = dict(page)
                        page["local_text"] = page["text"]
                        page["local_markdown"] = page["markdown"]
                        page["adjudicated_text"] = None
                        page["adjudicated_markdown"] = None
                        page["adjudication"] = None
                        page["canonical_source"] = (
                            f"local:{page['engine']}@{page['engine_version']}"
                        )
                        page["canonical_validation"] = {
                            "status": page["validation_status"],
                            "flags": list(page["validation_flags"]),
                        }
                        adjudication = self.store.db.one(
                            """SELECT * FROM adjudications
                               WHERE run_id=? AND page_number=?
                               ORDER BY (provider='openrouter') DESC,id DESC LIMIT 1""",
                            (run["id"], page["page_number"]),
                        )
                        if adjudication:
                            response_path = Path(adjudication["response_path"])
                            adjudicated_path = response_path.with_name("adjudicated.md")
                            if adjudicated_path.is_file():
                                model_markdown = adjudicated_path.read_text(encoding="utf-8")
                                flags = text_flags(
                                    model_markdown,
                                    reference=page["local_markdown"],
                                    config=self.config.validation,
                                )
                                page["adjudicated_text"] = model_markdown
                                page["adjudicated_markdown"] = model_markdown
                                if canonical_policy == "model":
                                    page["markdown"] = model_markdown
                                    page["text"] = model_markdown
                                    page["canonical_source"] = (
                                        f"{adjudication['provider']}:{adjudication['model']}"
                                    )
                                    page["canonical_validation"] = {
                                        "status": "review" if flags else "accepted",
                                        "flags": list(flags),
                                    }
                                page["adjudication"] = {
                                    "provider": adjudication["provider"],
                                    "model": adjudication["model"],
                                    "request_sha256": adjudication["request_sha256"],
                                    "prompt_tokens": adjudication["prompt_tokens"],
                                    "completion_tokens": adjudication["completion_tokens"],
                                    "total_tokens": adjudication["total_tokens"],
                                    "reported_cost": adjudication["reported_cost"],
                                    "created_at": adjudication["created_at"],
                                    "validation_flags": list(flags),
                                }
                        publication_pages.append(page)
                    markdown = "\n\n".join(page["markdown"] for page in publication_pages)
                    plain_text = "\n\n".join(page["text"] for page in publication_pages)
                    adjudicated_markdown = "\n\n".join(
                        page["adjudicated_markdown"] or "" for page in publication_pages
                    )
                    raw_path = Path(primary["raw_path"])
                    if sha256_file(raw_path) != digest:
                        raise RuntimeError(f"raw PDF checksum mismatch: {raw_path}")

                    public_sources = [
                        {
                            key: record[key]
                            for key in (
                                "record_id",
                                "source_type",
                                "house",
                                "parliament_number",
                                "session",
                                "document_number",
                                "document_subtype",
                                "document_date",
                                "title",
                                "ministry",
                                "language",
                                "source_url",
                                "official_page_url",
                            )
                        }
                        for record in source_records
                    ]
                    payload = {
                        "format_version": FORMAT_VERSION,
                        "document_sha256": digest,
                        "size_bytes": primary["size_bytes"],
                        "media_type": primary["media_type"],
                        "sources": public_sources,
                        "run": {
                            "id": run["id"],
                            "config": json.loads(run["config_json"]),
                            "started_at": run["started_at"],
                            "finished_at": run["finished_at"],
                        },
                        "pages": publication_pages,
                    }
                    prefix = digest
                    _tar_bytes(archive, f"{prefix}.md", markdown.encode("utf-8"))
                    _tar_bytes(archive, f"{prefix}.local.md", local_markdown.encode("utf-8"))
                    if adjudicated_markdown.strip():
                        _tar_bytes(
                            archive,
                            f"{prefix}.adjudicated.md",
                            adjudicated_markdown.encode("utf-8"),
                        )
                    _tar_bytes(archive, f"{prefix}.txt", plain_text.encode("utf-8"))
                    _tar_bytes(
                        archive,
                        f"{prefix}.json",
                        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                    )
                    if include_raw:
                        _tar_file(archive, f"{prefix}.original.pdf", raw_path)
                        optimized_path = temp_root / f"{digest}.optimized.pdf"
                        optimized = self._optimized_pdf(
                            raw_path, optimized_path, minimum_pdf_saving_percent
                        )
                        if optimized:
                            _tar_file(archive, f"{prefix}.optimized.pdf", optimized_path)
                            optimization_rows.append({"document_sha256": digest, **optimized})

                    slug = document_slug(primary, digest)
                    readable_path = f"documents/{slug}" if not compact else None
                    readable_files = []
                    if not compact:
                        readable_dir = output / readable_path
                        readable_dir.mkdir(parents=True, exist_ok=True)
                        (readable_dir / "document.md").write_text(markdown, encoding="utf-8")
                        (readable_dir / "document.txt").write_text(plain_text, encoding="utf-8")
                        (readable_dir / "document.json").write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        readable_files = ["document.md", "document.txt", "document.json"]
                        if include_raw:
                            shutil.copyfile(raw_path, readable_dir / "original.pdf")
                            readable_files.insert(0, "original.pdf")
                    source_urls = sorted({item["source_url"] for item in public_sources})
                    manifest_rows.append(
                        {
                            "path": readable_path,
                            "webdataset_shard": "webdataset/shard-00000.tar",
                            "webdataset_key": digest,
                            "document_sha256": digest,
                            "house": primary["house"],
                            "parliament_number": primary["parliament_number"],
                            "session": primary["session"],
                            "source_type": primary["source_type"],
                            "document_date": primary["document_date"],
                            "document_number": primary["document_number"],
                            "document_subtype": primary["document_subtype"],
                            "title": primary["title"],
                            "ministry": primary["ministry"],
                            "language": primary["language"],
                            "record_ids": [item["record_id"] for item in public_sources],
                            "source_urls": source_urls,
                            "official_page_urls": sorted(
                                item["official_page_url"]
                                for item in public_sources
                                if item["official_page_url"]
                            ),
                            "page_count": len(publication_pages),
                            "adjudicated_page_count": sum(
                                1 for page in publication_pages if page["adjudication"]
                            ),
                            "files": readable_files,
                        }
                    )
                    document_rows.append(
                        {
                            "document_sha256": digest,
                            "readable_path": readable_path,
                            "size_bytes": primary["size_bytes"],
                            "media_type": primary["media_type"],
                            "record_ids": [item["record_id"] for item in public_sources],
                            "source_urls": source_urls,
                            "house": primary["house"],
                            "parliament_number": primary["parliament_number"],
                            "session": primary["session"],
                            "document_number": primary["document_number"],
                            "document_date": primary["document_date"],
                            "title": primary["title"],
                            "ministry": primary["ministry"],
                            "language": primary["language"],
                            "page_count": len(publication_pages),
                            "markdown": markdown,
                            "text": plain_text,
                            "run_id": int(run["id"]),
                            "engine": document_json[0]["engine"],
                            "engine_version": document_json[0]["engine_version"],
                        }
                    )
                    for page in publication_pages:
                        page_rows.append(
                            {
                                "document_sha256": digest,
                                "page_number": page["page_number"],
                                "text": page["text"],
                                "markdown": page["markdown"],
                                "local_text": page["local_text"],
                                "local_markdown": page["local_markdown"],
                                "adjudicated_markdown": page["adjudicated_markdown"],
                                "route": page["route"],
                                "route_reasons": page["route_reasons"],
                                "engine": page["engine"],
                                "engine_version": page["engine_version"],
                                "mean_confidence": page["mean_confidence"],
                                "validation_status": page["validation_status"],
                                "validation_flags": page["validation_flags"],
                                "width": page["width"],
                                "height": page["height"],
                                "adjudication_provider": (
                                    page["adjudication"]["provider"] if page["adjudication"] else None
                                ),
                                "adjudication_model": (
                                    page["adjudication"]["model"] if page["adjudication"] else None
                                ),
                                "adjudication_request_sha256": (
                                    page["adjudication"]["request_sha256"]
                                    if page["adjudication"] else None
                                ),
                                "model_validation_flags": (
                                    page["adjudication"]["validation_flags"]
                                    if page["adjudication"] else []
                                ),
                                "canonical_source": page["canonical_source"],
                                "canonical_status": page["canonical_validation"]["status"],
                                "canonical_flags": page["canonical_validation"]["flags"],
                            }
                        )

        with (output / "manifest.jsonl").open("w", encoding="utf-8") as handle:
            for row in manifest_rows:
                handle.write(_json_line(row).decode("utf-8"))

        documents_path = output / "documents.parquet"
        pages_path = output / "pages.parquet"
        pq.write_table(pa.Table.from_pylist(document_rows), documents_path, compression="zstd")
        pq.write_table(pa.Table.from_pylist(page_rows), pages_path, compression="zstd")

        compressor = zstandard.ZstdCompressor(level=10)
        for name, rows in (("documents.jsonl.zst", document_rows), ("pages.jsonl.zst", page_rows)):
            with (output / name).open("wb") as target:
                with compressor.stream_writer(target) as writer:
                    for row in rows:
                        writer.write(_json_line(row))

        database_path = output / "sansad.duckdb"
        connection = duckdb.connect(str(database_path))
        try:
            connection.execute(
                "CREATE TABLE documents AS SELECT * FROM read_parquet(?)", [str(documents_path)]
            )
            connection.execute(
                "CREATE TABLE pages AS SELECT * FROM read_parquet(?)", [str(pages_path)]
            )
        finally:
            connection.close()

        metadata = {
            "format_version": FORMAT_VERSION,
            "created_at": utcnow(),
            "scope": {
                "source_type": scope.source_type,
                "house": scope.house,
                "parliament_number": scope.parliament_number,
                "session": scope.session,
            },
            "record_count": len(records),
            "document_count": len(document_rows),
            "page_count": len(page_rows),
            "raw_pdfs_included": include_raw,
            "optimized_pdf_count": len(optimization_rows),
            "adjudicated_page_count": sum(
                1 for page in page_rows if page["adjudication_provider"] is not None
            ),
            "model_flagged_page_count": sum(
                bool(page["model_validation_flags"]) for page in page_rows
            ),
            "model_numeric_disagreement_page_count": sum(
                "candidate-numeric-disagreement" in page["model_validation_flags"]
                for page in page_rows
            ),
            "readable_document_count": 0 if compact else len(manifest_rows),
            "webdataset_document_count": len(manifest_rows),
            "compact": compact,
            "manifest": "manifest.jsonl",
            "canonical_policy": canonical_policy,
            "code_commit": code_commit(self.config.project_root),
            "pdf_optimizations": optimization_rows,
            "complete_session_claimed": complete_session,
        }
        (output / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output / "README.md").write_text(
            self.scope_readme(metadata), encoding="utf-8"
        )
        checksum_lines = []
        for path in sorted(item for item in output.rglob("*") if item.is_file()):
            if path.name == "SHA256SUMS":
                continue
            checksum_lines.append(f"{sha256_file(path)}  {path.relative_to(output).as_posix()}")
        (output / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
        return {**metadata, "output": str(output), "checksums": len(checksum_lines)}

    @staticmethod
    def verify(output: Path) -> dict:
        output = output.resolve()
        checksum_file = output / "SHA256SUMS"
        checked = 0
        failures = []
        for line in checksum_file.read_text(encoding="utf-8").splitlines():
            expected, relative = line.split("  ", 1)
            path = output / relative
            if not path.is_file():
                failures.append({"path": relative, "error": "missing"})
            else:
                actual = sha256_file(path)
                if actual != expected:
                    failures.append({"path": relative, "expected": expected, "actual": actual})
            checked += 1
        return {"checked": checked, "failures": failures, "valid": not failures}

    @staticmethod
    def scope_readme(metadata: dict) -> str:
        scope = metadata["scope"]
        commit = metadata.get("code_commit") or "unknown"
        coverage = (
            "The workflow verified full census, acquisition, extraction and model-page "
            "coverage before publishing this session."
            if metadata.get("complete_session_claimed") else
            "This is a bounded tranche and does not claim the named session is complete."
        )
        browse = (
            "- `webdataset/shard-00000.tar` holds each original PDF together with its "
            "Markdown, plain text and JSON under the full SHA-256 key.\n"
            "- `manifest.jsonl` maps each key and shard to official source URLs and "
            "question metadata.\n"
            if metadata.get("compact") else
            "- `documents/<readable-name>/` contains `original.pdf`, `document.md`, "
            "`document.txt` and `document.json` for each question.\n"
            "- `manifest.jsonl` maps each readable path and WebDataset key to its "
            "official source URLs and question metadata.\n"
            "- `webdataset/shard-00000.tar` also keeps each original and text layer "
            "under the full SHA-256 key.\n"
        )
        return f"""# Sansad corpus publication tranche

This is a provenance-preserving research tranche from the Sansad PDF corpus.
It contains original official PDFs, extracted Markdown and plain text, structured
JSON/Parquet page records, and a DuckDB snapshot. {coverage}

- House: `{scope['house']}`
- Parliament: `{scope['parliament_number']}`
- Session: `{scope['session']}`
- Documents: {metadata['document_count']}
- Pages: {metadata['page_count']}
- Losslessly optimized PDF derivatives: {metadata['optimized_pdf_count']}
- Model-reviewed pages: {metadata['adjudicated_page_count']}
- Pipeline commit: `{commit}`

## How to browse this tranche

{browse}
- `pages.parquet`, `pages.jsonl.zst` and `sansad.duckdb` carry per-page
  canonical text, the local parser candidate, validation flags and model
  provenance.
- `SHA256SUMS` verifies every file in the tranche.

Every page keeps its layers side by side: `local_text`/`local_markdown` from
the parser or OCR, `adjudicated_markdown` from the vision model when present,
and the canonical `text`/`markdown` chosen by policy. This tranche used
canonical policy `{metadata.get('canonical_policy', 'local')}`: `local` keeps
the parser/OCR text canonical with the model layer beside it for comparison,
`model` makes the stored model adjudication canonical when one exists. Original
PDFs retain their source copyright. Reproduction is for attributed,
non-commercial research. Extracted text is machine-generated and may contain
errors; validation status and provenance are included for every page.
"""


def dataset_card() -> str:
    return """---
license: other
language:
- en
task_categories:
- document-question-answering
- text-retrieval
tags:
- parliament
- india
- ocr
- public-policy
pretty_name: Sansad Corpus
---

# Sansad Corpus

A provenance-preserving corpus of Lok Sabha and Rajya Sabha documents for
non-commercial research. Publications are uploaded in bounded tranches and do
not imply completeness unless a release explicitly says so.

Each tranche provides original official PDFs, per-document Markdown, JSON and
plain text in WebDataset TAR shards, Zstandard-compressed JSONL, Parquet tables,
a DuckDB snapshot, and SHA-256 checksums.

## Provenance and limitations

Every document retains its official source URL and SHA-256. Original Parliament
material retains its source copyright and must be appropriately attributed.
OCR, layout extraction, and model review are machine-generated and may contain
errors. Canonical text uses a stored review when available while retaining the
local candidate beside it. Treat validation flags and review provenance as part
of the data, not as optional metadata.
"""


def upload_bundle(repo_id: str, bundle: Path, path_in_repo: str, *, private: bool = False) -> dict:
    try:
        from huggingface_hub import HfApi
    except ImportError as error:
        raise RuntimeError("huggingface_hub is not installed") from error
    api = HfApi()
    repo = api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)
    if not api.file_exists(repo_id=repo_id, filename="README.md", repo_type="dataset"):
        api.upload_file(
            path_or_fileobj=dataset_card().encode("utf-8"),
            path_in_repo="README.md",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message="Add Sansad corpus dataset card",
        )
    for attempt in range(11):
        try:
            commit = api.upload_folder(
                folder_path=str(bundle),
                path_in_repo=path_in_repo,
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=f"Publish {path_in_repo}",
            )
            break
        except Exception as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status not in {429, 500, 502, 503, 504} or attempt >= (10 if status == 429 else 4):
                raise
            wait = min(600, 30 * 2**attempt)
            print(f"Hub publication HTTP {status}; retrying in {wait}s")
            time.sleep(wait)
    return {"repo_url": str(repo), "commit_url": str(commit.commit_url)}
