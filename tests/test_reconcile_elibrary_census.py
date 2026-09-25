from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.reconcile_elibrary_census import normalized_page, scan_shard


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


if __name__ == "__main__":
    unittest.main()
