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


def scope_key(house: str, parliament: str, session: str) -> str:
    return f"{house}-p{parliament}-s{session}"


def completed_scopes(repo: str | None) -> set[str]:
    if not repo:
        return set()
    try:
        from huggingface_hub import HfApi

        files = HfApi().list_repo_files(repo, repo_type="dataset")
    except Exception as error:  # a missing repo just means nothing is complete
        print(f"skip-existing disabled: {type(error).__name__}: {error}", file=sys.stderr)
        return set()
    prefix, suffix = "state/complete/session-complete-", ".json"
    done = set()
    for path in files:
        if path.startswith(prefix) and path.endswith(suffix):
            done.add(path[len(prefix):-len(suffix)])
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
            for parliament, session in available_lok_sabha_sessions()
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
