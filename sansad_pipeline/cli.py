from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
import time
from pathlib import Path

from .census import Census
from .config import load_config
from .events import EventLog
from .pipeline import Pipeline
from .publication import PublicationBuilder, Scope, upload_bundle
from .openrouter import (
    OpenRouterAdjudicator, OpenRouterCostViolationError,
    OpenRouterHTTPError, OpenRouterRateLimitError,
)
from .nvidia import NvidiaAdjudicator
from .secondary import compare_pdf_inspector
from .storage import Store


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "pipeline.toml"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sansad-pipeline")
    root.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create content-addressed storage and the state database")

    ingest = commands.add_parser("ingest", help="Copy local PDFs into immutable raw storage")
    ingest.add_argument("pdf", nargs="+", type=Path)

    manifest = commands.add_parser("ingest-manifest", help="Ingest local/downloadable manifest entries")
    manifest.add_argument("manifest", type=Path)

    download = commands.add_parser("download", help="Download official PDF URLs into raw storage")
    download.add_argument("url", nargs="+")
    download.add_argument("--expected-sha256")

    process = commands.add_parser("process", help="Process documents by SHA-256 or prefix")
    process.add_argument("document", nargs="+")
    process.add_argument("--force", action="store_true")

    batch = commands.add_parser("batch", help="Process every ingested document resumably")
    batch.add_argument("--force", action="store_true")
    batch.add_argument("--limit", type=int)
    batch.add_argument("--workers", type=int, default=1)

    process_scope = commands.add_parser(
        "process-scope", help="Process one census House/Parliament/session scope resumably"
    )
    process_scope.add_argument("--house", required=True)
    process_scope.add_argument("--parliament", required=True)
    process_scope.add_argument("--session", required=True)
    process_scope.add_argument("--limit", type=int)
    process_scope.add_argument("--workers", type=int, default=1)
    process_scope.add_argument("--force", action="store_true")

    status = commands.add_parser("status", help="Print corpus/run/page status")
    status.add_argument("--json", action="store_true")

    scope_status = commands.add_parser(
        "scope-status", help="Report acquisition, extraction and review state for one scope"
    )
    scope_status.add_argument("--house", required=True)
    scope_status.add_argument("--parliament", required=True)
    scope_status.add_argument("--session", required=True)

    export = commands.add_parser("export-jsonl", help="Export latest successful page records")
    export.add_argument("output", type=Path)
    export.add_argument("--accepted-only", action="store_true")
    export.add_argument("--with-adjudications", action="store_true")

    commands.add_parser("doctor", help="Check required and optional local engines")

    adjudicate = commands.add_parser(
        "adjudicate", help="Send review pages to a configured OpenRouter vision model"
    )
    adjudicate.add_argument("document", help="Document SHA-256 or prefix")
    adjudicate.add_argument("--pages", help="Comma-separated pages; default is review pages")
    adjudicate.add_argument("--model", help="Use only this model; disables configured fallback")
    adjudicate.add_argument(
        "--all-pages", action="store_true", help="Transcribe every page, not only review pages"
    )
    adjudicate.add_argument(
        "--include-ocr",
        action="store_true",
        help="Also transcribe every OCR-routed page, not only flagged pages",
    )
    adjudicate.add_argument("--force", action="store_true", help="Repeat already completed pages")

    openrouter_scope = commands.add_parser(
        "adjudicate-scope-openrouter",
        help="Send pending pages in one census scope to the configured OpenRouter models",
    )
    openrouter_scope.add_argument("--house", required=True)
    openrouter_scope.add_argument("--parliament", required=True)
    openrouter_scope.add_argument("--session", required=True)
    openrouter_scope.add_argument("--limit-pages", type=int)
    openrouter_scope.add_argument("--workers", type=int, default=1)
    openrouter_scope.add_argument(
        "--all-pages", action="store_true", help="Transcribe every page, not only review pages"
    )
    openrouter_scope.add_argument(
        "--include-ocr",
        action="store_true",
        help="Also transcribe every OCR-routed page, not only flagged pages",
    )
    openrouter_scope.add_argument("--force", action="store_true")
    openrouter_scope.add_argument("--summary-out", type=Path)
    openrouter_scope.add_argument("--log", type=Path, help="JSONL event log path")

    nvidia = commands.add_parser(
        "adjudicate-nvidia", help="Send review pages to NVIDIA Nemotron with safe throttling"
    )
    nvidia.add_argument("document", help="Document SHA-256 or prefix")
    nvidia.add_argument("--pages", help="Comma-separated pages; default is review pages")
    nvidia.add_argument(
        "--all-pages", action="store_true", help="Transcribe every page, not only review pages"
    )
    nvidia.add_argument("--force", action="store_true", help="Repeat already completed pages")

    nvidia_scope = commands.add_parser(
        "adjudicate-scope-nvidia",
        help="Review flagged pages in one census scope sequentially and resumably",
    )
    nvidia_scope.add_argument("--house", required=True)
    nvidia_scope.add_argument("--parliament", required=True)
    nvidia_scope.add_argument("--session", required=True)
    nvidia_scope.add_argument("--limit-pages", type=int)
    nvidia_scope.add_argument("--workers", type=int, default=1)
    nvidia_scope.add_argument("--force", action="store_true")

    compare = commands.add_parser(
        "compare-native", help="Compare latest LiteParse output with pdf-inspector"
    )
    compare.add_argument("document", help="Document SHA-256 or prefix")

    check_model = commands.add_parser(
        "check-openrouter-model", help="Verify live image capability and pricing without a paid call"
    )
    check_model.add_argument("model", nargs="+")

    census = commands.add_parser(
        "census-questions", help="Discover official English Lok Sabha question-answer PDFs"
    )
    census.add_argument("--lok-sabha")
    census.add_argument("--session")
    census.add_argument("--all-available", action="store_true")
    census.add_argument("--limit-per-session", type=int, default=10, help="0 means every record")
    census.add_argument("--page-size", type=int, default=100)
    census.add_argument("--sleep", type=float, default=0.25)
    census.add_argument("--workers", type=int, default=4)
    census.add_argument("--retry-failed-run", type=int)

    rs_census = commands.add_parser(
        "census-rs-questions", help="Discover official English Rajya Sabha Q&A PDFs"
    )
    rs_census.add_argument("--session")
    rs_census.add_argument("--all-available", action="store_true")
    rs_census.add_argument("--limit-per-session", type=int, default=10)
    rs_census.add_argument("--workers", type=int, default=4)

    elibrary_census = commands.add_parser(
        "census-elibrary-questions",
        help="Discover the official eLibrary Lok Sabha Q&A collection",
    )
    elibrary_census.add_argument("--limit", type=int, default=1000, help="0 means every item")
    elibrary_census.add_argument("--page-size", type=int, default=100)
    elibrary_census.add_argument("--start-page", type=int, default=0)
    elibrary_census.add_argument("--workers", type=int, default=4)
    elibrary_census.add_argument("--retry-failed-run", type=int)

    acquire_census = commands.add_parser(
        "acquire-census", help="Download pending question PDFs from the census"
    )
    acquire_census.add_argument("--house", choices=("lok_sabha", "rajya_sabha"))
    acquire_census.add_argument("--lok-sabha")
    acquire_census.add_argument("--session")
    acquire_census.add_argument("--limit", type=int, default=10, help="0 means every pending record")
    acquire_census.add_argument("--source", choices=("all", "current", "elibrary"), default="all")
    acquire_census.add_argument("--workers", type=int, default=2)
    acquire_census.add_argument("--retry-failed", action="store_true")
    acquire_census.add_argument("--min-free-gib", type=float, default=10.0)

    commands.add_parser("census-status", help="Summarize census acquisition state")
    estimate_storage = commands.add_parser(
        "estimate-elibrary-storage",
        help="Sample original eLibrary PDF sizes and project raw storage",
    )
    estimate_storage.add_argument("--samples", type=int, default=20)
    estimate_storage.add_argument("--workers", type=int, default=4)
    export_census = commands.add_parser("export-census", help="Export the PDF census as JSONL")
    export_census.add_argument("output", type=Path)

    prepare_publication = commands.add_parser(
        "prepare-publication", help="Build a checksum-verified research publication tranche"
    )
    prepare_publication.add_argument("--house", required=True)
    prepare_publication.add_argument("--parliament", required=True)
    prepare_publication.add_argument("--session", required=True)
    prepare_publication.add_argument("--source-type", default="questions_answers")
    prepare_publication.add_argument("--output", required=True, type=Path)
    prepare_publication.add_argument("--limit", type=int)
    prepare_publication.add_argument("--no-raw", action="store_true")
    prepare_publication.add_argument("--compact", action="store_true",
                                     help="Keep originals and text together in the WebDataset archive without per-document files")
    prepare_publication.add_argument("--complete-session", action="store_true",
                                     help="Claim completeness after the workflow has verified the full scope")
    prepare_publication.add_argument("--minimum-pdf-saving-percent", type=float, default=5.0)
    prepare_publication.add_argument(
        "--canonical-policy",
        choices=("local", "model"),
        default="local",
        help="Which layer becomes canonical text; the other layer is always retained",
    )

    verify_publication = commands.add_parser(
        "verify-publication", help="Verify every file against a publication SHA256SUMS"
    )
    verify_publication.add_argument("bundle", type=Path)

    publish_hf = commands.add_parser(
        "publish-hf", help="Upload a prepared publication tranche to Hugging Face"
    )
    publish_hf.add_argument("bundle", type=Path)
    publish_hf.add_argument("--repo", required=True)
    publish_hf.add_argument("--path-in-repo", required=True)
    publish_hf.add_argument("--private", action="store_true")
    return root


