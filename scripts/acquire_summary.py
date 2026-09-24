#!/usr/bin/env python3
"""Parse the acquire-census stdout into GitHub Actions step outputs.

The command prints one progress line per record before its final JSON result,
so the JSON is the last object in the stream. Fails loudly if no summary is
present, which means the command crashed before reporting.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/acquire.json")
    text = path.read_text(encoding="utf-8", errors="replace")
    index = text.rfind("\n{")
    blob = text[index + 1:] if index >= 0 else text
    data = json.loads(blob)
    downloaded = int(data.get("downloaded", 0))
    failed = int(data.get("failed", 0))
    print(f"downloaded={downloaded}")
    print(f"failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
