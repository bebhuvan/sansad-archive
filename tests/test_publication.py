from __future__ import annotations

import json
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.db import json_text
from sansad_pipeline.publication import (
    PublicationBuilder, Scope, _git_blob_id, compare_remote_bundle,
    document_slug, sha256_file, slugify,
)
from sansad_pipeline.storage import Store, now
from sansad_pipeline.validation import content_numbers, numbers, text_flags
from sansad_pipeline.config import ValidationConfig


class PublicationTests(unittest.TestCase):
    def test_builds_and_verifies_multiformat_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            Image.new("RGB", (100, 100), "white").save(source, "PDF")
            config = Config(project_root=root, storage=StorageConfig(root=Path("data")))
            store = Store(config)
            item = store.ingest(source, source_uri="https://example.test/source.pdf")
            artifact_dir = root / "data" / "artifacts" / item.sha256 / "run-00000001"
            artifact_dir.mkdir(parents=True)
            page = {
                "document_sha256": item.sha256,
                "run_id": 1,
                "page_number": 1,
                "route": "native",
                "route_reasons": [],
                "engine": "test",
                "engine_version": "1",
                "text": "Research text",
                "markdown": "# Research text",
                "width": 100.0,
                "height": 100.0,
                "mean_confidence": None,
                "validation_status": "review",
                "validation_flags": ["numeric-disagreement"],
            }
            (artifact_dir / "document.json").write_text(json.dumps([page]), encoding="utf-8")
            (artifact_dir / "document.md").write_text("# Research text\n", encoding="utf-8")
            run_id = store.db.execute(
                """INSERT INTO runs
                   (document_sha256,status,config_json,artifact_dir,started_at,finished_at,error)
                   VALUES (?,?,?,?,?,?,NULL)""",
                (item.sha256, "complete", "{}", str(artifact_dir), now(), now()),
            )
            self.assertEqual(run_id, 1)
            review_dir = artifact_dir / "nvidia" / "model" / "page-00001" / "attempt-1"
            review_dir.mkdir(parents=True)
            response_path = review_dir / "response.json"
            response_path.write_text("{}", encoding="utf-8")
            (review_dir / "adjudicated.md").write_text("# Reviewed text", encoding="utf-8")
            store.db.execute(
                """INSERT INTO adjudications
                   (run_id,page_number,provider,model,request_sha256,response_path,
                    prompt_tokens,completion_tokens,total_tokens,reported_cost,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (1, 1, "nvidia", "nvidia/test", "abc", str(response_path), 10, 2, 12, None, now()),
            )
            store.db.execute(
                """INSERT INTO census_records
                   (record_id,source_type,house,parliament_number,session,document_number,
                    document_subtype,document_date,title,ministry,members_json,language,
                    source_url,official_page_url,api_url,api_params_json,raw_json,
                    discovered_at,acquisition_status,document_sha256,last_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    "record-1", "questions_answers", "lok_sabha", "18", "8", "198",
                    "starred", "2026-07-31", "Title", "Ministry", "[]", "English",
                    "https://example.test/source.pdf", "https://example.test/item", "", "{}",
                    json_text({}), now(), "acquired", item.sha256,
                ),
            )
            output = root / "bundle"
            result = PublicationBuilder(config).build(
                Scope("lok_sabha", "18", "8"), output, minimum_pdf_saving_percent=100
            )
            self.assertEqual(result["document_count"], 1)
            self.assertEqual(result["page_count"], 1)
            self.assertEqual(result["adjudicated_page_count"], 1)
            self.assertEqual(result["model_flagged_page_count"], 0)
            self.assertEqual(result["model_numeric_disagreement_page_count"], 0)
            self.assertTrue((output / "documents.parquet").is_file())
            self.assertTrue((output / "pages.jsonl.zst").is_file())
            self.assertTrue((output / "sansad.duckdb").is_file())
            self.assertTrue((output / "webdataset" / "shard-00000.tar").is_file())
            with tarfile.open(output / "webdataset" / "shard-00000.tar") as archive:
                canonical = archive.extractfile(f"{item.sha256}.md").read().decode("utf-8")
                local = archive.extractfile(f"{item.sha256}.local.md").read().decode("utf-8")
                adjudicated = archive.extractfile(
                    f"{item.sha256}.adjudicated.md"
                ).read().decode("utf-8")
            self.assertEqual(canonical, "# Research text")
            self.assertEqual(local, "# Research text\n")
            self.assertEqual(adjudicated, "# Reviewed text")

            manifest = [
                json.loads(line)
                for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(manifest), 1)
            self.assertEqual(manifest[0]["document_sha256"], item.sha256)
            self.assertEqual(manifest[0]["record_ids"], ["record-1"])
            self.assertEqual(manifest[0]["page_count"], 1)
            self.assertEqual(manifest[0]["source_urls"], ["https://example.test/source.pdf"])
            readable = output / manifest[0]["path"]
            self.assertTrue((readable / "document.md").is_file())
            self.assertTrue((readable / "document.txt").is_file())
            self.assertTrue((readable / "document.json").is_file())
            self.assertTrue((readable / "original.pdf").is_file())
            payload = json.loads((readable / "document.json").read_text(encoding="utf-8"))
            page_payload = payload["pages"][0]
            self.assertEqual(page_payload["canonical_source"], "local:test@1")
            self.assertEqual(page_payload["local_markdown"], "# Research text")
            self.assertEqual(page_payload["adjudicated_markdown"], "# Reviewed text")
            self.assertIn("canonical_validation", page_payload)
            self.assertTrue(PublicationBuilder.verify(output)["valid"])

            model_output = root / "bundle-model"
            PublicationBuilder(config).build(
                Scope("lok_sabha", "18", "8"),
                model_output,
                minimum_pdf_saving_percent=100,
                canonical_policy="model",
            )
            with tarfile.open(model_output / "webdataset" / "shard-00000.tar") as archive:
                canonical = archive.extractfile(f"{item.sha256}.md").read().decode("utf-8")
            self.assertEqual(canonical, "# Reviewed text")

            compact_output = root / "bundle-compact"
            PublicationBuilder(config).build(
                Scope("lok_sabha", "18", "8"), compact_output,
                minimum_pdf_saving_percent=100, compact=True, complete_session=True,
            )
            compact_manifest = json.loads(
                (compact_output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertIsNone(compact_manifest["path"])
            self.assertEqual(compact_manifest["webdataset_key"], item.sha256)
            self.assertFalse((compact_output / "documents").exists())
            with tarfile.open(compact_output / "webdataset" / "shard-00000.tar") as archive:
                self.assertEqual(
                    archive.extractfile(f"{item.sha256}.original.pdf").read(),
                    source.read_bytes(),
                )
            self.assertTrue(PublicationBuilder.verify(compact_output)["valid"])
            model_transcript = review_dir / "adjudicated.md"
            model_transcript.unlink()
            with self.assertRaisesRegex(RuntimeError, "model transcript missing"):
                PublicationBuilder(config).build(
                    Scope("lok_sabha", "18", "8"), root / "bundle-missing-model",
                    compact=True, complete_session=True,
                )
            model_transcript.write_text("  ", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "model transcript empty"):
                PublicationBuilder(config).build(
                    Scope("lok_sabha", "18", "8"), root / "bundle-empty-model",
                    compact=True, complete_session=True,
                )
            model_transcript.write_text("# Reviewed text", encoding="utf-8")
            readable_pdf = readable / "original.pdf"
            readable_pdf.write_bytes(bytes([source.read_bytes()[0] ^ 1]) + source.read_bytes()[1:])
            readable_relative = readable_pdf.relative_to(output).as_posix()
            readable_checksums = output / "SHA256SUMS"
            readable_checksums.write_text(
                "\n".join(
                    f"{sha256_file(readable_pdf)}  {readable_relative}"
                    if line.endswith(f"  {readable_relative}") else line
                    for line in readable_checksums.read_text(encoding="utf-8").splitlines()
                ) + "\n", encoding="utf-8",
            )
            readable_verification = PublicationBuilder.verify(output)
            self.assertFalse(readable_verification["valid"])
            self.assertIn(
                f"{manifest[0]['path']}/original.pdf",
                [failure["path"] for failure in readable_verification["failures"]],
            )
            with self.assertRaisesRegex(ValueError, "retain the original PDFs"):
                PublicationBuilder(config).build(
                    Scope("lok_sabha", "18", "8"), root / "without-originals",
                    include_raw=False,
                )

            shard = compact_output / "webdataset" / "shard-00000.tar"
            altered = shard.with_name("altered.tar")
            with tarfile.open(shard) as original, tarfile.open(altered, "w") as target:
                for member in original:
                    payload = original.extractfile(member).read()
                    if member.name == f"{item.sha256}.original.pdf":
                        payload = bytes([payload[0] ^ 1]) + payload[1:]
                    target.addfile(member, io.BytesIO(payload))
            altered.replace(shard)
            checksum_file = compact_output / "SHA256SUMS"
            lines = checksum_file.read_text(encoding="utf-8").splitlines()
            checksum_file.write_text(
                "\n".join(
                    f"{sha256_file(shard)}  webdataset/shard-00000.tar"
                    if line.endswith("  webdataset/shard-00000.tar") else line
                    for line in lines
                ) + "\n", encoding="utf-8",
            )
            mismatch = PublicationBuilder.verify(compact_output)
            self.assertFalse(mismatch["valid"])
            pdf_failures = [failure for failure in mismatch["failures"]
                            if failure["path"] ==
                            f"webdataset/shard-00000.tar:{item.sha256}.original.pdf"]
            self.assertEqual(len(pdf_failures), 1)
            self.assertEqual(pdf_failures[0]["error"], "SHA-256 mismatch")
            self.assertEqual(pdf_failures[0]["expected"], item.sha256)
            self.assertNotEqual(pdf_failures[0]["actual"], item.sha256)
            with tarfile.open(shard) as original, tarfile.open(altered, "w") as target:
                for member in original:
                    if member.name != f"{item.sha256}.original.pdf":
                        target.addfile(member, original.extractfile(member))
            altered.replace(shard)
            checksum_file = compact_output / "SHA256SUMS"
            lines = checksum_file.read_text(encoding="utf-8").splitlines()
            checksum_file.write_text(
                "\n".join(
                    f"{sha256_file(shard)}  webdataset/shard-00000.tar"
                    if line.endswith("  webdataset/shard-00000.tar") else line
                    for line in lines
                ) + "\n", encoding="utf-8",
            )
            verification = PublicationBuilder.verify(compact_output)
            self.assertFalse(verification["valid"])
            self.assertIn(
                f"webdataset/shard-00000.tar:{item.sha256}.original.pdf",
                [failure["path"] for failure in verification["failures"]],
            )


class NamingAndValidationTests(unittest.TestCase):
    def test_standalone_page_footer_does_not_flag_content_numbers(self):
        local = "The allocation was 2380.86."
        model = "The allocation was 2380.86.\n\n**Page 2 of 2**"
        self.assertNotEqual(numbers(local), numbers(model))
        self.assertEqual(content_numbers(local), content_numbers(model))
        self.assertNotIn(
            "candidate-numeric-disagreement",
            text_flags(model, reference=local, config=ValidationConfig()),
        )
        self.assertIn(
            "candidate-numeric-disagreement",
            text_flags(model.replace("2380.86", "2380.87"), reference=local,
                       config=ValidationConfig()),
        )

    def test_remote_bundle_verifies_original_and_text_content_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "documents").mkdir()
            pdf = bundle / "documents" / "original.pdf"
            text = bundle / "documents" / "document.txt"
            pdf.write_bytes(b"%PDF-1.7\noriginal")
            text.write_text("extracted text", encoding="utf-8")
            entries = [
                SimpleNamespace(path="data/tranche/documents/original.pdf",
                                size=pdf.stat().st_size,
                                lfs={"sha256": sha256_file(pdf)}, blob_id="pointer"),
                SimpleNamespace(path="data/tranche/documents/document.txt",
                                size=text.stat().st_size, lfs=None,
                                blob_id=_git_blob_id(text)),
            ]
            self.assertTrue(compare_remote_bundle(bundle, "data/tranche", entries)["valid"])
            entries[0].lfs = {"sha256": "wrong"}
            self.assertFalse(compare_remote_bundle(bundle, "data/tranche", entries)["valid"])
            entries.pop()
            self.assertEqual(
                compare_remote_bundle(bundle, "data/tranche", entries)["failures"][-1]["error"],
                "LFS SHA-256 mismatch",
            )

    def test_document_slug_is_human_readable_and_hash_suffixed(self):
        record = {
            "source_type": "questions_answers",
            "document_date": "2026-02-11",
            "document_number": "1948",
            "title": "Biogas Projects in Rajasthan: State-wise details",
        }
        slug = document_slug(record, "3322680af96b5a593b748120e620fcd706bdad03dd0462d3884e76889c0eee4b")
        self.assertTrue(slug.startswith("2026-02-11_Q1948_biogas-projects-in-rajasthan"))
        self.assertTrue(slug.endswith("__3322680a"))
        self.assertNotIn(" ", slug)
        self.assertEqual(slugify("  "), "document")

    def test_text_flags_marks_numeric_disagreement(self):
        flags = text_flags(
            "Total 42 acres",
            reference="Total 4.2 acres",
            config=ValidationConfig(),
        )
        self.assertIn("candidate-numeric-disagreement", flags)
        self.assertEqual(
            text_flags("Total 42 acres", reference="Total 42 acres", config=ValidationConfig()),
            (),
        )

    def test_link_targets_do_not_double_count_visible_numbers(self):
        visible = "https://example.test/index1.php?level=1&id=795"
        linked = f"[{visible}]({visible})"
        self.assertEqual(
            text_flags(linked, reference=visible, config=ValidationConfig()),
            (),
        )
        self.assertEqual(
            text_flags("[Report 2024](https://example.test/file(7).pdf)",
                       reference="Report 2024", config=ValidationConfig()),
            (),
        )


if __name__ == "__main__":
    unittest.main()
