from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.refresh_elibrary_census import ACCESSION_SORT, collect_prefix


def page_response(page: int, ids: list[str], dates: list[str], total: int = 6) -> dict:
    return {"_embedded": {"searchResult": {
        "page": {"number": page, "size": 2, "totalElements": total, "totalPages": 3},
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


class RefreshElibraryCensusTests(unittest.TestCase):
    def setUp(self):
        self.pages = {
            0: page_response(0, ["new-a", "new-b"], ["2026-09-02", "2026-09-01"]),
            1: page_response(1, ["old-a", "old-b"], ["2026-08-02", "2026-08-01"]),
            2: page_response(2, ["old-c", "old-d"], ["2026-07-02", "2026-07-01"]),
        }
        self.existing = {f"elibrary_ls_question_old-{letter}" for letter in "abcd"}

    def fetch(self, *, page, page_size, sort):
        self.assertEqual(page_size, 2)
        self.assertEqual(sort, ACCESSION_SORT)
        return self.pages[page]

    @patch("scripts.refresh_elibrary_census.time.sleep")
    def test_prefix_append_preserves_source_provenance(self, _sleep):
        records, evidence = collect_prefix(
            self.existing, 4, page_size=2, overlap_pages=1, fetch=self.fetch,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(evidence["live_count"], 6)
        self.assertEqual(evidence["overlap_checked"], 2)
        self.assertEqual(records[0]["record_id"], "elibrary_ls_question_new-a")
        self.assertEqual(records[0]["api_params"]["sort"], ACCESSION_SORT)
        self.assertEqual(records[0]["raw"]["dc.date.accessioned"], ["2026-09-02"])
        self.assertEqual(records[0]["acquisition_status"], "discovered")
        self.assertIsNone(records[0]["document_sha256"])

    @patch("scripts.refresh_elibrary_census.time.sleep")
    def test_rejects_known_id_in_new_prefix(self, _sleep):
        self.pages[0] = page_response(0, ["old-a", "new-b"], ["2026-09-02", "2026-09-01"])
        with self.assertRaisesRegex(RuntimeError, "known ID inside new-accession prefix"):
            collect_prefix(self.existing, 4, page_size=2, overlap_pages=1, fetch=self.fetch)

    @patch("scripts.refresh_elibrary_census.time.sleep")
    def test_rejects_unknown_id_after_boundary(self, _sleep):
        self.pages[1] = page_response(1, ["new-c", "old-b"], ["2026-08-02", "2026-08-01"])
        with self.assertRaisesRegex(RuntimeError, "unknown ID beyond count-delta boundary"):
            collect_prefix(self.existing, 4, page_size=2, overlap_pages=1, fetch=self.fetch)

    @patch("scripts.refresh_elibrary_census.time.sleep")
    def test_rejects_reordered_or_changed_source(self, _sleep):
        self.pages[1] = page_response(1, ["old-a", "old-b"], ["2026-08-01", "2026-08-02"])
        with self.assertRaisesRegex(RuntimeError, "not accession-descending"):
            collect_prefix(self.existing, 4, page_size=2, overlap_pages=1, fetch=self.fetch)
        self.pages[1] = page_response(1, ["old-a", "old-b"],
                                      ["2026-08-02", "2026-08-01"], total=7)
        with self.assertRaisesRegex(RuntimeError, "live count changed"):
            collect_prefix(self.existing, 4, page_size=2, overlap_pages=1, fetch=self.fetch)


if __name__ == "__main__":
    unittest.main()
