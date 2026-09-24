from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .db import Database, json_text


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class IngestedDocument:
    sha256: str
    raw_path: Path
    size_bytes: int
    already_present: bool


class Store:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.data_root
        self.db = Database(self.root / "pipeline.sqlite3")

    def initialize(self) -> None:
        for name in ("raw", "artifacts", "exports", "tmp"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.db.initialize()

    def ingest(
        self,
        path: Path,
        *,
        source_uri: str | None = None,
        metadata: dict | None = None,
        expected_sha256: str | None = None,
    ) -> IngestedDocument:
        self.initialize()
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError(f"not a PDF: {path}")
        digest = sha256_file(path)
        if expected_sha256 and digest.casefold() != expected_sha256.casefold():
            raise ValueError(f"SHA-256 mismatch for {path}: expected {expected_sha256}, got {digest}")
        target = self.root / "raw" / "sha256" / digest[:2] / f"{digest}.pdf"
        already_present = target.exists()
        if already_present and sha256_file(target) != digest:
            raise IOError(f"stored PDF SHA-256 mismatch: {target}")
        if not already_present:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, staged_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{digest}.", suffix=".partial"
            )
            staged = Path(staged_name)
            try:
                with open(descriptor, "wb") as output, path.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                if sha256_file(staged) != digest:
                    raise IOError(f"copy verification failed: {path}")
                if target.exists():
                    already_present = True
                    if sha256_file(target) != digest:
                        raise IOError(f"stored PDF SHA-256 mismatch: {target}")
                else:
                    staged.replace(target)
                    target.chmod(0o444)
            finally:
                staged.unlink(missing_ok=True)
        acquired = now()
        self.db.execute(
            "INSERT OR IGNORE INTO documents VALUES (?, ?, ?, ?, ?)",
            (digest, target.stat().st_size, "application/pdf", str(target), acquired),
        )
        self.db.execute(
            """INSERT OR IGNORE INTO sources
               (document_sha256, source_uri, original_name, acquired_at, metadata_json)
               VALUES (?, ?, ?, ?, ?)""",
            (digest, source_uri or path.as_uri(), path.name, acquired, json_text(metadata or {})),
        )
        return IngestedDocument(digest, target, target.stat().st_size, already_present)

    def download(
        self,
        url: str,
        *,
        metadata: dict | None = None,
        expected_sha256: str | None = None,
    ) -> IngestedDocument:
        self.initialize()
        filename = Path(urlparse(url).path).name or "download.pdf"
        with tempfile.TemporaryDirectory(dir=self.root / "tmp") as temp_dir:
            downloaded = Path(temp_dir) / filename
            last_error: Exception | None = None
            for attempt in range(1, 6):
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "SansadArchive/0.1", "Accept": "application/pdf,*/*"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=90) as response, downloaded.open("wb") as output:
                        shutil.copyfileobj(response, output, length=1024 * 1024)
                    last_error = None
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as error:
                    last_error = error
                    downloaded.unlink(missing_ok=True)
                    if isinstance(error, urllib.error.HTTPError):
                        if 400 <= error.code < 500 and error.code not in {408, 429}:
                            break
                    if attempt < 5:
                        delay = min(2 ** attempt, 30)
                        if isinstance(error, urllib.error.HTTPError) and error.code == 429:
                            retry_after = error.headers.get("Retry-After")
                            if retry_after and retry_after.isdecimal():
                                delay = max(delay, min(int(retry_after), 60))
                        time.sleep(delay)
            if last_error is not None:
                raise last_error
            return self.ingest(
                downloaded,
                source_uri=url,
                metadata=metadata,
                expected_sha256=expected_sha256,
            )

    def ingest_manifest(self, manifest_path: Path) -> list[IngestedDocument]:
        manifest_path = manifest_path.resolve()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        results = []
        for item in payload.get("samples", payload.get("documents", [])):
            relative = item.get("file")
            source_url = item.get("source_url")
            expected = item.get("sha256")
            if relative and (manifest_path.parent / relative).is_file():
                results.append(
                    self.ingest(
                        manifest_path.parent / relative,
                        source_uri=source_url,
                        metadata=item,
                        expected_sha256=expected,
                    )
                )
            elif source_url:
                results.append(self.download(source_url, metadata=item, expected_sha256=expected))
            else:
                raise ValueError(f"manifest entry has no available file or source_url: {item}")
        return results

    def document(self, identifier: str):
        row = self.db.one("SELECT * FROM documents WHERE sha256 = ?", (identifier,))
        if row:
            return row
        matches = self.db.all("SELECT * FROM documents WHERE sha256 LIKE ?", (f"{identifier}%",))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"ambiguous SHA-256 prefix: {identifier}")
        raise KeyError(f"unknown document: {identifier}")
