#!/usr/bin/env python3
"""Emit a GitHub Actions matrix of House/Parliament/session scopes.

Scopes already marked complete on Hugging Face are skipped when
--skip-existing is given, which makes a nightly batch an incremental
scheduler: it picks up new sessions and anything left incomplete.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sansad_pipeline.sources.questions import (  # noqa: E402
    available_lok_sabha_sessions,
    available_rajya_sabha_sessions,
)
from scripts.classify_session import skip_reason  # noqa: E402
from scripts.run_summary import is_session_complete, tranche_files_present  # noqa: E402


def scope_key(house: str, parliament: str, session: str) -> str:
    return f"{house}-p{parliament}-s{session}"


def valid_marker(path: str, payload: dict) -> bool:
    """Require current evidence, not merely a historical marker filename."""
    status = payload.get("scope_status") or {}
    inputs = payload.get("inputs") or {}
    key = scope_key(
        str(payload.get("house") or inputs.get("house") or status.get("house") or ""),
        str(payload.get("parliament") or inputs.get("parliament") or status.get("parliament") or ""),
        str(payload.get("session") or inputs.get("session") or status.get("session") or ""),
    )
    if not path.endswith(f"-{key}.json"):
        return False
    if path.startswith("state/skipped/"):
        return skip_reason(status) is not None
    if path.startswith("state/complete/"):
        return bool(payload.get("session_complete") and is_session_complete(
            status, tranche_path=str(payload.get("tranche_path") or ""),
            all_pages=str(inputs.get("all_pages") or "").lower() == "true",
            unlimited=str(inputs.get("limit") or "") == "0",
            adjudication_required=str(inputs.get("max_pages") or "0") != "0",
        ))
    return False


def completed_scopes(repo: str | None) -> set[str]:
    if not repo:
        return set()
    try:
        from huggingface_hub import HfApi, hf_hub_download

        files = HfApi().list_repo_files(repo, repo_type="dataset")
    except Exception as error:
        raise RuntimeError(
            f"cannot inspect Hub markers; refusing to schedule duplicate scopes: {error}"
        ) from error
    available = set(files)
    markers = (
        ("state/complete/session-complete-", ".json"),
        ("state/skipped/session-skipped-", ".json"),
    )
    done = set()
    for path in files:
        for prefix, suffix in markers:
            if path.startswith(prefix) and path.endswith(suffix):
                try:
                    payload = json.loads(Path(hf_hub_download(
                        repo, path, repo_type="dataset"
                    )).read_text(encoding="utf-8"))
                except Exception as error:
                    raise RuntimeError(f"cannot validate Hub marker {path}: {error}") from error
                if valid_marker(path, payload):
                    if path.startswith("state/complete/") and not tranche_files_present(
                        available, str(payload.get("tranche_path") or "")
                    ):
                        print(f"ignoring marker with missing tranche files: {path}", file=sys.stderr)
                    else:
                        done.add(path[len(prefix):-len(suffix)])
                else:
                    print(f"ignoring unproven Hub marker: {path}", file=sys.stderr)
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--house", choices=("all", "lok_sabha", "rajya_sabha"), default="all")
    parser.add_argument("--max", type=int, default=0, help="0 means every scope")
    parser.add_argument("--exclude", action="append", default=[],
                        help="house:parliament:session to exclude; repeatable")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--repo", default="")
    args = parser.parse_args()

    scopes: list[dict] = []
    if args.house in {"all", "lok_sabha"}:
        scopes += [
            {"house": "lok_sabha", "parliament": parliament, "session": session}
            for parliament, session in reversed(available_lok_sabha_sessions())
        ]
    if args.house in {"all", "rajya_sabha"}:
        scopes += [
            {"house": "rajya_sabha", "parliament": "", "session": session}
            for session in available_rajya_sabha_sessions()
        ]

    excluded = {
        part.strip()
        for item in args.exclude
        for part in item.split(",")
        if part.strip()
    }
    scopes = [
        scope for scope in scopes
        if f"{scope['house']}:{scope['parliament']}:{scope['session']}" not in excluded
    ]
    if args.skip_existing:
        done = completed_scopes(args.repo or None)
        scopes = [
            scope for scope in scopes
            if scope_key(scope["house"], scope["parliament"], scope["session"]) not in done
        ]
    if args.max:
        scopes = scopes[: args.max]

    for scope in scopes:
        print(
            f"{scope['house']} parliament={scope['parliament'] or '-'} session={scope['session']}",
            file=sys.stderr,
        )
    print(f"scopes: {len(scopes)}", file=sys.stderr)
    print("matrix=" + json.dumps({"include": scopes}))
    print("count=" + str(len(scopes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
