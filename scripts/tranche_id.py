#!/usr/bin/env python3
"""Stable publication identity for both text and original-PDF inventory."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def snapshot_tranche(bundle: Path, canonical_policy: str) -> str:
    digest = hashlib.sha256()
    for name in ("pages.jsonl.zst", "manifest.jsonl"):
        path = bundle / name
        digest.update(name.encode("ascii") + b"\0")
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return f"snapshot-{canonical_policy}-{digest.hexdigest()[:20]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--canonical-policy", required=True)
    args = parser.parse_args()
    print(snapshot_tranche(args.bundle, args.canonical_policy))


if __name__ == "__main__":
    main()
