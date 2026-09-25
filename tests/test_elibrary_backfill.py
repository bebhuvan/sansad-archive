from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sansad_pipeline.census import Census
from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.sources.questions import QuestionRecord


class ElibraryBackfillTests(unittest.TestCase):
    def test_legacy_hindi_first_selection_is_repaired_from_retained_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            record_id = "elibrary_ls_question_item"
            census._upsert_many([QuestionRecord(
                record_id=record_id, source_type="questions_answers",
                house="lok_sabha", parliament_number="17", session="IX",
                document_number="1", document_subtype="U", document_date="",
                title="question", ministry="", members=[], language="und",
                source_url="https://example.test/item", official_page_url="",
                api_url="", api_params={},
                raw={"_source_system": "sansad_elibrary_dspace", "uuid": "item"},
            )])
            pdfs = [
                ("https://example.test/hindi", {"uuid": "hi", "name": "AU3055_hindi.pdf"}),
                ("https://example.test/english", {"uuid": "en", "name": "AU3055.pdf"}),
            ]
            bodies = {pdfs[0][0]: b"%PDF-1.4\nHindi",
                      pdfs[1][0]: b"%PDF-1.4\nEnglish"}

            def respond(request, *, timeout):
                return io.BytesIO(bodies[request.full_url])

            with patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond):
                documents = [census.store.download(url) for url, _ in pdfs]
            census.store.db.execute(
                """UPDATE census_records SET acquisition_status='downloaded',
                   document_sha256=? WHERE record_id=?""",
                (documents[0].sha256, record_id),
            )
            census._record_elibrary_pdf_attachments(
                record_id, pdfs, [document.sha256 for document in documents],
                primary_sha256=documents[0].sha256,
            )
            result = census.reselect_elibrary_primary_from_ledger(
                house="lok_sabha", parliament="17", session="IX",
            )
            self.assertEqual(result["changed"], 1)
            selected = census.store.db.one(
                "SELECT document_sha256 FROM census_records WHERE record_id=?", (record_id,)
            )["document_sha256"]
            self.assertEqual(selected, documents[1].sha256)
            self.assertTrue(Path(census.store.document(documents[0].sha256)["raw_path"]).exists())
            again = census.reselect_elibrary_primary_from_ledger(
                house="lok_sabha", parliament="17", session="IX",
            )
            self.assertEqual(again["changed"], 0)

    def test_new_acquisition_selects_english_even_when_bundle_lists_hindi_first(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            record_id = "elibrary_ls_question_item"
            census._upsert_many([QuestionRecord(
                record_id=record_id, source_type="questions_answers",
                house="lok_sabha", parliament_number="17", session="IX",
                document_number="1", document_subtype="U", document_date="",
                title="question", ministry="", members=[], language="und",
                source_url="https://example.test/item", official_page_url="",
                api_url="", api_params={},
                raw={"_source_system": "sansad_elibrary_dspace", "uuid": "item"},
            )])
            pdfs = [
                ("https://example.test/hindi", {"uuid": "hi", "name": "AU3055_hindi.pdf"}),
                ("https://example.test/english", {"uuid": "en", "name": "AU3055.pdf"}),
            ]
            bodies = {
                pdfs[0][0]: b"%PDF-1.4\nHindi original",
                pdfs[1][0]: b"%PDF-1.4\nEnglish original",
            }

            def respond(request, *, timeout):
                return io.BytesIO(bodies[request.full_url])

            with patch("sansad_pipeline.census.list_original_pdfs", return_value=pdfs), \
                 patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond), \
                 redirect_stdout(io.StringIO()):
                result = census.acquire_questions(
                    source="elibrary", house="lok_sabha", lok_sabha="17",
                    session="IX", workers=1, min_free_gib=0,
                )
            self.assertEqual(result["downloaded"], 1)
            self.assertEqual(result["original_pdfs_downloaded"], 2)
            selected = census.store.db.one(
                "SELECT document_sha256 FROM census_records WHERE record_id=?", (record_id,)
            )["document_sha256"]
            self.assertEqual(Path(census.store.document(selected)["raw_path"]).read_bytes(),
                             bodies[pdfs[1][0]])
            attachments = census.store.db.all(
                "SELECT bitstream_id,document_sha256 FROM elibrary_pdf_attachments "
                "ORDER BY position"
            )
            self.assertEqual([row["bitstream_id"] for row in attachments], ["hi", "en"])
            self.assertEqual(attachments[1]["document_sha256"], selected)

    def test_failed_extra_pdf_does_not_create_complete_ledger_and_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            record_id = "elibrary_ls_question_item"
            census._upsert_many([QuestionRecord(
                record_id=record_id, source_type="questions_answers",
                house="lok_sabha", parliament_number="17", session="IX",
                document_number="1", document_subtype="U", document_date="",
                title="question", ministry="", members=[], language="und",
                source_url="https://example.test/item", official_page_url="",
                api_url="", api_params={},
                raw={"_source_system": "sansad_elibrary_dspace", "uuid": "item"},
            )])
            pdfs = [
                (f"https://example.test/{name}", {"uuid": name, "name": f"{name}.pdf"})
                for name in ("english", "hindi")
            ]
            bodies = {pdfs[0][0]: b"%PDF-1.4\nEnglish", pdfs[1][0]: b"broken"}

            def respond(request, *, timeout):
                return io.BytesIO(bodies[request.full_url])

            with patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond):
                selected = census.store.download(pdfs[0][0])
            census.store.db.execute(
                """UPDATE census_records SET acquisition_status='downloaded',document_sha256=?
                   WHERE record_id=?""", (selected.sha256, record_id),
            )
            with patch("sansad_pipeline.census.list_original_pdfs", return_value=pdfs), \
                 patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond), \
                 redirect_stdout(io.StringIO()):
                failed = census.backfill_elibrary_pdf_attachments(
                    house="lok_sabha", parliament="17", session="IX", min_free_gib=0,
                )
            self.assertEqual(failed["failed"], 1)
            self.assertEqual(census.store.db.one(
                "SELECT COUNT(*) n FROM elibrary_pdf_attachments")["n"], 0)
            self.assertIn("attachment backfill", census.store.db.one(
                "SELECT last_error FROM census_records WHERE record_id=?",
                (record_id,))["last_error"])
            bodies[pdfs[1][0]] = b"%PDF-1.4\nHindi"
            with patch("sansad_pipeline.census.list_original_pdfs", return_value=pdfs), \
                 patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond), \
                 redirect_stdout(io.StringIO()):
                recovered = census.backfill_elibrary_pdf_attachments(
                    house="lok_sabha", parliament="17", session="IX", min_free_gib=0,
                )
            self.assertEqual(recovered["completed"], 1)
            self.assertEqual(census.store.db.one(
                "SELECT COUNT(*) n FROM elibrary_pdf_attachments")["n"], 2)
            self.assertIsNone(census.store.db.one(
                "SELECT last_error FROM census_records WHERE record_id=?",
                (record_id,))["last_error"])

    def test_backfills_missing_attachment_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            record_id = "elibrary_ls_question_item"
            census._upsert_many([QuestionRecord(
                record_id=record_id, source_type="questions_answers",
                house="lok_sabha", parliament_number="17", session="IX",
                document_number="1", document_subtype="U", document_date="",
                title="question", ministry="", members=[], language="und",
                source_url="https://example.test/item", official_page_url="",
                api_url="", api_params={},
                raw={"_source_system": "sansad_elibrary_dspace", "uuid": "item"},
            )])
            pdfs = [
                (f"https://example.test/{name}", {"uuid": name, "name": f"{name}.pdf"})
                for name in ("english", "hindi")
            ]
            bodies = {
                pdfs[0][0]: b"%PDF-1.4\nEnglish original",
                pdfs[1][0]: b"%PDF-1.4\nHindi original",
            }
            calls = []

            def respond(request, *, timeout):
                calls.append(request.full_url)
                return io.BytesIO(bodies[request.full_url])

            with patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond):
                selected = census.store.download(pdfs[0][0])
            census.store.db.execute(
                """UPDATE census_records SET acquisition_status='downloaded',document_sha256=?
                   WHERE record_id=?""", (selected.sha256, record_id),
            )
            with patch("sansad_pipeline.census.list_original_pdfs", return_value=pdfs), \
                 patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond), \
                 redirect_stdout(io.StringIO()):
                result = census.backfill_elibrary_pdf_attachments(
                    house="lok_sabha", parliament="17", session="IX", min_free_gib=0,
                )
                replay = census.backfill_elibrary_pdf_attachments(
                    house="lok_sabha", parliament="17", session="IX", min_free_gib=0,
                )
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["original_pdfs"], 2)
            self.assertEqual(result["new_original_pdfs"], 1)
            self.assertEqual(replay["selected"], 0)
            self.assertEqual(calls, [pdfs[0][0], pdfs[1][0]])
            attachments = census.store.db.all(
                "SELECT bitstream_id,document_sha256 FROM elibrary_pdf_attachments ORDER BY position"
            )
            self.assertEqual([row["bitstream_id"] for row in attachments], ["english", "hindi"])
            self.assertEqual(attachments[0]["document_sha256"], selected.sha256)
            self.assertNotEqual(attachments[1]["document_sha256"], selected.sha256)
            for row, (url, _) in zip(attachments, pdfs):
                path = census.store.document(row["document_sha256"])["raw_path"]
                self.assertEqual(Path(path).read_bytes(), bodies[url])


if __name__ == "__main__":
    unittest.main()
