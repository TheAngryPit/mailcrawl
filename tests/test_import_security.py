# SPDX-License-Identifier: MIT
"""Synthetic import boundary checks with deliberately small resource limits."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mailcrawl import inputio
from mailcrawl.contract import AttachmentRec, ExportRejected, _read_raw
from mailcrawl.eml import eml_record, parse_bytes
from mailcrawl.extract import PdftotextBackend
from mailcrawl.gognative import ExportInvalid, _label_ids, load_native
from mailcrawl.ingest import _ingest, _keep_image, import_gog_export, import_mbox
from mailcrawl.mbox import Refused
from mailcrawl.store import connect, init_account

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "testdata/fixtures/gog-export/native"
FIXTURE = ROOT / "testdata/fixtures/gog-export/v0"


class InputBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def native(self):
        target = self.root / "export"
        shutil.copytree(NATIVE, target)
        return target

    def test_regular_size_and_symlink_checks_use_open_fd(self):
        (self.root / "file").write_bytes(b"abcdef")
        (self.root / "link").symlink_to(self.root / "file")
        with inputio.SafeRoot(self.root) as reader:
            with self.assertRaises(inputio.InputLimit):
                reader.read("file", 3)
            with self.assertRaises(inputio.UnsafeInput):
                reader.read("link", 99)
            with self.assertRaises(inputio.UnsafeInput):
                reader.read("../file", 99)
            with reader.open("file") as handle:
                (self.root / "file").rename(self.root / "old")
                (self.root / "file").symlink_to(self.root / "old")
                self.assertEqual(inputio.bounded_read(handle, 99), b"abcdef")

    def test_open_root_is_pinned_through_root_replacement(self):
        base = self.root / "base"
        base.mkdir()
        (base / "file").write_bytes(b"inside")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"outside")
        with inputio.SafeRoot(base) as reader:
            base.rename(self.root / "old-base")
            base.symlink_to(outside)
            self.assertEqual(reader.read("file", 99), b"inside")

    def test_intermediate_symlink_and_fifo_are_refused(self):
        (self.root / "real").mkdir()
        (self.root / "real/file").write_bytes(b"x")
        (self.root / "link").symlink_to(self.root / "real")
        os.mkfifo(self.root / "fifo")
        with inputio.SafeRoot(self.root) as reader:
            for name in ("link/file", "fifo"):
                with self.assertRaises(inputio.UnsafeInput):
                    reader.read(name, 99)

    def test_fixture_raw_symlink_is_rejected(self):
        (self.root / "actual.eml").write_bytes(b"Subject: secret\n\nbody")
        (self.root / "raw.eml").symlink_to(self.root / "actual.eml")
        with self.assertRaises(ExportRejected):
            _read_raw("raw.eml", self.root)

    def test_native_metadata_symlinks_are_rejected(self):
        for name in ("labels.json", "messages/index.jsonl"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as folder:
                target = Path(folder) / "export"
                shutil.copytree(NATIVE, target)
                metadata = target / "gmail/acct_synthetic" / name
                other = Path(folder) / "outside"
                metadata.rename(other)
                metadata.symlink_to(other)
                with self.assertRaises(ExportInvalid):
                    load_native(target)

    def test_duplicate_native_labels_are_rejected(self):
        with self.assertRaises(ExportInvalid):
            _label_ids(["INBOX", "INBOX"], "synthetic")

    def test_native_message_is_lazy_and_identical_references_reuse_parse(self):
        target = self.native()
        index = target / "gmail/acct_synthetic/messages/index.jsonl"
        first = index.read_text().splitlines()[0]
        item = json.loads(first)
        second = dict(item, id="other-id")
        index.write_text(first + "\n" + json.dumps(second) + "\n" + json.dumps(second) + "\n")
        import mailcrawl.gognative as native
        actual = native.eml_record
        with patch.object(native, "eml_record", wraps=actual) as parse:
            document, _sha = load_native(target)
            self.assertEqual(parse.call_count, 0)
            messages = list(document.messages)
            self.assertEqual(len(messages), 3)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(messages[1].gmail_message_id, "other-id")

    def test_message_limits_precede_email_parser(self):
        with patch.object(inputio, "MAX_MESSAGE_BYTES", 8), patch("mailcrawl.eml.LimitedFeedParser") as parser:
            with self.assertRaises(inputio.InputLimit):
                parse_bytes(b"Subject: huge\n\nbody")
            parser.assert_not_called()

    def test_mime_part_depth_and_decode_budgets(self):
        message = EmailMessage()
        message.set_content("body")
        message.add_attachment(b"abcdef", maintype="application", subtype="octet-stream", filename="a")
        raw = message.as_bytes()
        for constant, threshold in (("MAX_MIME_PARTS", 1), ("MAX_MIME_DEPTH", 1), ("MAX_DECODED_BYTES", 3)):
            with self.subTest(constant=constant), patch.object(inputio, constant, threshold):
                with self.assertRaises(inputio.InputLimit):
                    eml_record(raw, gmail_message_id="x", gmail_thread_id="t", label_ids=(), internal_date=0, raw_path="synthetic")

    def test_native_oversized_message_is_refused_and_next_is_processed(self):
        target = self.native()
        with patch.object(inputio, "MAX_MESSAGE_BYTES", 500):
            document, _sha = load_native(target)
            items = list(document.messages)
        self.assertTrue(any(isinstance(x, Refused) for x in items))
        self.assertTrue(any(not isinstance(x, Refused) for x in items))

    def test_manifest_and_index_limits(self):
        target = self.native()
        with patch.object(inputio, "MAX_INDEX_BYTES", 8):
            with self.assertRaises(inputio.InputLimit):
                load_native(target)
        db = init_account(self.root / "home", "personal", "ada@example.com")
        with connect(db) as conn, patch.object(inputio, "MAX_MANIFEST_BYTES", 8):
            with self.assertRaises(inputio.InputLimit):
                import_gog_export(conn, alias="personal", export_dir=FIXTURE, taxonomy_path=None, account_email="ada@example.com")
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_mbox_oversized_entry_is_refused_and_next_is_processed(self):
        mbox = self.root / "test.mbox"
        mbox.write_bytes(b"From 1@xxx Thu Jan 01 00:00:00 +0000 2026\nSubject: large\n\n" + b"x" * 300 + b"\n\nFrom 2@xxx Thu Jan 01 00:00:00 +0000 2026\nSubject: okay\n\nokay\n")
        db = init_account(self.root / "home", "personal", "ada@example.com")
        with connect(db) as conn, patch.object(inputio, "MAX_MESSAGE_BYTES", 150):
            result = import_mbox(conn, alias="personal", mbox_path=mbox, labels_path=None, taxonomy_path=None, account_email="ada@example.com")
            self.assertEqual((result.n_new, result.n_refused), (1, 1))

    def test_image_rollback_only_removes_new_blobs(self):
        db = init_account(self.root / "home", "personal", "ada@example.com")
        with connect(db) as conn:
            old = AttachmentRec("old.png", "image/png", 3, None, image_bytes=b"old")
            old_path = Path(_keep_image(conn, old))
            message = eml_record(b"Subject: synthetic\n\nbody", gmail_message_id="x", gmail_thread_id="t", label_ids=(), internal_date=0, raw_path="test")
            from dataclasses import replace
            message = replace(message, attachments=(AttachmentRec("new.png", "image/png", 3, None, image_bytes=b"new"), old))
            def stream():
                yield message
                raise KeyboardInterrupt("synthetic cancellation")
            with self.assertRaises(KeyboardInterrupt):
                _ingest(conn, account_id=1, alias="personal", source="gog-export", items=stream(), ref=lambda: "sha", run_json=None, catalogue={}, account_email="ada@example.com")
            self.assertTrue(old_path.is_file())
            self.assertFalse((old_path.parent / hashlib.sha256(b"new").hexdigest()).exists())
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_fixture_aggregate_exhaustion_stops_reads_and_rolls_back(self):
        from mailcrawl.contract import _fixture_messages
        items = []
        for index in range(5):
            name = f"{index}.eml"
            (self.root / name).write_bytes(b"Subject: synthetic\n\n" + b"x" * 492)
            items.append({"example_message_id": str(index), "example_thread_id": "t", "example_label_ids": [], "example_date": 1760054400, "example_raw_file": name})
        db = init_account(self.root / "home", "personal", "ada@example.com")
        two_record_bytes = sum(
            len(json.dumps(item, ensure_ascii=False).encode())
            + (self.root / item["example_raw_file"]).stat().st_size
            for item in items[:2]
        ) + 1
        actual_read = inputio.bounded_read
        with connect(db) as conn, patch.object(inputio, "bounded_read", wraps=actual_read) as read:
            with self.assertRaises(inputio.InputLimit):
                _ingest(conn, account_id=1, alias="personal", source="gog-export", items=_fixture_messages(items, self.root, limits=inputio.ImportLimits(max_total_bytes=two_record_bytes)), ref=lambda: "sha", run_json=None, catalogue={}, account_email="ada@example.com", limits=inputio.ImportLimits(max_total_bytes=two_record_bytes))
            self.assertEqual(read.call_count, 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_native_aggregate_work_budget_stops_before_next_read(self):
        target = self.native()
        document, _sha = load_native(target, limits=inputio.ImportLimits(max_total_bytes=600))
        actual = inputio.bounded_read
        with patch.object(inputio, "bounded_read", wraps=actual) as read:
            with self.assertRaises(inputio.ImportExhausted):
                list(document.messages)
            self.assertEqual(read.call_count, 1)

    def test_header_and_physical_line_limits(self):
        for constant in ("MAX_HEADER_BYTES", "MAX_LINE_BYTES"):
            with self.subTest(constant=constant), patch.object(inputio, constant, 10):
                with self.assertRaises(inputio.InputLimit):
                    parse_bytes(b"Subject: oversized header\n\nbody")

    def test_per_import_image_budget_rolls_back_instead_of_skipping_later_record(self):
        from dataclasses import replace
        db = init_account(self.root / "home", "personal", "ada@example.com")
        first = eml_record(b"Subject: first\n\nbody", gmail_message_id="1", gmail_thread_id="t", label_ids=(), internal_date=0, raw_path="test")
        first = replace(first, attachments=(AttachmentRec("a.png", "image/png", 4, None, image_bytes=b"aaaa"),))
        second = replace(first, gmail_message_id="2", attachments=(AttachmentRec("b.png", "image/png", 4, None, image_bytes=b"bbbb"),))
        with connect(db) as conn:
            with self.assertRaises(inputio.ImportExhausted):
                _ingest(
                    conn,
                    account_id=1,
                    alias="personal",
                    source="gog-export",
                    items=iter((first, second)),
                    ref=lambda: "sha",
                    run_json=None,
                    catalogue={},
                    account_email="ada@example.com",
                    limits=inputio.ImportLimits(max_image_bytes=6),
                )
            self.assertEqual(conn.execute("SELECT count(*) FROM attachments").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_cumulative_extraction_budget(self):
        from mailcrawl.extract import parse_attachment
        token = inputio.import_budget.set(inputio.ImportBudget(limits=inputio.ImportLimits(max_extract_bytes=5)))
        try:
            self.assertEqual(parse_attachment("text/plain", b"abc").extract_status, "ok")
            with self.assertRaises(inputio.ImportExhausted):
                parse_attachment("text/plain", b"abc")
        finally:
            inputio.import_budget.reset(token)

    def test_mbox_held_blank_refusal_does_not_feed_discarded_parser(self):
        from mailcrawl.mbox import _Entry
        with patch.object(inputio, "MAX_HEADER_BYTES", 8), patch("mailcrawl.mbox._FEED_CHUNK", 4):
            entry = _Entry(0, b"From 1@xxx\n", lambda key: False)
            entry.add(b"X: 123\r\n")
            entry.add(b"\r\n")
            entry.add(b"body\r\n")
            with self.assertRaises(inputio.InputLimit):
                entry.finish()
        path = self.root / "held-blank.mbox"
        path.write_bytes(b"From 1@xxx Thu Jan 01 00:00:00 +0000 2026\nX: 123\n\nbody\n\nFrom 2@xxx Thu Jan 01 00:00:00 +0000 2026\n\nokay\n")
        db = init_account(self.root / "home", "personal", "ada@example.com")
        with connect(db) as conn, patch.object(inputio, "MAX_HEADER_BYTES", 8), patch("mailcrawl.mbox._FEED_CHUNK", 4):
            result = import_mbox(conn, alias="personal", mbox_path=path, labels_path=None, taxonomy_path=None, account_email="ada@example.com")
            self.assertEqual((result.n_new, result.n_refused), (1, 1))

    def test_pdf_output_and_stderr_caps_reap_child(self):
        import subprocess
        actual = subprocess.Popen
        for stream, constant in (("stdout", "MAX_EXTRACT_BYTES"), ("stderr", "MAX_EXTRACT_STDERR_BYTES")):
            def launch(_args, **kwargs):
                return actual([sys.executable, "-c", f"import sys; sys.{stream}.buffer.write(b'x'*1000)"], **kwargs)
            with self.subTest(stream=stream), patch("mailcrawl.extract.subprocess.Popen", side_effect=launch), patch.object(inputio, constant, 10):
                with self.assertRaises(inputio.InputLimit):
                    PdftotextBackend().read("pdf", b"synthetic")


if __name__ == "__main__":
    unittest.main()