def status_payload(store: Store) -> dict:
    queries = {
        "documents": "SELECT COUNT(*) AS n FROM documents",
        "sources": "SELECT COUNT(*) AS n FROM sources",
        "runs_complete": "SELECT COUNT(*) AS n FROM runs WHERE status='complete'",
        "runs_failed": "SELECT COUNT(*) AS n FROM runs WHERE status='failed'",
        "pages": "SELECT COUNT(*) AS n FROM pages",
        "pages_native": "SELECT COUNT(*) AS n FROM pages WHERE route='native'",
        "pages_ocr": "SELECT COUNT(*) AS n FROM pages WHERE route='ocr'",
        "pages_review": "SELECT COUNT(*) AS n FROM pages WHERE validation_status='review'",
        "adjudications": "SELECT COUNT(*) AS n FROM adjudications",
        "nvidia_adjudications": "SELECT COUNT(*) AS n FROM adjudications WHERE provider='nvidia'",
        "openrouter_adjudications": "SELECT COUNT(*) AS n FROM adjudications WHERE provider='openrouter'",
    }
    payload = {name: int(store.db.one(sql)["n"]) for name, sql in queries.items()}
    size = store.db.one("SELECT COALESCE(SUM(size_bytes), 0) AS n FROM documents")
    payload["raw_bytes"] = int(size["n"])
    cost = store.db.one("SELECT COALESCE(SUM(reported_cost), 0) AS n FROM adjudications")
    payload["openrouter_reported_cost"] = float(cost["n"])
    return payload


