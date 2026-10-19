import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailcrawl.clock import as_of
from mailcrawl.paths import UsageError
from mailcrawl.sources import configured_sources, source_digest, source_status, sync_sources
from mailcrawl.store import connect as store_connect, init_account

ROOT = Path(__file__).resolve().parents[1]


class _ConnectionFault:
    def __init__(self, connection, *, fail_commit_at=None, fail_rollback=False):
        self.connection = connection
        self.fail_commit_at = fail_commit_at
        self.fail_rollback = fail_rollback
        self.commit_calls = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        self.commit_calls += 1
        if self.commit_calls == self.fail_commit_at:
            raise sqlite3.OperationalError("private commit failure")
        return self.connection.commit()

    def rollback(self):
        if self.fail_rollback:
            raise sqlite3.OperationalError("private rollback failure")
        return self.connection.rollback()


class LocalSourcesTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.home = self.root / "archive"
        init_account(self.home, "thehive", "ada@example.com")
        self.export = self.root / "export"
        shutil.copytree(ROOT / "testdata/fixtures/gog-export/native", self.export)
        self.config = self.root / "config.toml"
        self.config.write_text('[accounts.thehive]\nemail="ada@example.com"\n'
                               '[sources.daily]\naccount="thehive"\nkind="gog-export"\n'
                               'path="export"\nstale_after_seconds=60\n')

    def rows(self):
        conn = sqlite3.connect(self.home / "thehive.db")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def test_repeat_is_idempotent_and_receipt_is_local_freshness(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            first = sync_sources(self.home, self.config)
            second = sync_sources(self.home, self.config)
        self.assertTrue(first["ok"])
        self.assertEqual(first["sources"][0]["n_new"], 3)
        self.assertTrue(second["ok"])
        self.assertEqual(second["sources"][0]["n_new"], 0)
        self.assertEqual(second["sources"][0]["n_dup"], 3)
        conn = self.rows()
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
        spec = configured_sources(self.config)[0]
        status = source_status(conn, spec, as_of(None))
        self.assertEqual(status["state"], "current_local")
        self.assertTrue(status["local_only"])
        self.assertNotIn("ada@example.com", json.dumps(status))
        self.assertEqual(source_status(conn, spec, as_of(None) + 61)["state"], "stale")

    def test_missing_message_rolls_back_and_preserves_last_success(self):
        self.assertTrue(sync_sources(self.home, self.config)["ok"])
        conn = self.rows()
        before = conn.execute("SELECT value FROM cursors WHERE stream='local-export:daily'").fetchone()[0]
        index = self.export / "gmail/acct_synthetic/messages/index.jsonl"
        record = json.loads(index.read_text().splitlines()[0])
        record.update(id="new-missing-message", eml="gmail/acct_synthetic/messages/missing.eml")
        with index.open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        failed = sync_sources(self.home, self.config)
        self.assertFalse(failed["ok"])
        self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT value FROM cursors WHERE stream='local-export:daily'").fetchone()[0], before)
        self.assertEqual(source_status(conn, configured_sources(self.config)[0], as_of(None))["state"], "failed")

    def test_changed_source_configuration_requires_import(self):
        self.assertTrue(sync_sources(self.home, self.config)["ok"])
        self.config.write_text(self.config.read_text().replace('path="export"', 'path="another-export"'))
        spec = configured_sources(self.config)[0]
        self.assertEqual(source_status(self.rows(), spec, as_of(None))["state"], "stale")

    def test_configuration_cannot_launch_an_exporter(self):
        with self.config.open("a") as stream:
            stream.write('command="gog backup export"\n')
        with self.assertRaises(UsageError):
            configured_sources(self.config)

    def test_missing_database_is_not_created(self):
        home = self.root / "missing-archive"
        result = sync_sources(home, self.config)
        self.assertFalse(result["ok"])
        self.assertFalse(home.exists())

    def test_corrupt_account_does_not_stop_a_valid_second_source(self):
        personal = init_account(self.home, "personal", "private-personal@example.com")
        personal.write_bytes(b"corrupt-private-database")
        self.config.write_text(
            '[accounts.personal]\nemail="private-personal@example.com"\n'
            '[accounts.thehive]\nemail="ada@example.com"\n'
            '[sources.a_corrupt]\naccount="personal"\nkind="gog-export"\npath="export"\n'
            '[sources.b_valid]\naccount="thehive"\nkind="gog-export"\npath="export"\n'
        )

        result = sync_sources(self.home, self.config)
        rows = {row["id"]: row for row in result["sources"]}

        self.assertFalse(result["ok"])
        self.assertEqual(rows["a_corrupt"]["outcome"], "failed")
        self.assertEqual(rows["a_corrupt"]["error"], "import_failed")
        self.assertFalse(rows["a_corrupt"]["receipt_persisted"])
        self.assertNotIn("n_new", rows["a_corrupt"])
        self.assertEqual(rows["b_valid"]["outcome"], "success")
        self.assertEqual(rows["b_valid"]["n_new"], 3)
        self.assertEqual(personal.read_bytes(), b"corrupt-private-database")
        self.assertNotIn("private-personal@example.com", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_missing_account_config_is_sanitized_and_later_source_runs(self):
        init_account(self.home, "personal", "private-personal@example.com")
        self.config.write_text(
            '[accounts.thehive]\nemail="ada@example.com"\n'
            '[sources.a_missing_account]\naccount="personal"\nkind="gog-export"\npath="export"\n'
            '[sources.b_valid]\naccount="thehive"\nkind="gog-export"\npath="export"\n'
        )

        result = sync_sources(self.home, self.config)
        rows = {row["id"]: row for row in result["sources"]}

        self.assertFalse(result["ok"])
        self.assertEqual(rows["a_missing_account"]["error"], "import_failed")
        self.assertTrue(rows["a_missing_account"]["receipt_persisted"])
        self.assertNotIn("n_new", rows["a_missing_account"])
        self.assertEqual(rows["b_valid"]["outcome"], "success")
        personal_conn = sqlite3.connect(self.home / "personal.db")
        personal_conn.row_factory = sqlite3.Row
        self.addCleanup(personal_conn.close)
        self.assertEqual(
            source_status(
                personal_conn, configured_sources(self.config)[0], as_of(None)
            )["state"],
            "failed",
        )
        self.assertNotIn("private-personal@example.com", json.dumps(result))

    def test_failed_receipt_write_is_reported_without_leaking_exception(self):
        missing = self.root / "missing-export"
        self.config.write_text(
            '[accounts.thehive]\nemail="ada@example.com"\n'
            '[sources.daily]\naccount="thehive"\nkind="gog-export"\n'
            f'path={json.dumps(str(missing))}\n'
        )
        with patch(
            "mailcrawl.sources._receipt",
            side_effect=sqlite3.OperationalError("private receipt write failure"),
        ):
            result = sync_sources(self.home, self.config)

        report = result["sources"][0]
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["error"], "import_failed")
        self.assertFalse(report["receipt_persisted"])
        self.assertNotIn("private receipt write failure", json.dumps(result))

    def test_failed_receipt_commit_is_best_effort_and_sanitized(self):
        missing = self.root / "missing-export"
        self.config.write_text(
            '[accounts.thehive]\nemail="ada@example.com"\n'
            '[sources.daily]\naccount="thehive"\nkind="gog-export"\n'
            f'path={json.dumps(str(missing))}\n'
        )
        def fail_receipt_commit(path):
            return _ConnectionFault(store_connect(path), fail_commit_at=1)

        with patch("mailcrawl.sources.connect", side_effect=fail_receipt_commit):
            result = sync_sources(self.home, self.config)

        report = result["sources"][0]
        self.assertEqual(report["outcome"], "failed")
        self.assertFalse(report["receipt_persisted"])
        self.assertNotIn("private commit failure", json.dumps(result))

    def test_rollback_failure_is_sanitized_and_does_not_abort(self):
        missing = self.root / "missing-export"
        self.config.write_text(
            '[accounts.thehive]\nemail="ada@example.com"\n'
            '[sources.daily]\naccount="thehive"\nkind="gog-export"\n'
            f'path={json.dumps(str(missing))}\n'
        )
        def fail_rollback(path):
            return _ConnectionFault(store_connect(path), fail_rollback=True)

        with patch("mailcrawl.sources.connect", side_effect=fail_rollback):
            result = sync_sources(self.home, self.config)

        report = result["sources"][0]
        self.assertEqual(report["outcome"], "failed")
        self.assertFalse(report["receipt_persisted"])
        self.assertNotIn("private rollback failure", json.dumps(result))

    def test_committed_counts_are_not_reported_when_import_commit_fails(self):
        def fail_import_commit(path):
            return _ConnectionFault(store_connect(path), fail_commit_at=1)

        with patch("mailcrawl.sources.connect", side_effect=fail_import_commit):
            result = sync_sources(self.home, self.config)

        report = result["sources"][0]
        self.assertEqual(report["outcome"], "failed")
        self.assertTrue(report["receipt_persisted"])
        self.assertEqual(report["attempted_counts"]["n_new"], 3)
        self.assertNotIn("n_new", report)
        self.assertNotIn("private commit failure", json.dumps(result))
        self.assertEqual(
            self.rows().execute("SELECT count(*) FROM messages").fetchone()[0], 0
        )

    def test_cancellation_records_failed_attempt_then_reraises(self):
        self.assertTrue(sync_sources(self.home, self.config)["ok"])
        conn = self.rows()
        success_before = conn.execute(
            "SELECT value FROM cursors WHERE stream='local-export:daily'"
        ).fetchone()[0]

        for cancellation in (KeyboardInterrupt, SystemExit):
            with self.subTest(cancellation=cancellation.__name__):
                with patch(
                    "mailcrawl.sources.import_gog_export",
                    side_effect=cancellation("private cancellation detail"),
                ):
                    with self.assertRaises(cancellation):
                        sync_sources(self.home, self.config)

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM cursors WHERE stream='local-export:daily'"
                    ).fetchone()[0],
                    success_before,
                )
                attempt = conn.execute(
                    "SELECT value FROM cursors WHERE stream='local-export-attempt:daily'"
                ).fetchone()[0]
                self.assertEqual(json.loads(attempt)["status"], "failed")
                self.assertNotIn("private cancellation detail", attempt)

    def test_expected_source_requires_full_spec_match_before_import(self):
        expected = configured_sources(self.config)[0]
        self.config.write_text(self.config.read_text().replace(
            "stale_after_seconds=60", "stale_after_seconds=61"
        ))

        with self.assertRaisesRegex(UsageError, "configured source changed"):
            sync_sources(
                self.home,
                self.config,
                source_id=expected.id,
                account=expected.account,
                expected_source=expected,
            )

        self.assertEqual(
            self.rows().execute("SELECT count(*) FROM messages").fetchone()[0], 0
        )

    def test_failed_connection_setup_closes_its_owned_connection(self):
        from mailcrawl.store import connect
        connection = sqlite3.connect(":memory:")
        with patch("mailcrawl.store.sqlite3.connect", return_value=connection), \
                patch("mailcrawl.store.migrate", side_effect=sqlite3.DatabaseError("fixture")):
            with self.assertRaises(sqlite3.DatabaseError):
                connect(":memory:")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
