from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterator


LS_API = "https://sansad.in/api_ls"
LS_PAGE = "https://sansad.in/ls/questions/questions-and-answers"
RS_API = "https://sansad.in/api_rs"
RS_DOCUMENT_API = "https://rsdoc.nic.in/Question/Search_Questions"
RS_PAGE = "https://sansad.in/rs/questions/questions-and-answers"
USER_AGENT = "SansadArchive/0.1 (+public-research-corpus)"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 5,
    timeout: int = 90,
) -> Any:
    query = urllib.parse.urlencode(params or {})
    target = f"{url}?{query}" if query else url
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            target,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException, json.JSONDecodeError) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 30))
    raise last_error or RuntimeError(f"failed to request {target}")


def parse_date(value: str) -> str:
    parts = (value or "").strip().split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        day, month, year = parts
        return f"{year.zfill(4)}-{month.zfill(2)}-{day.zfill(2)}"
    return ""


@dataclass(frozen=True)
class QuestionRecord:
    record_id: str
    source_type: str
    house: str
    parliament_number: str
    session: str
    document_number: str
    document_subtype: str
    document_date: str
    title: str
    ministry: str
    members: list[str]
    language: str
    source_url: str
    official_page_url: str
    api_url: str
    api_params: dict[str, Any]
    raw: dict[str, Any]

    def metadata(self) -> dict[str, Any]:
        return asdict(self)


def available_lok_sabha_sessions() -> list[tuple[str, str]]:
    rows = request_json(f"{LS_API}/business/getAllLoksabhaAndSession", params={"locale": "en"})
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("official Lok Sabha session inventory is empty or malformed")
    sessions = {
        (str(house["loksabha"]), str(session["sessionNo"]))
        for house in rows
        for session in house.get("sessions", [])
    }
    if not sessions:
        raise RuntimeError("official Lok Sabha session inventory has no sessions")
    return sorted(sessions, key=lambda item: (int(item[0]), int(item[1])))


def latest_lok_sabha_session() -> tuple[str, str]:
    sessions = available_lok_sabha_sessions()
    if not sessions:
        raise RuntimeError("official API returned no Lok Sabha sessions")
    return sessions[-1]


def available_rajya_sabha_sessions() -> list[str]:
    rows = request_json(f"{RS_API}/business/getSessionsList", params={"docType": "SQ"})
    return [str(row["session"]) for row in rows if row.get("session") is not None]


def latest_rajya_sabha_session() -> str:
    sessions = available_rajya_sabha_sessions()
    if not sessions:
        raise RuntimeError("official API returned no Rajya Sabha sessions")
    return sessions[0]


def discover_rajya_sabha_questions(session: str, *, limit: int = 0) -> Iterator[QuestionRecord]:
    params = {"whereclause": f"ses_no={session}"}
    rows = request_json(RS_DOCUMENT_API, params=params)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_url = str(row.get("files") or "").strip()
        if not source_url:
            continue
        qslno = str(row.get("qslno") or "").strip()
        key = qslno or source_url
        group = grouped.setdefault(key, {"row": row, "members": []})
        member = " ".join(
            part for part in (str(row.get("shri") or "").strip(), str(row.get("name") or "").strip())
            if part
        )
        if member and member not in group["members"]:
            group["members"].append(member)
    for index, (key, group) in enumerate(grouped.items(), 1):
        row = group["row"]
        qno_value = row.get("qno")
        qno = str(int(qno_value)) if isinstance(qno_value, float) and qno_value.is_integer() else str(qno_value or "")
        subtype = str(row.get("qtype") or "unknown").strip().upper()
        raw = dict(row)
        raw["_all_members"] = group["members"]
        yield QuestionRecord(
            record_id=f"rs_qsl{key}_en",
            source_type="questions_answers",
            house="rajya_sabha",
            parliament_number="",
            session=str(row.get("ses_no") or session),
            document_number=qno,
            document_subtype=subtype,
            document_date=parse_date(str(row.get("ans_date") or "")),
            title=str(row.get("qtitle") or "").strip(),
            ministry=str(row.get("min_name") or "").strip(),
            members=list(group["members"]),
            language="en",
            source_url=str(row.get("files") or "").strip(),
            official_page_url=RS_PAGE,
            api_url=RS_DOCUMENT_API,
            api_params=params,
            raw=raw,
        )
        if limit and index >= limit:
            return


def discover_lok_sabha_questions(
    lok_sabha: str,
    session: str,
    *,
    limit: int = 0,
    page_size: int = 100,
    sleep_seconds: float = 0.25,
) -> Iterator[QuestionRecord]:
    api_url = f"{LS_API}/question/qetFilteredQuestionsAns"
    page_number = 1
    yielded = 0
    rows_seen = 0
    expected_total: int | None = None
    record_ids: set[str] = set()
    while True:
        params = {
            "loksabhaNo": lok_sabha,
            "sessionNumber": session,
            "pageNo": page_number,
            "locale": "en",
            "pageSize": page_size,
        }
        response = request_json(api_url, params=params)
        if not isinstance(response, list) or not response or not isinstance(response[0], dict):
            raise RuntimeError(f"Lok Sabha questions page {page_number} has an invalid response")
        payload = response[0]
        if "totalRecordSize" not in payload or not isinstance(payload.get("listOfQuestions"), list):
            raise RuntimeError(f"Lok Sabha questions page {page_number} lacks pagination fields")
        rows = payload["listOfQuestions"]
        total = int(payload["totalRecordSize"])
        if total < 0 or (expected_total is not None and total != expected_total):
            raise RuntimeError("Lok Sabha question count changed during pagination")
        expected_total = total
        if not rows:
            if rows_seen < total:
                raise RuntimeError(f"Lok Sabha questions ended after {rows_seen} of {total} rows")
            break
        for row in rows:
            source_url = str(row.get("questionsFilePath") or "").strip()
            number = str(row.get("quesNo") or "").strip()
            if not number:
                raise RuntimeError(f"Lok Sabha questions page {page_number} has a record without quesNo")
            subtype = str(row.get("type") or "unknown").strip().upper()
            record_id = f"ls_l{lok_sabha}_s{session}_q{number}_{subtype.casefold()}_en"
            if record_id in record_ids:
                raise RuntimeError(f"duplicate Lok Sabha question in paginated census: {record_id}")
            record_ids.add(record_id)
            yield QuestionRecord(
                record_id=record_id,
                source_type="questions_answers",
                house="lok_sabha",
                parliament_number=lok_sabha,
                session=str(row.get("sessionNo") or session),
                document_number=number,
                document_subtype=subtype,
                document_date=parse_date(str(row.get("date") or "")),
                title=str(row.get("subjects") or "").strip(),
                ministry=str(row.get("ministry") or "").strip(),
                members=[str(member).strip() for member in row.get("member") or [] if str(member).strip()],
                language="en",
                source_url=source_url,
                official_page_url=LS_PAGE,
                api_url=api_url,
                api_params=params,
                raw=row,
            )
            yielded += 1
            if limit and yielded >= limit:
                return
        rows_seen += len(rows)
        if rows_seen >= total:
            break
        page_number += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)
