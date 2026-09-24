from __future__ import annotations

import math
import re
from typing import Any, Iterator

from .questions import QuestionRecord, request_json


ELIBRARY_BASE = "https://elibrary.sansad.in"
ELIBRARY_API = f"{ELIBRARY_BASE}/server/api"
LS_QUESTIONS_COLLECTION = "75228d43-3a98-4b7d-b90f-ffec6aa9fa11"
LS_QUESTIONS_PAGE = f"{ELIBRARY_BASE}/collections/{LS_QUESTIONS_COLLECTION}"
MAX_PAGE_SIZE = 100


def normalize_session_label(value: str, members: list[str]) -> tuple[str, dict[str, str] | None]:
    """Repair a member-name prefix accidentally joined to a session label.

    Do not infer from dates or fuzzy names. Preserve the source value and rule
    in the record's raw provenance whenever a repair is made.
    """
    if re.fullmatch(r"(?:[IVXLCDM]+|[0-9]+)", value, re.IGNORECASE):
        return value, None
    candidates = {
        value[len(member):] for member in members
        if member and value.startswith(member)
        and re.fullmatch(r"(?:[IVXLCDM]+|[0-9]+)", value[len(member):], re.IGNORECASE)
    }
    if len(candidates) == 1:
        normalized = candidates.pop()
        return normalized, {
            "original": value, "normalized": normalized,
            "rule": "exact-listed-member-prefix",
        }
    return value, None


def _values(metadata: dict[str, Any], key: str) -> list[str]:
    return [
        str(entry.get("value") or "").strip()
        for entry in metadata.get(key, [])
        if str(entry.get("value") or "").strip()
    ]


def _value(metadata: dict[str, Any], key: str) -> str:
    values = _values(metadata, key)
    return values[0] if values else ""


def search_page(*, page: int = 0, page_size: int = 100) -> dict[str, Any]:
    if page < 0 or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"eLibrary page_size must be between 1 and {MAX_PAGE_SIZE}")
    response = request_json(
        f"{ELIBRARY_API}/discover/search/objects",
        params={
            "scope": LS_QUESTIONS_COLLECTION,
            "dsoType": "ITEM",
            "page": page,
            "size": page_size,
        },
    )
    result = response.get("_embedded", {}).get("searchResult", {})
    page_info = result.get("page") or {}
    try:
        actual_page = int(page_info["number"])
        actual_size = int(page_info["size"])
        total = int(page_info["totalElements"])
        total_pages = int(page_info["totalPages"])
        objects = result["_embedded"]["objects"]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"eLibrary search page {page} has incomplete pagination metadata") from error
    expected = max(0, min(page_size, total - page * page_size))
    if (actual_page != page or actual_size != page_size or total < 0
            or total_pages != math.ceil(total / page_size)
            or not isinstance(objects, list) or len(objects) != expected):
        raise RuntimeError(
            f"eLibrary search page {page} is incomplete or inconsistent: "
            f"number={actual_page} size={actual_size} total={total} "
            f"totalPages={total_pages} objects={len(objects) if isinstance(objects, list) else 'invalid'} "
            f"expected={expected}"
        )
    return response


def lok_sabha_question_count() -> int:
    response = search_page(page=0, page_size=1)
    result = response.get("_embedded", {}).get("searchResult", {})
    return int(result.get("page", {}).get("totalElements") or 0)


