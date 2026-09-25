#!/usr/bin/env python3
"""Compare a published session with its separate full LiteParse OCR shards.

This read-only audit never treats OCR or Space Bunny as ground truth. Visual
conflicts are reported only when the OCR shard carries validated image metrics.
Older shards without those measurements are explicitly counted as unassessed.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pyarrow.parquet as pq
from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sansad_pipeline.image_quality import valid_image_metrics  # noqa: E402
from sansad_pipeline.text_quality import local_content_empty  # noqa: E402
from sansad_pipeline.validation import content_numbers  # noqa: E402
from scripts.full_ocr_layer import (Page, completion_marker_path, inventory_sha256,
                                    shard_paths, verify_shard_rows)  # noqa: E402
from scripts.cloud_state import _commit_with_retry  # noqa: E402
from scripts.visual_evidence_layer import completed_shards as completed_visual_shards  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_pages(repo: str, source: str, house: str, parliament: str,
                 session: str, token: str | None) -> tuple[list[dict], dict]:
    key = f"{house}-p{parliament}-s{session}"
    marker_path = completion_marker_path(source, key)
    marker_file = Path(hf_hub_download(repo, marker_path, repo_type="dataset", token=token))
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    inputs = marker.get("inputs") or {}
    complete_field = "snapshot_complete" if source == "elibrary" else "session_complete"
    if (marker.get(complete_field) is not True
            or any(inputs.get(field) != value for field, value in {
                "source": source, "house": house, "parliament": parliament,
                "session": session,
            }.items())):
        raise RuntimeError(f"completion marker identity mismatch: {marker_path}")
    tranche = str(marker.get("tranche_path") or "").strip("/")
    if not tranche.startswith(f"data/{house}/parliament-{parliament}/session-{session}/"):
        raise RuntimeError(f"completion marker has invalid tranche path: {tranche}")
    checksums = Path(hf_hub_download(
        repo, f"{tranche}/SHA256SUMS", repo_type="dataset", token=token,
    )).read_text(encoding="utf-8")
    expected = dict(line.split("  ", 1)[::-1] for line in checksums.splitlines())
    if "pages.parquet" not in expected or "metadata.json" not in expected:
        raise RuntimeError("published tranche lacks checksums for page data or metadata")
    page_file = Path(hf_hub_download(
        repo, f"{tranche}/pages.parquet", repo_type="dataset", token=token,
    ))
    metadata_file = Path(hf_hub_download(
        repo, f"{tranche}/metadata.json", repo_type="dataset", token=token,
    ))
    for name, path in (("pages.parquet", page_file), ("metadata.json", metadata_file)):
        if sha256_file(path) != expected[name]:
            raise RuntimeError(f"published tranche checksum mismatch: {tranche}/{name}")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    rows = pq.read_table(page_file, columns=[
        "document_sha256", "page_number", "local_text", "local_markdown",
        "adjudicated_markdown", "route",
    ]).to_pylist()
    rows.sort(key=lambda row: (row["document_sha256"], row["page_number"]))
    keys = [(row["document_sha256"], row["page_number"]) for row in rows]
    if (len(keys) != len(set(keys)) or len(rows) != metadata.get("page_count")
            or any(not re.fullmatch(r"[0-9a-f]{64}", digest)
                   or type(number) is not int or number < 1 for digest, number in keys)):
        raise RuntimeError("published page inventory is incomplete or invalid")
    if any(row["adjudicated_markdown"] is None for row in rows):
        raise RuntimeError("published session lacks a separate Space Bunny layer on some pages")
    return rows, {"tranche": tranche, "marker": marker_path,
                  "marker_sha256": sha256_file(marker_file),
                  "pages_sha256": expected["pages.parquet"]}


def sidecar_rows(repo: str, key: str, pages: list[dict], token: str | None,
                 api: HfApi, marker_sha256: str) -> tuple[list[dict], dict]:
    identities = [Page(row["document_sha256"], row["page_number"], Path("unused"))
                  for row in pages]
    inventory = inventory_sha256(identities)
    prefix = f"layers/full-ocr/{key}/inventory-{inventory[:16]}/"
    all_files = set(api.list_repo_files(repo, repo_type="dataset"))
    roots = {path.rsplit("/part-", 1)[0] for path in all_files
             if path.startswith(prefix) and "/part-" in path and path.endswith(".json")}
    if len(roots) != 1:
        raise RuntimeError(f"expected one matching OCR root for {key}; found {sorted(roots)}")
    root = roots.pop()
    manifests = sorted(path for path in all_files
                       if path.startswith(root + "/part-") and path.endswith(".json"))
    if not manifests:
        raise RuntimeError(f"no OCR shards at {root}")
    result: list[dict] = []
    next_index = 0
    for manifest_path in manifests:
        manifest = json.loads(Path(hf_hub_download(
            repo, manifest_path, repo_type="dataset", token=token,
        )).read_text(encoding="utf-8"))
        start, end = manifest.get("start_index"), manifest.get("end_index")
        if (type(start) is not int or type(end) is not int or start != next_index
                or end < start or end >= len(pages)
                or manifest.get("inventory_sha256") != inventory
                or manifest.get("record_count") != end - start + 1
                or manifest_path != shard_paths(root, start, end)[1]):
            raise RuntimeError(f"OCR shard inventory mismatch: {manifest_path}")
        archive_path, _ = shard_paths(root, start, end)
        if archive_path not in all_files or manifest.get("path") != archive_path:
            raise RuntimeError(f"OCR shard archive missing: {archive_path}")
        archive = Path(hf_hub_download(
            repo, archive_path, repo_type="dataset", token=token,
        ))
        if (archive.stat().st_size != manifest.get("bytes")
                or sha256_file(archive) != manifest.get("sha256")):
            raise RuntimeError(f"OCR shard checksum mismatch: {archive_path}")
        origins = verify_shard_rows(archive, identities, start, end,
                                    manifest["engine_version"])
        if "origin_counts" in manifest and manifest["origin_counts"] != origins:
            raise RuntimeError(f"OCR shard origin count mismatch: {manifest_path}")
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            result.extend(json.loads(line) for line in handle)
        next_index = end + 1
    completion_path = root + "/complete.json"
    complete = completion_path in all_files
    if complete:
        completion = json.loads(Path(hf_hub_download(
            repo, completion_path, repo_type="dataset", token=token,
        )).read_text(encoding="utf-8"))
        if (completion.get("inventory_sha256") != inventory
                or completion.get("pages") != len(pages)
                or completion.get("shards") != len(manifests)
                or (completion.get("completion_marker") or {}).get("sha256") != marker_sha256
                or next_index != len(pages)):
            raise RuntimeError("OCR completion marker disagrees with verified shard coverage")
    return result, {"root": root, "inventory_sha256": inventory,
                    "shards": len(manifests), "complete": complete}


def visual_rows(repo: str, key: str, pages: list[dict], token: str | None,
                api: HfApi, marker_sha256: str) -> tuple[list[dict], dict]:
    identities = [Page(row["document_sha256"], row["page_number"], Path("unused"))
                  for row in pages]
    inventory = inventory_sha256(identities)
    prefix = f"layers/visual-evidence/{key}/inventory-{inventory[:16]}/"
    files = set(api.list_repo_files(repo, repo_type="dataset"))
    roots = {path.rsplit("/part-", 1)[0] for path in files
             if path.startswith(prefix) and "/part-" in path and path.endswith(".json")}
    if not roots:
        return [], {"status": "not_found"}
    if len(roots) != 1:
        raise RuntimeError(f"expected one visual-evidence root for {key}: {sorted(roots)}")
    root = roots.pop()
    match = re.search(r"/render-(\d+)dpi-v1$", root)
    if not match:
        raise RuntimeError(f"unsupported visual-evidence method: {root}")
    dpi = int(match.group(1))
    shards = completed_visual_shards(api, repo, root, identities, inventory, dpi, token)
    result = []
    for manifest in shards:
        archive = Path(hf_hub_download(repo, manifest["path"],
                                       repo_type="dataset", token=token))
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            result.extend(json.loads(line) for line in handle)
    completion_path = root + "/complete.json"
    complete = completion_path in files
    if complete:
        completion = json.loads(Path(hf_hub_download(
            repo, completion_path, repo_type="dataset", token=token,
        )).read_text(encoding="utf-8"))
        if (completion.get("inventory_sha256") != inventory
                or completion.get("pages") != len(pages)
                or completion.get("shards") != len(shards)
                or (completion.get("completion_marker") or {}).get("sha256") != marker_sha256
                or len(result) != len(pages)):
            raise RuntimeError("visual completion marker disagrees with verified page coverage")
    return result, {"status": "complete" if complete else "checkpointed",
                    "root": root, "shards": len(shards), "pages": len(result)}


def audit_layers(pages: list[dict], ocr_rows: list[dict],
                 visual_rows: list[dict] | None = None) -> dict:
    visual_rows = visual_rows or []
    if len(ocr_rows) > len(pages):
        raise RuntimeError("OCR rows exceed published page inventory")
    if len(visual_rows) > len(pages):
        raise RuntimeError("visual rows exceed published page inventory")
    flags: list[dict] = []
    model_empty_visible: list[dict] = []
    numeric_disagreements: list[dict] = []
    numeric_compared = 0
    triad_counts = {name: 0 for name in (
        "all_equal", "local_model_equal", "local_ocr_equal",
        "model_ocr_equal", "all_different",
    )}
    triad_disagreements: list[dict] = []
    measured = 0
    blank = 0
    ocr_unmeasured = 0
    for index, published in enumerate(pages):
        key = (published["document_sha256"], published["page_number"])
        ocr = ocr_rows[index] if index < len(ocr_rows) else None
        visual = visual_rows[index] if index < len(visual_rows) else None
        if ocr is not None and key != (ocr["document_sha256"], ocr["page_number"]):
            raise RuntimeError(f"OCR/published page identity mismatch at index {index}")
        if visual is not None and key != (visual["document_sha256"], visual["page_number"]):
            raise RuntimeError(f"visual/published page identity mismatch at index {index}")
        model_text = str(published["adjudicated_markdown"] or "")
        ocr_text = str((ocr or {}).get("markdown") or (ocr or {}).get("text") or "")
        if ocr is not None and ocr_text.strip() and model_text.strip():
            numeric_compared += 1
            ocr_numbers = content_numbers(ocr_text)
            model_numbers = content_numbers(model_text)
            if ocr_numbers != model_numbers:
                ocr_only = sorted((ocr_numbers - model_numbers).elements())
                model_only = sorted((model_numbers - ocr_numbers).elements())
                numeric_disagreements.append({
                    "document_sha256": key[0], "page_number": key[1],
                    "ocr_only_count": len(ocr_only), "model_only_count": len(model_only),
                    "ocr_only_sample": ocr_only[:20], "model_only_sample": model_only[:20],
                })
            if not local_content_empty(published["local_text"], published["local_markdown"]):
                local_text = str(published["local_markdown"] or published["local_text"] or "")
                local_numbers = content_numbers(local_text)
                if local_numbers or ocr_numbers or model_numbers:
                    if local_numbers == ocr_numbers == model_numbers:
                        pattern = "all_equal"
                    elif local_numbers == model_numbers:
                        pattern = "local_model_equal"
                    elif local_numbers == ocr_numbers:
                        pattern = "local_ocr_equal"
                    elif model_numbers == ocr_numbers:
                        pattern = "model_ocr_equal"
                    else:
                        pattern = "all_different"
                    triad_counts[pattern] += 1
                    if pattern != "all_equal":
                        triad_disagreements.append({
                            "document_sha256": key[0], "page_number": key[1],
                            "route": published.get("route"), "pattern": pattern,
                            "local_sample": sorted(local_numbers.elements())[:20],
                            "ocr_sample": sorted(ocr_numbers.elements())[:20],
                            "model_sample": sorted(model_numbers.elements())[:20],
                        })
        ocr_metrics = ocr if ocr is not None and "visually_blank" in ocr else None
        if ocr_metrics is not None and not valid_image_metrics(ocr_metrics):
            raise RuntimeError(f"invalid OCR visual evidence for {key}")
        if visual is not None and not valid_image_metrics(visual):
            raise RuntimeError(f"invalid backfilled visual evidence for {key}")
        if ocr_metrics is not None and visual is not None and any(
            ocr_metrics[field] != visual[field] for field in (
                "image_pixels", "image_dark_pixels", "image_dark_pixel_cutoff", "visually_blank"
            )
        ):
            raise RuntimeError(f"OCR/backfill visual evidence disagrees for {key}")
        metrics = visual or ocr_metrics
        if metrics is None:
            ocr_unmeasured += ocr is not None
            continue
        measured += 1
        if not metrics["visually_blank"]:
            if not model_text.strip():
                model_empty_visible.append({
                    "document_sha256": key[0], "page_number": key[1],
                    "image_dark_pixels": metrics["image_dark_pixels"],
                    "image_pixels": metrics["image_pixels"],
                })
            continue
        blank += 1
        reasons = []
        if ocr is not None and str(ocr.get("text") or "").strip():
            reasons.append("ocr-nonempty-on-blank")
        if not local_content_empty(published["local_text"], published["local_markdown"]):
            reasons.append("local-nonempty-on-blank")
        if str(published["adjudicated_markdown"] or "").strip():
            reasons.append("model-nonempty-on-blank")
        if reasons:
            flags.append({"document_sha256": key[0], "page_number": key[1],
                          "reasons": reasons, "image_dark_pixels": metrics["image_dark_pixels"],
                          "image_pixels": metrics["image_pixels"]})
    counts = {reason: sum(reason in item["reasons"] for item in flags)
              for reason in ("ocr-nonempty-on-blank", "local-nonempty-on-blank",
                             "model-nonempty-on-blank")}
    return {"published_pages": len(pages), "ocr_pages": len(ocr_rows),
            "visual_sidecar_pages": len(visual_rows),
            "unprocessed_ocr_pages": len(pages) - len(ocr_rows),
            "visually_assessed_pages": measured,
            "ocr_pages_without_visual_metrics": ocr_unmeasured,
            "published_pages_without_visual_metrics": len(pages) - measured,
            "visually_blank_pages_among_assessed": blank,
            "conflict_counts": counts, "conflicts": flags,
            "model_empty_on_visible_pages": len(model_empty_visible),
            "model_empty_on_visible": model_empty_visible,
            "ocr_model_numeric_pages_compared": numeric_compared,
            "ocr_model_numeric_disagreement_pages": len(numeric_disagreements),
            "ocr_model_numeric_disagreements": numeric_disagreements,
            "numeric_triad_pages_compared": sum(triad_counts.values()),
            "numeric_triad_pattern_counts": triad_counts,
            "numeric_triad_disagreements": triad_disagreements}


def publish_report(repo: str, scope: str, report_bytes: bytes, ocr_pages: int,
                   token: str, api: HfApi) -> str:
    digest = hashlib.sha256(report_bytes).hexdigest()
    path = (f"audits/full-ocr-model-visual/{scope}/"
            f"ocr-pages-{ocr_pages:08d}-{digest[:16]}.json")
    found = list(api.get_paths_info(repo, [path], repo_type="dataset", expand=False))
    if not found:
        _commit_with_retry(api, repo=repo, operations=[CommitOperationAdd(
            path_in_repo=path, path_or_fileobj=report_bytes,
        )], message=f"Audit OCR/model visual conflicts {scope} {ocr_pages} pages")
    remote = Path(hf_hub_download(repo, path, repo_type="dataset", token=token,
                                  force_download=True)).read_bytes()
    if remote != report_bytes:
        raise RuntimeError(f"published audit report differs: {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source", choices=("current", "elibrary"), required=True)
    parser.add_argument("--house", choices=("lok_sabha", "rajya_sabha"), required=True)
    parser.add_argument("--parliament", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--publish", action="store_true",
                        help="store the immutable, verified report on Hugging Face")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.parliament) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", args.session
    ):
        parser.error("parliament and session labels must be path-safe")
    token = os.environ.get("HF_TOKEN")
    if args.publish and not token:
        parser.error("--publish requires HF_TOKEN")
    pages, publication = source_pages(args.repo, args.source, args.house,
                                      args.parliament, args.session, token)
    key = f"{args.house}-p{args.parliament}-s{args.session}"
    ocr, sidecar = sidecar_rows(args.repo, key, pages, token,
                                HfApi(token=token), publication["marker_sha256"])
    visual, visual_sidecar = visual_rows(args.repo, key, pages, token,
                                         HfApi(token=token), publication["marker_sha256"])
    report = {"scope": key, "source": args.source, "publication": publication,
              "ocr_sidecar": sidecar, "visual_sidecar": visual_sidecar,
              **audit_layers(pages, ocr, visual)}
    value = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(value, encoding="utf-8")
    published = (publish_report(args.repo, key, value.encode("utf-8"), len(ocr),
                                token, HfApi(token=token)) if args.publish else None)
    print(json.dumps({key: report[key] for key in (
        "scope", "published_pages", "ocr_pages", "unprocessed_ocr_pages",
        "visual_sidecar_pages", "visually_assessed_pages",
        "ocr_pages_without_visual_metrics", "published_pages_without_visual_metrics",
        "visually_blank_pages_among_assessed", "conflict_counts",
        "model_empty_on_visible_pages",
        "ocr_model_numeric_pages_compared", "ocr_model_numeric_disagreement_pages",
        "numeric_triad_pages_compared", "numeric_triad_pattern_counts",
    )} | {"report_path": published}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
