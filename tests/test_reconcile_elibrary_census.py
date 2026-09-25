from __future__ import annotations

import unittest
import gzip
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from scripts.reconcile_elibrary_census import (
    load_shards, normalized_page, page_digest, scan_shard,
    verified_shard_lines, verify_first_shard_files,
)


def response(page: int, ids: list[str], dates: list[str], total: int) -> dict:
    return {"_embedded": {"searchResult": {
        "page": {"totalElements": total},
        "_embedded": {"objects": [
            {"_embedded": {"indexableObject": {
                "uuid": identifier, "name": identifier,
                "metadata": {
                    "dc.date.accessioned": [{"value": date}],
                    "dc.identifier.loksabhanumber": [{"value": "01"}],
                    "dc.identifier.sessionnumber": [{"value": "I"}],
                },
            }}}
            for identifier, date in zip(ids, dates)
        ]},
    }}}


class ReconcileElibraryCensusTests(unittest.TestCase):
    def setUp(self):
        self.pages = {
            0: response(0, ["a", "b"], ["2020-01-01", "2020-01-02"], 3),
            1: response(1, ["c"], ["2020-01-03"], 3),
        }

    def fetch(self, *, page, page_size, sort):
        self.assertEqual(page_size, 2)
        self.assertEqual(sort, "dc.date.accessioned,asc")
        return self.pages[page]

    @patch("scripts.reconcile_elibrary_census.PAGE_SIZE", 2)
    def test_two_pass_hash_agreement_and_tail_growth(self):
        with patch("scripts.reconcile_elibrary_census.time.sleep"):
            lines, first = scan_shard(
                self.fetch, kind="first", start=0, end=1, target_count=3,
                prior_accession=None, first=None, sleep_seconds=0,
            )
            self.assertEqual(len(lines), 3)
            self.assertEqual(first["record_count"], 3)
            self.assertEqual(len(first["page_hashes"]), 2)
            # A later accession at the tail is outside the fixed first-pass
            # prefix. It must not invalidate a valid dated snapshot.
            self.pages[1] = response(
                1, ["c", "new-tail"], ["2020-01-03", "2020-01-04"], 4,
            )
            audit_lines, audit = scan_shard(
                self.fetch, kind="audit", start=0, end=1, target_count=3,
                prior_accession=None, first=first, sleep_seconds=0,
            )
            self.assertEqual(audit_lines, [])
            self.assertEqual(audit["page_hashes"], first["page_hashes"])

    @patch("scripts.reconcile_elibrary_census.PAGE_SIZE", 2)
    def test_changed_metadata_or_shifted_prefix_fails_audit(self):
        with patch("scripts.reconcile_elibrary_census.time.sleep"):
            _, first = scan_shard(
                self.fetch, kind="first", start=0, end=1, target_count=3,
                prior_accession=None, first=None, sleep_seconds=0,
            )
            self.pages[1] = response(1, ["different"], ["2020-01-03"], 3)
            with self.assertRaisesRegex(RuntimeError, "record set or metadata changed"):
                scan_shard(self.fetch, kind="audit", start=0, end=1,
                           target_count=3, prior_accession=None,
                           first=first, sleep_seconds=0)
            self.pages[1] = response(1, ["c"], ["2020-01-03"], 2)
            with self.assertRaisesRegex(RuntimeError, "live count fell"):
                scan_shard(self.fetch, kind="audit", start=0, end=1,
                           target_count=3, prior_accession=None,
                           first=first, sleep_seconds=0)

    @patch("scripts.reconcile_elibrary_census.PAGE_SIZE", 2)
    def test_rejects_reversed_order_and_missing_accession(self):
        with self.assertRaisesRegex(RuntimeError, "not accession-ascending"):
            normalized_page(response(0, ["a", "b"], ["2021", "2020"], 3), 0, 3)
        with self.assertRaisesRegex(RuntimeError, "lacks an accession"):
            normalized_page(response(0, ["a", "b"], ["", "2020"], 3), 0, 3)

    def test_resume_rejects_shard_gap(self):
        root = "state/census/full-scan-test"
        paths = [f"{root}/first/part-{number:06d}-{number:06d}.json"
                 for number in (0, 2)]
        api = Mock()
        api.list_repo_files.return_value = paths
        with tempfile.TemporaryDirectory() as directory:
            files = {}
            for number, path in zip((0, 2), paths):
                local = Path(directory) / f"{number}.json"
                local.write_text(json.dumps({
                    "kind": "first", "sort": "dc.date.accessioned,asc",
                    "start_page": number, "end_page": number,
                    "page_hashes": ["hash"],
                }))
                files[path] = str(local)
            with patch("scripts.reconcile_elibrary_census.hf_hub_download",
                       side_effect=lambda repo, path, **kw: files[path]):
                with self.assertRaisesRegex(RuntimeError, "noncontiguous"):
                    load_shards("test/repo", root, "first", token="token", api=api)

    def test_small_git_backed_shard_is_verified_by_sha256(self):
        root = "state/census/full-scan-test"
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "part.jsonl.gz"
            local.write_bytes(b"small census shard")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            row = {"start_page": 0, "end_page": 0,
                   "bytes": local.stat().st_size, "sha256": digest}
            path = f"{root}/first/part-000000-000000.jsonl.gz"
            api = Mock()
            api.get_paths_info.return_value = [SimpleNamespace(
                path=path, size=local.stat().st_size, lfs=None)]
            with patch("scripts.reconcile_elibrary_census.hf_hub_download",
                       return_value=str(local)):
                verify_first_shard_files("test/repo", root, [row],
                                         token="token", api=api)
                row["sha256"] = "0" * 64
                with self.assertRaisesRegex(RuntimeError, "Git-file hash mismatch"):
                    verify_first_shard_files("test/repo", root, [row],
                                             token="token", api=api)

    @patch("scripts.reconcile_elibrary_census.PAGE_SIZE", 2)
    def test_final_snapshot_rechecks_decompressed_page_hashes(self):
        lines = ['{"record_id":"a"}\n', '{"record_id":"b"}\n',
                 '{"record_id":"c"}\n']
        shard = {"start_page": 0, "end_page": 1, "target_count": 3,
                 "record_count": 3,
                 "page_hashes": [page_digest(lines[:2]), page_digest(lines[2:])]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as output:
                output.writelines(lines)
            self.assertEqual(list(verified_shard_lines(path, shard)), lines)
            corrupted = {**shard, "page_hashes": ["0" * 64, shard["page_hashes"][1]]}
            with self.assertRaisesRegex(RuntimeError, "page hash mismatch"):
                list(verified_shard_lines(path, corrupted))
            with gzip.open(path, "wt", encoding="utf-8") as output:
                output.writelines(lines + ['{"record_id":"extra"}\n'])
            with self.assertRaisesRegex(RuntimeError, "extra records"):
                list(verified_shard_lines(path, shard))


if __name__ == "__main__":
    unittest.main()