def doctor() -> int:
    required = {
        "python": sys.version.split()[0],
        "liteparse": importlib.metadata.version("liteparse"),
    }
    optional = {
        "tesseract": shutil.which("tesseract"),
        "ocrmypdf": shutil.which("ocrmypdf"),
        "cargo": shutil.which("cargo"),
    }
    try:
        import pdf_inspector  # type: ignore
        optional["pdf_inspector"] = getattr(pdf_inspector, "__version__", "installed")
    except ImportError:
        optional["pdf_inspector"] = None
    print(json.dumps({"required": required, "optional": optional}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_config(args.config)
    store = Store(config)
    store.initialize()

    if args.command == "init":
        print(config.data_root)
        return 0
    if args.command == "ingest":
        for pdf in args.pdf:
            item = store.ingest(pdf)
            print(f"{item.sha256}\t{item.size_bytes}\t{item.raw_path}")
        return 0
    if args.command == "ingest-manifest":
        for item in store.ingest_manifest(args.manifest):
            print(f"{item.sha256}\t{item.size_bytes}\t{item.raw_path}")
        return 0
    if args.command == "download":
        if args.expected_sha256 and len(args.url) != 1:
            raise SystemExit("--expected-sha256 can only be used with one URL")
        for url in args.url:
            item = store.download(url, expected_sha256=args.expected_sha256)
            print(f"{item.sha256}\t{item.size_bytes}\t{item.raw_path}")
        return 0
    if args.command in {"process", "batch", "process-scope"}:
        pipeline = Pipeline(config)
        if args.command == "process":
            identifiers = args.document
        elif args.command == "batch":
            identifiers = [
                row["sha256"]
                for row in store.db.all("SELECT sha256 FROM documents ORDER BY created_at")
            ][: args.limit]
        else:
            params: list[object] = [args.house, args.parliament, args.session]
            limit_sql = ""
            if args.limit is not None:
                if args.limit < 1:
                    raise SystemExit("--limit must be positive")
                limit_sql = " LIMIT ?"
                params.append(args.limit)
            identifiers = [
                row["document_sha256"]
                for row in store.db.all(
                    """SELECT DISTINCT document_sha256 FROM census_records
                       WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                         AND document_sha256 IS NOT NULL
                       ORDER BY document_sha256""" + limit_sql,
                    tuple(params),
                )
            ]
        failures = 0

        def process_one(identifier: str):
            try:
                run_id = pipeline.process(identifier, force=args.force)
                return identifier, run_id, None
            except Exception as error:
                return identifier, None, error

        workers = args.workers if args.command in {"batch", "process-scope"} else 1
        if workers < 1:
            raise SystemExit("--workers must be positive")
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_one, identifier): identifier for identifier in identifiers}
            completed = 0
            for future in as_completed(futures):
                identifier, run_id, error = future.result()
                completed += 1
                if error is None:
                    print(
                        f"[{completed}/{len(identifiers)}] {identifier[:12]} run={run_id}",
                        flush=True,
                    )
                else:
                    failures += 1
                    print(
                        f"[{completed}/{len(identifiers)}] {identifier[:12]} ERROR {error}",
                        file=sys.stderr,
                    )
        return 1 if failures else 0
    if args.command == "status":
        payload = status_payload(store)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for name, value in payload.items():
                print(f"{name:20} {value}")
        return 0
    if args.command == "scope-status":
        params = (args.house, args.parliament, args.session)
        census_row = store.db.one(
            """SELECT status FROM census_scopes
               WHERE house=? AND parliament_number=? AND session=?
               ORDER BY run_id DESC LIMIT 1""",
            params,
        )
        acquisition = {
            row["acquisition_status"]: int(row["n"])
            for row in store.db.all(
                """SELECT acquisition_status,COUNT(*) n FROM census_records
                   WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                   GROUP BY acquisition_status""",
                params,
            )
        }
        unsupported_html = store.db.one(
            """SELECT COUNT(*) n FROM census_records
               WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                 AND acquisition_status='failed'
                 AND LOWER(source_url) LIKE '%.htm%'
                 AND last_error LIKE '%not a PDF:%'""",
            params,
        )
        metrics = store.db.one(
            """WITH scoped AS (
                   SELECT DISTINCT document_sha256 FROM census_records
                    WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                      AND document_sha256 IS NOT NULL
               ), latest AS (
                   SELECT s.document_sha256,
                          (SELECT MAX(r.id) FROM runs r
                            WHERE r.document_sha256=s.document_sha256
                              AND r.status='complete') run_id
                     FROM scoped s
               )
               SELECT COUNT(*) acquired_documents,
                      COALESCE(SUM(d.size_bytes),0) raw_bytes,
                      SUM(CASE WHEN l.run_id IS NOT NULL THEN 1 ELSE 0 END) processed_documents,
                      (SELECT COUNT(*) FROM pages p JOIN latest x ON x.run_id=p.run_id) pages,
                      (SELECT COUNT(*) FROM pages p JOIN latest x ON x.run_id=p.run_id
                        WHERE p.route='ocr') ocr_pages,
                      (SELECT COUNT(*) FROM pages p JOIN latest x ON x.run_id=p.run_id
                        WHERE p.validation_status='review') review_pages,
                      (SELECT COUNT(*) FROM pages p JOIN latest x ON x.run_id=p.run_id
                        WHERE EXISTS (SELECT 1 FROM adjudications a
                                      WHERE a.run_id=p.run_id AND a.page_number=p.page_number
                                        AND a.provider='openrouter')) openrouter_adjudicated_pages,
                      (SELECT COUNT(*) FROM adjudications a JOIN latest x ON x.run_id=a.run_id
                        WHERE a.provider='nvidia') nvidia_adjudications
                 FROM latest l JOIN documents d ON d.sha256=l.document_sha256""",
            params,
        )
        payload = {
            "scope": {"house": args.house, "parliament": args.parliament, "session": args.session},
            "records": sum(acquisition.values()),
            "census_status": census_row["status"] if census_row else None,
            "acquisition": acquisition,
            "unsupported_html_records": int(unsupported_html["n"]),
            **{key: int(metrics[key] or 0) for key in metrics.keys()},
        }
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "export-jsonl":
        count = Pipeline(config).export_jsonl(
            args.output,
            accepted_only=args.accepted_only,
            with_adjudications=args.with_adjudications,
        )
        print(f"wrote {count} pages to {args.output}")
        return 0
    if args.command == "doctor":
        return doctor()
    if args.command == "adjudicate":
        pages = [int(value) for value in args.pages.split(",")] if args.pages else None
        try:
            summary = OpenRouterAdjudicator(config).adjudicate_with_fallback(
                args.document,
                pages=pages,
                model=args.model,
                all_pages=args.all_pages,
                include_ocr=args.include_ocr,
                force=args.force,
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        print(json.dumps(summary, indent=2))
        return 1 if summary["failed"] else 0
    if args.command == "adjudicate-nvidia":
        pages = [int(value) for value in args.pages.split(",")] if args.pages else None
        try:
            summary = NvidiaAdjudicator(config).adjudicate(
                args.document, pages=pages, all_pages=args.all_pages, force=args.force
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        print(json.dumps(summary, indent=2))
        return 1 if summary["failed"] else 0
    if args.command == "adjudicate-scope-nvidia":
        if args.limit_pages is not None and args.limit_pages < 1:
            raise SystemExit("--limit-pages must be positive")
        if not 1 <= args.workers <= config.nvidia.max_concurrency:
            raise SystemExit(
                f"--workers must be 1-{config.nvidia.max_concurrency}; "
                "the measured production ceiling is global across simultaneous commands"
            )
        rows = store.db.all(
            """SELECT DISTINCT c.document_sha256
                 FROM census_records c
                 JOIN runs r ON r.id=(SELECT MAX(r2.id) FROM runs r2
                                      WHERE r2.document_sha256=c.document_sha256
                                        AND r2.status='complete')
                WHERE c.house=? AND COALESCE(c.parliament_number,'')=? AND c.session=?
                ORDER BY c.document_sha256""",
            (args.house, args.parliament, args.session),
        )
        reviewer = NvidiaAdjudicator(config)
        tasks = []
        for row in rows:
            if args.limit_pages is not None and len(tasks) >= args.limit_pages:
                break
            _, run = reviewer._latest_run(row["document_sha256"])
            page_rows = reviewer._page_rows(int(run["id"]), None, all_pages=True)
            if not args.force:
                page_rows = [
                    page for page in page_rows
                    if not store.db.one(
                        """SELECT id FROM adjudications
                           WHERE run_id=? AND page_number=? AND provider='nvidia'
                             AND model=? ORDER BY id DESC LIMIT 1""",
                        (run["id"], page["page_number"], config.nvidia.model),
                    )
                ]
            if args.limit_pages is not None:
                page_rows = page_rows[: args.limit_pages - len(tasks)]
            tasks.extend(
                (row["document_sha256"], int(page["page_number"])) for page in page_rows
            )

        import threading
        worker_state = threading.local()

        def transcribe_page(task):
            document_sha, page_number = task
            if not hasattr(worker_state, "reviewer"):
                worker_state.reviewer = NvidiaAdjudicator(config)
            result = worker_state.reviewer.adjudicate(
                document_sha, pages=[page_number], all_pages=True, force=args.force
            )
            return document_sha, result

        completed = skipped = failed = 0
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(transcribe_page, task) for task in tasks]
            for future in as_completed(futures):
                document_sha, result = future.result()
                completed += len(result["completed"])
                skipped += len(result["skipped"])
                failed += len(result["failed"])
                print(json.dumps({"document": document_sha, **result}), flush=True)
        print(json.dumps({
            "scope": {"house": args.house, "parliament": args.parliament, "session": args.session},
            "workers": args.workers,
            "pages_considered": len(tasks),
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
        }, indent=2))
        return 1 if failed else 0
    if args.command == "adjudicate-scope-openrouter":
        if args.limit_pages is not None and args.limit_pages < 1:
            raise SystemExit("--limit-pages must be positive")
        if not 1 <= args.workers <= config.openrouter.max_concurrency:
            raise SystemExit(
                f"--workers must be 1-{config.openrouter.max_concurrency}; "
                "provider rate limits are global across simultaneous commands"
            )
        reviewer = OpenRouterAdjudicator(config)
        models = reviewer.configured_models()
        if not models:
            raise SystemExit("no OpenRouter models configured")
        rows = store.db.all(
            """SELECT DISTINCT c.document_sha256
                 FROM census_records c
                 JOIN runs r ON r.id=(SELECT MAX(r2.id) FROM runs r2
                                      WHERE r2.document_sha256=c.document_sha256
                                        AND r2.status='complete')
                WHERE c.house=? AND COALESCE(c.parliament_number,'')=? AND c.session=?
                ORDER BY c.document_sha256""",
            (args.house, args.parliament, args.session),
        )
        placeholders = ",".join("?" for _ in models)
        tasks: list[tuple[str, int]] = []
        for row in rows:
            if args.limit_pages is not None and len(tasks) >= args.limit_pages:
                break
            _, run = reviewer._latest_run(row["document_sha256"])
            page_rows = reviewer._page_rows(
                int(run["id"]), None,
                all_pages=args.all_pages,
                include_ocr=args.include_ocr,
            )
            if not args.force:
                page_rows = [
                    page for page in page_rows
                    if not store.db.one(
                        f"""SELECT id FROM adjudications
                            WHERE run_id=? AND page_number=? AND provider='openrouter'
                              AND model IN ({placeholders}) ORDER BY id DESC LIMIT 1""",
                        (run["id"], page["page_number"], *models),
                    )
                ]
            if args.limit_pages is not None:
                page_rows = page_rows[: args.limit_pages - len(tasks)]
            tasks.extend(
                (row["document_sha256"], int(page["page_number"])) for page in page_rows
            )

        import threading
        worker_state = threading.local()

        def transcribe_page(task):
            document_sha, page_number = task
            if not hasattr(worker_state, "reviewer"):
                worker_state.reviewer = OpenRouterAdjudicator(config)
            started = time.monotonic()
            try:
                worker_state.reviewer.adjudicate(
                    document_sha,
                    pages=[page_number],
                    all_pages=args.all_pages,
                    include_ocr=args.include_ocr,
                )
                return document_sha, page_number, None, time.monotonic() - started
            except Exception as error:  # classified by the caller
                return document_sha, page_number, error, time.monotonic() - started

        log_path = args.log
        if log_path is None:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            log_path = config.data_root / "logs" / f"adjudicate-openrouter-{stamp}.jsonl"
        events = EventLog(log_path)
        events.emit(
            "scope_start",
            provider="openrouter",
            models=",".join(models),
            house=args.house,
            parliament=args.parliament,
            session=args.session,
            all_pages=args.all_pages,
            include_ocr=args.include_ocr,
            workers=args.workers,
            limit_pages=args.limit_pages,
            pages_pending=len(tasks),
            log=str(log_path),
        )
        completed = failed = 0
        rate_limited = fatal_error = False
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(transcribe_page, task) for task in tasks]
            for future in as_completed(futures):
                document_sha, page_number, error, seconds = future.result()
                if error is None:
                    completed += 1
                    events.emit(
                        "page_completed",
                        document=document_sha[:12],
                        page=page_number,
                        seconds=round(seconds, 1),
                        progress=f"{completed + failed}/{len(tasks)}",
                    )
                    continue
                if isinstance(error, OpenRouterRateLimitError) or (
                    isinstance(error, OpenRouterHTTPError) and error.status == 429
                ):
                    rate_limited = True
                    events.emit("rate_limited", detail=str(error))
                    for pending in futures:
                        pending.cancel()
                    break
                if isinstance(error, OpenRouterCostViolationError):
                    fatal_error = True
                    events.emit("cost_violation", detail=str(error))
                    for pending in futures:
                        pending.cancel()
                    break
                failed += 1
                events.emit(
                    "page_failed",
                    document=document_sha[:12],
                    page=page_number,
                    seconds=round(seconds, 1),
                    error=str(error),
                    progress=f"{completed + failed}/{len(tasks)}",
                )
        summary = {
            "provider": "openrouter",
            "models": models,
            "scope": {"house": args.house, "parliament": args.parliament, "session": args.session},
            "all_pages": args.all_pages,
            "include_ocr": args.include_ocr,
            "workers": args.workers,
            "limit_pages": args.limit_pages,
            "pages_considered": len(tasks),
            "completed": completed,
            "failed": failed,
            "rate_limited": rate_limited,
            "fatal_error": fatal_error,
            "log": str(log_path),
        }
        events.emit("scope_summary", **summary)
        if args.summary_out:
            args.summary_out.parent.mkdir(parents=True, exist_ok=True)
            args.summary_out.write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
        print(json.dumps(summary, indent=2))
        return 1 if failed or rate_limited or fatal_error else 0
    if args.command == "compare-native":
        path = compare_pdf_inspector(config, args.document)
        print(path)
        return 0
    if args.command == "check-openrouter-model":
        adjudicator = OpenRouterAdjudicator(config)
        failed = False
        for model in args.model:
            try:
                print(json.dumps(adjudicator._model_snapshot(model), indent=2))
            except RuntimeError as error:
                failed = True
                print(f"{model}: REJECTED: {error}", file=sys.stderr)
        return 1 if failed else 0
    if args.command == "census-questions":
        if args.limit_per_session < 0 or args.page_size < 1 or args.sleep < 0 or args.workers < 1:
            raise SystemExit("limits/page size/sleep must be non-negative (page size at least 1)")
        result = Census(config).discover_lok_sabha(
            lok_sabha=args.lok_sabha,
            session=args.session,
            all_available=args.all_available,
            limit=args.limit_per_session,
            page_size=args.page_size,
            sleep_seconds=args.sleep,
            workers=args.workers,
            retry_failed_run=args.retry_failed_run,
        )
        print(json.dumps(result, indent=2))
        return 1 if result["failed_scopes"] else 0
    if args.command == "census-elibrary-questions":
        if (
            args.limit < 0
            or not 1 <= args.page_size <= 100
            or args.start_page < 0
            or args.workers < 1
        ):
            raise SystemExit(
                "limit/start page must be non-negative; eLibrary page size must be 1-100"
            )
        result = Census(config).discover_elibrary_lok_sabha(
            limit=args.limit,
            page_size=args.page_size,
            start_page=args.start_page,
            workers=args.workers,
            retry_failed_run=args.retry_failed_run,
        )
        print(json.dumps(result, indent=2))
        return 1 if result["failed_pages"] else 0
    if args.command == "census-rs-questions":
        if args.limit_per_session < 0 or args.workers < 1:
            raise SystemExit("limit must be non-negative; workers must be positive")
        result = Census(config).discover_rajya_sabha(
            session=args.session,
            all_available=args.all_available,
            limit=args.limit_per_session,
            workers=args.workers,
        )
        print(json.dumps(result, indent=2))
        return 1 if result["failed_scopes"] else 0
    if args.command == "acquire-census":
        if args.limit < 0 or args.workers < 1 or args.min_free_gib < 0:
            raise SystemExit("limit/minimum free space must be non-negative; workers must be positive")
        result = Census(config).acquire_questions(
            limit=args.limit,
            house=args.house,
            lok_sabha=args.lok_sabha,
            session=args.session,
            source=args.source,
            workers=args.workers,
            retry_failed=args.retry_failed,
            min_free_gib=args.min_free_gib,
        )
        print(json.dumps(result, indent=2))
        return 1 if result["failed"] or result["stopped_low_disk"] else 0
    if args.command == "census-status":
        print(json.dumps(Census(config).status(), indent=2))
        return 0
    if args.command == "estimate-elibrary-storage":
        if args.samples < 1 or args.workers < 1:
            raise SystemExit("samples and workers must be positive")
        result = Census(config).estimate_elibrary_storage(
            samples=args.samples, workers=args.workers
        )
        print(json.dumps(result, indent=2))
        return 1 if result["failed_samples"] else 0
    if args.command == "export-census":
        count = Census(config).export_jsonl(args.output)
        print(f"wrote {count} records to {args.output}")
        return 0
    if args.command == "prepare-publication":
        if args.limit is not None and args.limit < 1:
            raise SystemExit("--limit must be positive")
        if args.minimum_pdf_saving_percent < 0:
            raise SystemExit("--minimum-pdf-saving-percent must be non-negative")
        scope = Scope(
            house=args.house,
            parliament_number=args.parliament,
            session=args.session,
            source_type=args.source_type,
        )
        result = PublicationBuilder(config).build(
            scope,
            args.output,
            limit=args.limit,
            include_raw=not args.no_raw,
            minimum_pdf_saving_percent=args.minimum_pdf_saving_percent,
            canonical_policy=args.canonical_policy,
            compact=args.compact,
            complete_session=args.complete_session,
        )
        verification = PublicationBuilder.verify(args.output)
        print(json.dumps({"publication": result, "verification": verification}, indent=2))
        return 0 if verification["valid"] else 1
    if args.command == "verify-publication":
        result = PublicationBuilder.verify(args.bundle)
        print(json.dumps(result, indent=2))
        return 0 if result["valid"] else 1
    if args.command == "publish-hf":
        verification = PublicationBuilder.verify(args.bundle)
        if not verification["valid"]:
            raise SystemExit("publication checksum verification failed; refusing upload")
        result = upload_bundle(
            args.repo,
            args.bundle,
            args.path_in_repo,
            private=args.private,
        )
        print(json.dumps({"verification": verification, "upload": result}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
