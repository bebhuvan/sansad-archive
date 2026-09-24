from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sansad_pipeline.census import Census
from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.sources.questions import QuestionRecord


class AcquisitionRetryTests(unittest.TestCase):
    def test_elibrary_acquisition_retains_all_offered_pdf_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            census._upsert_many([QuestionRecord(
                record_id="elibrary_ls_question_item", source_type="questions_answers",
                house="lok_sabha", parliament_number="17", session="IX",
                document_number="1", document_subtype="U", document_date="",
                title="question", ministry="", members=[], language="und",
                source_url="https://example.test/item", official_page_url="",
                api_url="", api_params={},
                raw={"_source_system": "sansad_elibrary_dspace", "uuid": "item"},
            )])
            pdfs = [
                (f"https://example.test/{name}", {"uuid": name, "name": f"{name}.pdf", "sizeBytes": 12})
                for name in ("english", "hindi")
            ]
            document = SimpleNamespace(sha256="a" * 64, size_bytes=12, already_present=False)
            census.store.db.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?)",
                (document.sha256, 12, "application/pdf", "/dummy.pdf", "now"),
            )
            with patch("sansad_pipeline.census.list_original_pdfs", return_value=pdfs), \
                 patch.object(census.store, "download", return_value=document) as download, \
                 redirect_stdout(io.StringIO()):
                census.acquire_questions(source="elibrary", house="lok_sabha",
                                         lok_sabha="17", session="IX", min_free_gib=0)
            self.assertEqual([call.args[0] for call in download.call_args_list],
                             [pdfs[0][0], pdfs[1][0]])
            inventory = download.call_args_list[0].kwargs["metadata"]["elibrary_original_pdf_inventory"]
            self.assertEqual([row["uuid"] for row in inventory], ["english", "hindi"])
            self.assertTrue(download.call_args_list[0].kwargs["metadata"]["elibrary_primary_pdf"])
            self.assertFalse(download.call_args_list[1].kwargs["metadata"]["elibrary_primary_pdf"])
            attachments = census.store.db.all(
                "SELECT bitstream_id,position,document_sha256 FROM elibrary_pdf_attachments ORDER BY position"
            )
            self.assertEqual([row["bitstream_id"] for row in attachments], ["english", "hindi"])
            self.assertTrue(all(row["document_sha256"] == document.sha256 for row in attachments))

    def test_extra_pdf_failure_keeps_item_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            census._upsert_many([QuestionRecord(
                record_id="elibrary_ls_question_item", source_type="questions_answers",
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
            document = SimpleNamespace(sha256="a" * 64, size_bytes=12, already_present=False)
            with patch("sansad_pipeline.census.list_original_pdfs", return_value=pdfs), \
                 patch.object(census.store, "download",
                              side_effect=[document, ValueError("not a PDF")]), \
                 redirect_stdout(io.StringIO()):
                result = census.acquire_questions(source="elibrary", house="lok_sabha",
                                                  lok_sabha="17", session="IX",
                                                  min_free_gib=0)
            self.assertEqual(result["failed"], 1)
            row = census.store.db.one("SELECT acquisition_status,document_sha256 FROM census_records")
            self.assertEqual(row["acquisition_status"], "failed")
            self.assertIsNone(row["document_sha256"])

    def test_extra_original_bytes_are_stored_with_independent_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            census._upsert_many([QuestionRecord(
                record_id="elibrary_ls_question_item", source_type="questions_answers",
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
            def respond(request, *, timeout):
                return io.BytesIO(bodies[request.full_url])

            with patch("sansad_pipeline.census.list_original_pdfs", return_value=pdfs), \
                 patch("sansad_pipeline.storage.urllib.request.urlopen", side_effect=respond), \
                 redirect_stdout(io.StringIO()):
                result = census.acquire_questions(source="elibrary", house="lok_sabha",
                                                  lok_sabha="17", session="IX",
                                                  min_free_gib=0)
            self.assertEqual(result["downloaded"], 1)
            self.assertEqual(result["bytes_added"], sum(map(len, bodies.values())))
            attachments = census.store.db.all(
                "SELECT bitstream_id,document_sha256 FROM elibrary_pdf_attachments ORDER BY position"
            )
            self.assertEqual(len({row["document_sha256"] for row in attachments}), 2)
            for row, (url, _) in zip(attachments, pdfs):
                path = census.store.document(row["document_sha256"])["raw_path"]
                self.assertEqual(Path(path).read_bytes(), bodies[url])

    def test_retry_pass_visits_more_than_one_batch_of_failed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            census._upsert_many([
                QuestionRecord(
                    record_id=f"q{i:03d}", source_type="questions_answers",
                    house="lok_sabha", parliament_number="18", session="8",
                    document_number=str(i), document_subtype="S", document_date="",
                    title="", ministry="", members=[], language="en",
                    source_url=f"https://example.test/{i}.pdf", official_page_url="",
                    api_url="", api_params={}, raw={},
                ) for i in range(45)
            ])
            census.store.db.execute("UPDATE census_records SET acquisition_status='failed'")
            with patch.object(census.store, "download", side_effect=ValueError("not a PDF")) as download:
                with redirect_stdout(io.StringIO()):
                    result = census.acquire_questions(
                        source="current", house="lok_sabha", lok_sabha="18",
                        session="8", retry_failed=True, workers=4, min_free_gib=0,
                    )
            self.assertEqual(result["selected"], 45)
            self.assertEqual(download.call_count, 45)


if __name__ == "__main__":
    unittest.main()
