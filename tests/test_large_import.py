# SPDX-License-Identifier: MIT
"""Regression coverage for streaming large mbox imports and atomic callbacks."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from mailcrawl import inputio
from mailcrawl.ingest import _ingest, import_gog_export, import_mbox
from mailcrawl.mbox import MboxSource, Refused
from mailcrawl.store import connect, init_account


def _record(index: int) -> bytes:
    return (
        f"From {index + 1}@xxx Thu Jan 01 00:00:00 +0000 2026\n"
        f"Message-ID: <message-{index}@example.test>\n"
        "Date: Thu, 01 Jan 2026 00:00:00 +0000\n"
        f"Subject: Synthetic {index}\n"
        "From: sender@example.test\n"
        "\n"
        "synthetic body\n"
    ).encode()


def _mbox(count: int) -> bytes:
    return b"\n\n".join(_record(index).rstrip(b"\n") for index in range(count)) + b"\n"


class LargeImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mbox = self.root / "synthetic.mbox"
        self.db = init_account(self.root / "home", "personal", "ada@example.test")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def import_file(self, **kwargs):
        with closing(connect(self.db)) as conn:
            return import_mbox(
                conn,
                alias="personal",
                mbox_path=self.mbox,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.test",
                **kwargs,
            )

    def _write_fixture_export(self, records: list[dict[str, object]]) -> Path:
        export = self.root / "fixture-export"
        export.mkdir()
        (export / "export.json").write_text(
            json.dumps(
                {
                    "schema": "mailcrawl.gog-export.v0",
                    "example_manifest": {"messages": len(records)},
                    "example_messages": records,
                }
            ),
            encoding="utf-8",
        )
        return export

    @staticmethod
    def _fixture_record(index: int, body: str = "synthetic inline body") -> dict[str, object]:
        return {
            "example_message_id": f"fixture-{index}",
            "example_thread_id": f"thread-{index}",
            "example_label_ids": [],
            "example_date": 1760054400,
            "example_body": body,
        }

    def test_default_mbox_policy_does_not_reapply_legacy_archive_caps(self) -> None:
        self.mbox.write_bytes(_mbox(2))
        with patch.object(inputio, "MAX_IMPORT_BYTES", 1), patch.object(
            inputio, "MAX_IMPORT_RECORDS", 1
        ), patch.object(inputio, "MAX_IMPORT_IMAGE_BYTES", 1), patch.object(
            inputio, "MAX_IMPORT_EXTRACT_BYTES", 1
        ):
            result = self.import_file()
        self.assertEqual((result.n_seen, result.n_new, result.n_refused), (2, 2, 0))

    def test_explicit_record_work_limit_aborts_whole_mbox_transaction(self) -> None:
        self.mbox.write_bytes(_mbox(2))
        limits = inputio.ImportLimits(max_records=1)
        with closing(connect(self.db)) as conn:
            with self.assertRaises(inputio.ImportExhausted):
                import_mbox(
                    conn,
                    alias="personal",
                    mbox_path=self.mbox,
                    labels_path=None,
                    taxonomy_path=None,
                    account_email="ada@example.test",
                    limits=limits,
                )
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM import_runs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM ledger").fetchone()[0], 0)

    def test_explicit_byte_work_limit_aborts_before_reading_mbox(self) -> None:
        self.mbox.write_bytes(_mbox(1))
        limits = inputio.ImportLimits(max_total_bytes=1)
        with closing(connect(self.db)) as conn:
            with self.assertRaises(inputio.ImportExhausted):
                import_mbox(
                    conn,
                    alias="personal",
                    mbox_path=self.mbox,
                    labels_path=None,
                    taxonomy_path=None,
                    account_email="ada@example.test",
                    limits=limits,
                )
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_limits_reject_nonpositive_and_boolean_values(self) -> None:
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                inputio.ImportLimits(max_records=value)

    def test_inline_fixture_bytes_obey_lowered_aggregate_budget(self) -> None:
        records = [self._fixture_record(index, "x" * 256) for index in range(2)]
        export = self._write_fixture_export(records)
        first_record_bytes = len(json.dumps(records[0], ensure_ascii=False).encode())
        limits = inputio.ImportLimits(
            max_total_bytes=first_record_bytes + 1,
            max_records=inputio.DEFAULT_GOG_IMPORT_LIMITS.max_records,
            max_image_bytes=inputio.DEFAULT_GOG_IMPORT_LIMITS.max_image_bytes,
            max_extract_bytes=inputio.DEFAULT_GOG_IMPORT_LIMITS.max_extract_bytes,
        )
        with closing(connect(self.db)) as conn:
            with self.assertRaises(inputio.ImportExhausted):
                import_gog_export(
                    conn,
                    alias="personal",
                    export_dir=export,
                    taxonomy_path=None,
                    account_email="ada@example.test",
                    limits=limits,
                )
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM import_runs").fetchone()[0], 0)

    def test_fixture_explicit_record_override_supersedes_small_baseline(self) -> None:
        from dataclasses import replace

        records = [self._fixture_record(index) for index in range(3)]
        export = self._write_fixture_export(records)
        limits = replace(inputio.DEFAULT_GOG_IMPORT_LIMITS, max_records=len(records))
        with patch.object(inputio, "MAX_IMPORT_RECORDS", 1), closing(connect(self.db)) as conn:
            result = import_gog_export(
                conn,
                alias="personal",
                export_dir=export,
                taxonomy_path=None,
                account_email="ada@example.test",
                limits=limits,
            )
            self.assertEqual((result.n_seen, result.n_new), (3, 3))
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 3)

    def test_input_file_growth_during_scan_aborts(self) -> None:
        self.mbox.write_bytes(_mbox(2))
        source = MboxSource(self.mbox, {})
        items = iter(source.items())
        first = next(items)
        self.assertFalse(isinstance(first, Refused))
        with self.mbox.open("ab") as handle:
            handle.write(b"growth")
        with self.assertRaises(inputio.ImportExhausted):
            list(items)

    def test_same_size_overwrite_during_scan_aborts(self) -> None:
        self.mbox.write_bytes(_mbox(2))
        initial = self.mbox.stat()
        source = MboxSource(self.mbox, {})
        items = iter(source.items())
        first = next(items)
        self.assertFalse(isinstance(first, Refused))

        with self.mbox.open("r+b") as handle:
            handle.seek(0)
            handle.write(b"X")
            handle.flush()
        os.utime(
            self.mbox,
            ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000),
        )
        changed = self.mbox.stat()
        self.assertEqual(changed.st_size, initial.st_size)
        self.assertGreater(changed.st_mtime_ns, initial.st_mtime_ns)

        with self.assertRaises(inputio.ImportExhausted):
            list(items)

    def test_success_callback_sees_repair_and_writes_inside_import_commit(self) -> None:
        self.mbox.write_bytes(
            _record(1).replace(
                b"Message-ID:",
                b"X-Gmail-Labels: LocalLabel\nX-GM-THRID: 123\nMessage-ID:",
                1,
            )
        )
        observed = []
        with closing(connect(self.db)) as conn:
            def checkpoint(result):
                self.assertEqual(conn.execute("SELECT count(*) FROM import_runs").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM ledger").fetchone()[0], 1)
                self.assertIn("LocalLabel", result.takeout_unmapped)
                conn.execute(
                    "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (1, 'local-export:test', ?, 1)",
                    (json.dumps({"messages": result.n_new}),),
                )
                observed.append(result)

            result = import_mbox(
                conn,
                alias="personal",
                mbox_path=self.mbox,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.test",
                on_success=checkpoint,
            )
            checkpoint_value = conn.execute(
                "SELECT value FROM cursors WHERE stream = 'local-export:test'"
            ).fetchone()[0]
            self.assertEqual(checkpoint_value, '{"messages": 1}')
        self.assertEqual(observed, [result])

    def test_success_callback_failure_rolls_back_source_and_checkpoint(self) -> None:
        self.mbox.write_bytes(_mbox(1))
        with closing(connect(self.db)) as conn:
            def fail(_result):
                conn.execute(
                    "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (1, 'local-export:test', '{}', 1)"
                )
                raise KeyboardInterrupt("cancelled")

            with self.assertRaises(KeyboardInterrupt):
                import_mbox(
                    conn,
                    alias="personal",
                    mbox_path=self.mbox,
                    labels_path=None,
                    taxonomy_path=None,
                    account_email="ada@example.test",
                    on_success=fail,
                )
            for table in ("messages", "import_runs", "ledger", "cursors"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_gog_success_callback_failure_rolls_back_import(self) -> None:
        native = Path(__file__).resolve().parents[1] / "testdata/fixtures/gog-export/native"
        with closing(connect(self.db)) as conn:
            def fail(_result):
                conn.execute(
                    "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (1, 'local-export:test', '{}', 1)"
                )
                raise KeyboardInterrupt("cancelled")

            with self.assertRaises(KeyboardInterrupt):
                import_gog_export(
                    conn,
                    alias="personal",
                    export_dir=native,
                    taxonomy_path=None,
                    account_email="ada@example.test",
                    on_success=fail,
                )
            for table in ("messages", "import_runs", "ledger", "cursors"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_refusal_ledger_samples_are_bounded_and_counts_are_exact(self) -> None:
        with closing(connect(self.db)) as conn:
            result = _ingest(
                conn,
                account_id=1,
                alias="personal",
                source="gog-export",
                items=(Refused(index, "synthetic") for index in range(150)),
                ref=lambda: "synthetic-ref",
                run_json=None,
                catalogue={},
                account_email="ada@example.test",
            )
            detail = json.loads(conn.execute("SELECT detail_json FROM ledger").fetchone()[0])
        self.assertEqual((result.n_seen, result.n_refused), (150, 150))
        self.assertEqual(detail["n_refused"], 150)
        self.assertEqual(len(detail["refused"]), 100)

    @unittest.skipUnless(
        os.environ.get("MAILCRAWL_LARGE_PROOF") == "1",
        "set MAILCRAWL_LARGE_PROOF=1 to scan a generated >512 MiB sparse mbox",
    )
    def test_generated_mbox_larger_than_legacy_cap_streams(self) -> None:
        with self.mbox.open("wb") as handle:
            handle.write(_record(1))
            handle.write(
                b"\nFrom 2@xxx Thu Jan 01 00:00:00 +0000 2026\n"
                b"Message-ID: <oversized@example.test>\nSubject: padded\n\n"
            )
            handle.truncate((512 << 20) + 1)
        source = MboxSource(self.mbox, {})
        parsed = list(source.items())
        self.assertGreater(self.mbox.stat().st_size, inputio.MAX_IMPORT_BYTES)
        self.assertEqual(sum(not isinstance(item, Refused) for item in parsed), 1)
        self.assertTrue(any(isinstance(item, Refused) for item in parsed))


if __name__ == "__main__":
    unittest.main()