def records_from_search_response(
    response: dict[str, Any], *, page: int
) -> list[QuestionRecord]:
    result = response.get("_embedded", {}).get("searchResult", {})
    objects = result.get("_embedded", {}).get("objects", [])
    records: list[QuestionRecord] = []
    seen: set[str] = set()
    for hit in objects:
        item = hit.get("_embedded", {}).get("indexableObject", {})
        item_id = str(item.get("uuid") or item.get("id") or "").strip()
        if not item_id:
            raise RuntimeError(f"eLibrary search page {page} contains an item without an ID")
        if item_id in seen:
            raise RuntimeError(f"eLibrary search page {page} repeats item {item_id}")
        seen.add(item_id)
        metadata = item.get("metadata") or {}
        handle = str(item.get("handle") or "").strip()
        source_url = _value(metadata, "dc.identifier.uri")
        if not source_url and handle:
            source_url = f"{ELIBRARY_BASE}/handle/{handle}"
        if not source_url:
            source_url = f"{ELIBRARY_API}/core/items/{item_id}"
        # Keeping the entire HAL item for 1.15M rows would add roughly 4 GiB
        # of duplicated metadata to SQLite. Normalized fields live in the
        # census columns; retain only stable resolver/provenance identifiers.
        raw = {
            "_source_system": "sansad_elibrary_dspace",
            "uuid": item_id,
            "handle": handle,
            "dc.language.iso": _values(metadata, "dc.language.iso"),
            "dc.type": _values(metadata, "dc.type"),
        }
        members = _values(metadata, "dc.contributor.members")
        original_session = _value(metadata, "dc.identifier.sessionnumber")
        session, repair = normalize_session_label(original_session, members)
        if repair:
            raw["session_normalization"] = repair
        records.append(
            QuestionRecord(
                record_id=f"elibrary_ls_question_{item_id}",
                source_type="questions_answers",
                house="lok_sabha",
                parliament_number=_value(metadata, "dc.identifier.loksabhanumber"),
                session=session,
                document_number=_value(metadata, "dc.identifier.questionnumber"),
                document_subtype=_value(metadata, "dc.identifier.questiontype").upper(),
                document_date=_value(metadata, "dc.date.issued"),
                title=_value(metadata, "dc.title") or str(item.get("name") or "").strip(),
                ministry=_value(metadata, "dc.relation.ministry"),
                members=members,
                language="en",
                source_url=source_url,
                official_page_url=LS_QUESTIONS_PAGE,
                api_url=f"{ELIBRARY_API}/core/items/{item_id}",
                api_params={"collection": LS_QUESTIONS_COLLECTION, "page": page},
                raw=raw,
            )
        )
    return records


def discover_lok_sabha_questions(
    *,
    limit: int = 0,
    page_size: int = 100,
    start_page: int = 0,
) -> Iterator[QuestionRecord]:
    yielded = 0
    page = start_page
    while True:
        response = search_page(page=page, page_size=page_size)
        result = response.get("_embedded", {}).get("searchResult", {})
        objects = result.get("_embedded", {}).get("objects", [])
        page_info = result.get("page", {})
        if not objects:
            return
        for record in records_from_search_response(response, page=page):
            yield record
            yielded += 1
            if limit and yielded >= limit:
                return
        total_pages = int(page_info.get("totalPages") or 0)
        page += 1
        if total_pages and page >= total_pages:
            return


def resolve_original_pdf(item_id: str) -> tuple[str, dict[str, Any]]:
    bundles = request_json(
        f"{ELIBRARY_API}/core/items/{item_id}/bundles", params={"size": 100}
    ).get("_embedded", {}).get("bundles", [])
    original = next((bundle for bundle in bundles if bundle.get("name") == "ORIGINAL"), None)
    if original is None:
        raise RuntimeError(f"eLibrary item {item_id} has no ORIGINAL bundle")
    bitstreams_url = original.get("_links", {}).get("bitstreams", {}).get("href")
    if not bitstreams_url:
        raise RuntimeError(f"eLibrary item {item_id} has no ORIGINAL bitstream link")
    bitstreams = request_json(bitstreams_url, params={"size": 100}).get("_embedded", {}).get(
        "bitstreams", []
    )
    pdf = next(
        (
            bitstream
            for bitstream in bitstreams
            if str(bitstream.get("name") or "").casefold().endswith(".pdf")
        ),
        None,
    )
    if pdf is None:
        raise RuntimeError(f"eLibrary item {item_id} has no PDF in ORIGINAL bundle")
    content_url = pdf.get("_links", {}).get("content", {}).get("href")
    if not content_url:
        raise RuntimeError(f"eLibrary item {item_id} PDF has no content link")
    return str(content_url), pdf
