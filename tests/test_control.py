# SPDX-License-Identifier: MIT
"""Read-only control output exposes health without archive contents."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mailcrawl.cli import main
from mailcrawl.clock import as_of
from mailcrawl.control import doctor, metadata, readonly_archive, status
from mailcrawl.paths import UsageError
from mailcrawl.schema import SCHEMA_SQL, USER_VERSION
from mailcrawl.sources import configured_sources, source_digest
from mailcrawl.store import init_account


def _file_state(
    paths: tuple[Path, ...],
) -> dict[str, tuple[str, int, int, int, int] | None]:
    state = {}
    for path in paths:
        if not path.exists():
            state[path.name] = None
            continue
        info = path.stat()
        state[path.name] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            info.st_mode & 0o777,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
    return state


def _database_paths(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    )


def _healthy_database(home: Path, alias: str = "personal") -> Path:
    path = init_account(home, alias, "private-person@example.test")
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {USER_VERSION}")
        account_id = conn.execute(
            "SELECT account_id FROM accounts WHERE alias = ?", (alias,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO import_runs(account_id, source, finished_at, n_seen, n_new, n_dup, n_error, raw_json) "
            "VALUES (?, 'mbox', 100, 4, 3, 1, 0, ?)",
            (
                account_id,
                json.dumps(
                    {
                        "subject": "private-subject-value",
                        "body": "private-body-value",
                        "token": "private-token-value",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO import_runs(account_id, source, finished_at, n_seen, n_new, n_dup, n_error) "
            "VALUES (?, 'in-progress', NULL, 1000, 1000, 0, 99)",
            (account_id,),
        )
        conn.execute(
            "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (?, 'mbox', 'fixture', 4000000000)",
            (account_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return path


class ControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "data"
        self.home.mkdir()
        self.config = self.root / "config.toml"
        self.config.write_text(
            '[accounts.personal]\nemail = "private-person@example.test"\n'
            '[inference.llm]\napi_key = "private-config-token"\n',
            encoding="utf-8",
        )
        os.chmod(self.config, 0o600)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_metadata_advertises_only_local_implemented_capabilities(self) -> None:
        manifest = metadata()

        self.assertEqual(manifest["schema_version"], "crawlkit.control.v1")
        self.assertEqual(manifest["binary"], {"name": "mailcrawl"})
        self.assertEqual(
            manifest["capabilities"], ["status", "doctor", "sync", "search"]
        )
        self.assertEqual(
            manifest["commands"]["metadata"]["argv"],
            ["mailcrawl", "metadata", "--json"],
        )
        self.assertEqual(
            manifest["commands"]["search"]["argv"],
            ["mailcrawl", "--json", "search", "--account", "{config:account}", "--read-only"],
        )
        self.assertEqual(
            manifest["config_options"][0]["default_value"], "personal"
        )
        self.assertEqual(
            manifest["config_options"][0]["choices"], ["personal", "thehive", "vcl"]
        )
        self.assertTrue(manifest["commands"]["sync"]["mutates"])
        self.assertNotIn("publish", manifest["commands"])
        self.assertTrue(manifest["privacy"]["contains_private_messages"])

    def test_readonly_search_skips_inference_and_preserves_database_files(self) -> None:
        path = _healthy_database(self.home)
        os.chmod(path, 0o640)
        before = _file_state(_database_paths(path) + (self.config,))
        stdout = io.StringIO()

        with (
            mock.patch(
                "mailcrawl.cli.inference_config",
                side_effect=AssertionError("read-only search loaded inference config"),
            ),
            mock.patch(
                "mailcrawl.cli._open_embedder",
                side_effect=AssertionError("read-only search started an embedder"),
            ),
            mock.patch(
                "mailcrawl.cli.open_client",
                side_effect=AssertionError("read-only search started a reranker"),
            ),
            redirect_stdout(stdout),
        ):
            code = main(
                [
                    "--json", "--home", str(self.home), "--config", str(self.config),
                    "search", "needle", "--account", "personal", "--read-only",
                ]
            )

        self.assertEqual(code, 0)
        document = json.loads(stdout.getvalue())
        self.assertTrue(document["read_only"])
        self.assertEqual(document["hits"], [])
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)

    def test_readonly_search_rejects_old_schema_without_migration_or_chmod(self) -> None:
        path = self.home / "personal.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE old_data(value TEXT)")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        conn.close()
        os.chmod(path, 0o640)
        before = _file_state(_database_paths(path) + (self.config,))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "--json", "--home", str(self.home), "--config", str(self.config),
                    "search", "needle", "--account", "personal", "--read-only",
                ]
            )

        self.assertEqual(code, 2)
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["error"], "usage")
        self.assertEqual(document["detail"], "database schema is incompatible")
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)
        check = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 10)
        finally:
            check.close()

    def test_readonly_search_refuses_a_nonempty_wal_without_touching_it(self) -> None:
        path = _healthy_database(self.home)
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "UPDATE accounts SET email_hash = 'changed' WHERE alias = 'personal'"
        )
        writer.commit()
        wal = Path(str(path) + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        before = _file_state(_database_paths(path) + (self.config,))
        stdout = io.StringIO()
        try:
            with redirect_stdout(stdout):
                code = main(
                    [
                        "--json", "--home", str(self.home), "--config", str(self.config),
                        "search", "needle", "--account", "personal", "--read-only",
                    ]
                )
            self.assertEqual(code, 2)
            document = json.loads(stdout.getvalue())
            self.assertEqual(document["detail"], "database WAL requires maintenance")
            self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)
        finally:
            writer.close()

    def test_readonly_archive_rejects_a_file_change_during_search(self) -> None:
        path = _healthy_database(self.home)
        with self.assertRaisesRegex(UsageError, "database changed during read"):
            with readonly_archive(path, account="personal"):
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))

    def test_missing_config_and_database_are_reported_without_creation(self) -> None:
        missing_home = self.root / "not-created"
        missing_config = self.root / "missing.toml"

        result = status(missing_home, missing_config, "personal")

        self.assertEqual(result["state"], "needs_setup")
        self.assertEqual(
            {item["code"] for item in result["warnings"]},
            {"config_missing", "database_missing"},
        )
        self.assertFalse(missing_home.exists())
        self.assertFalse(missing_config.exists())

    def test_corrupt_database_is_reported_without_repair_or_path_leak(self) -> None:
        path = self.home / "personal.db"
        path.write_bytes(b"not a sqlite database")
        os.chmod(path, 0o640)
        before = _file_state(_database_paths(path) + (self.config,))

        result = doctor(self.home, self.config, "personal")

        self.assertEqual(result["state"], "needs_maintenance")
        self.assertIn(
            "database_corrupt", {item["code"] for item in result["warnings"]}
        )
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_old_schema_is_never_migrated(self) -> None:
        path = self.home / "personal.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE old_data(value TEXT)")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        conn.close()
        os.chmod(path, 0o640)
        before = _file_state(_database_paths(path) + (self.config,))

        result = doctor(self.home, self.config, "personal")

        self.assertEqual(result["state"], "needs_maintenance")
        self.assertIn(
            "database_schema_outdated",
            {item["code"] for item in result["warnings"]},
        )
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)
        check = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 10)
        finally:
            check.close()

    def test_stable_read_uses_import_completion_and_redacts_private_values(self) -> None:
        path = _healthy_database(self.home)
        before = _file_state(_database_paths(path) + (self.config,))

        result = status(self.home, self.config, "personal")
        rendered = json.dumps(result)

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["freshness"]["last_import_at"], 100)
        self.assertEqual(result["databases"][0]["freshness"]["last_import_at"], 100)
        self.assertEqual(
            result["databases"][0]["freshness"]["latest_run"],
            {"n_seen": 4, "n_new": 3, "n_dup": 1, "n_error": 0},
        )
        self.assertEqual(result["databases"][0]["freshness"]["import_errors_total"], 0)
        self.assertEqual(
            {row["id"]: row["value"] for row in result["counts"]}["messages"], 0
        )
        for private_value in (
            "private-person@example.test",
            "private-subject-value",
            "private-body-value",
            "private-token-value",
            "private-config-token",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)

    def test_multiple_accounts_can_include_an_empty_archive(self) -> None:
        _healthy_database(self.home)
        init_account(self.home, "thehive", "fixture@example.test")
        with self.config.open("a") as stream:
            stream.write('[accounts.thehive]\nemail="fixture@example.test"\n')
        result = status(self.home, self.config)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["freshness"]["last_import_at"], 100)

    def test_configured_source_freshness_uses_local_completion_receipt(self) -> None:
        path = _healthy_database(self.home)
        source_path = self.root / "takeout.mbox"
        self.config.write_text(
            '[accounts.personal]\nemail = "private-person@example.test"\n'
            '[sources.takeout]\n'
            'account = "personal"\n'
            'kind = "mbox"\n'
            f'path = {json.dumps(str(source_path))}\n',
            encoding="utf-8",
        )
        last_import = as_of(None) - 30
        spec_hash = source_digest(configured_sources(self.config)[0])
        conn = sqlite3.connect(path)
        try:
            account_id = conn.execute(
                "SELECT account_id FROM accounts WHERE alias = 'personal'"
            ).fetchone()[0]
            receipt = {
                "schema_version": "mailcrawl.refresh.v1",
                "status": "success",
                "last_import_at": last_import,
                "source_spec_sha256": spec_hash,
                "index_manifest_sha256": "fixture-manifest",
                "counts": {
                    "n_seen": 2,
                    "n_new": 2,
                    "n_error": 0,
                    "n_refused": 4,
                    "private_field": "do not expose",
                },
            }
            conn.execute(
                "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (?, ?, ?, ?)",
                (account_id, "local-export:takeout", json.dumps(receipt), 4000000000),
            )
            conn.execute(
                "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (?, ?, ?, ?)",
                (
                    account_id,
                    "local-export-attempt:takeout",
                    json.dumps({"status": "success", "last_attempt_at": last_import}),
                    4000000000,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        before = _file_state(_database_paths(path) + (self.config,))
        result = status(self.home, self.config, "personal")
        source = result["freshness"]["sources"][0]

        self.assertEqual(source["id"], "takeout")
        self.assertEqual(source["state"], "current_local")
        self.assertTrue(source["local_only"])
        self.assertEqual(source["last_import_at"], last_import)
        self.assertEqual(source["counts"]["n_refused"], 4)
        self.assertEqual(result["freshness"]["last_import_at"], last_import)
        self.assertNotIn(str(source_path), json.dumps(result))
        self.assertNotIn("do not expose", json.dumps(result))
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)

    def test_enabled_source_health_degrades_with_safe_warnings(self) -> None:
        path = _healthy_database(self.home)
        self.config.write_text(
            '[accounts.personal]\nemail = "private-person@example.test"\n'
            '[sources.stale]\naccount = "personal"\nkind = "mbox"\npath = "/tmp/stale.mbox"\n'
            '[sources.never]\naccount = "personal"\nkind = "mbox"\npath = "/tmp/never.mbox"\n'
            '[sources.failed]\naccount = "personal"\nkind = "mbox"\npath = "/tmp/failed.mbox"\n'
            '[sources.invalid]\naccount = "personal"\nkind = "mbox"\npath = "/tmp/invalid.mbox"\n'
            '[sources.disabled]\naccount = "personal"\nkind = "mbox"\npath = "/tmp/disabled.mbox"\nenabled = false\n',
            encoding="utf-8",
        )
        now = as_of(None)
        sources = {source.id: source for source in configured_sources(self.config)}
        conn = sqlite3.connect(path)
        try:
            account_id = conn.execute(
                "SELECT account_id FROM accounts WHERE alias = 'personal'"
            ).fetchone()[0]
            stale_receipt = {
                "schema_version": "mailcrawl.refresh.v1",
                "status": "success",
                "last_import_at": now - 86401,
                "source_spec_sha256": source_digest(sources["stale"]),
                "counts": {},
            }
            conn.execute(
                "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (?, ?, ?, ?)",
                (account_id, "local-export:stale", json.dumps(stale_receipt), now),
            )
            conn.execute(
                "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (?, ?, ?, ?)",
                (
                    account_id,
                    "local-export-attempt:failed",
                    json.dumps(
                        {
                            "status": "failed",
                            "last_attempt_at": now - 5,
                            "error": "private-failure-detail",
                        }
                    ),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (?, ?, ?, ?)",
                (
                    account_id,
                    "local-export:invalid",
                    "malformed-private-receipt",
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        result = status(self.home, self.config, "personal")
        source_states = {
            row["id"]: row["state"] for row in result["freshness"]["sources"]
        }

        self.assertEqual(result["state"], "degraded")
        self.assertIn("source refresh", result["summary"])
        self.assertEqual(source_states["stale"], "stale")
        self.assertEqual(source_states["never"], "never_imported")
        self.assertEqual(source_states["failed"], "failed")
        self.assertEqual(source_states["invalid"], "invalid_receipt")
        self.assertEqual(source_states["disabled"], "disabled")
        warning_codes = {item["code"] for item in result["warnings"]}
        self.assertTrue(
            {
                "source_refresh_stale",
                "source_never_imported",
                "source_refresh_failed",
                "source_receipt_invalid",
            }.issubset(warning_codes)
        )
        self.assertNotIn("source_disabled", warning_codes)
        rendered = json.dumps(result)
        self.assertNotIn("private-failure-detail", rendered)
        self.assertNotIn("malformed-private-receipt", rendered)

    def test_source_health_does_not_override_setup_or_storage_maintenance(self) -> None:
        self.config.write_text(
            '[accounts.personal]\nemail = "private-person@example.test"\n'
            '[sources.never]\naccount = "personal"\nkind = "mbox"\npath = "/tmp/never.mbox"\n',
            encoding="utf-8",
        )
        setup = status(self.home, self.config, "personal")
        self.assertEqual(setup["state"], "needs_setup")
        self.assertNotIn(
            "source_never_imported", {item["code"] for item in setup["warnings"]}
        )

        path = self.home / "personal.db"
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE marker(value TEXT)")
        writer.commit()
        writer.execute("INSERT INTO marker(value) VALUES ('committed-in-wal')")
        writer.commit()
        maintenance = status(self.home, self.config, "personal")
        writer.close()

        self.assertEqual(maintenance["state"], "needs_maintenance")

    def test_active_wal_is_visible_but_never_reported_as_fresh_or_modified(self) -> None:
        path = self.home / "personal.db"
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE marker(value TEXT)")
        writer.execute("PRAGMA user_version = 11")
        writer.commit()
        writer.execute("INSERT INTO marker(value) VALUES ('committed-in-wal')")
        writer.commit()
        wal = Path(str(path) + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        before = _file_state(_database_paths(path) + (self.config,))

        result = status(self.home, self.config, "personal")

        database = result["databases"][0]
        self.assertEqual(result["state"], "needs_maintenance")
        self.assertIn(
            "sqlite_wal_requires_maintenance",
            {item["code"] for item in result["warnings"]},
        )
        self.assertGreater(database["wal_bytes"], 0)
        self.assertNotIn("counts", database)
        self.assertIsNone(result["freshness"]["last_import_at"])
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)
        writer.close()

    def test_active_rollback_journal_is_reported_without_querying(self) -> None:
        path = _healthy_database(self.home)
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode = DELETE")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE accounts SET created_at = created_at + 1 WHERE alias = 'personal'")
        journal = Path(str(path) + "-journal")
        self.assertGreater(journal.stat().st_size, 0)
        before = _file_state(_database_paths(path) + (self.config,))

        result = status(self.home, self.config, "personal")

        self.assertEqual(result["state"], "needs_maintenance")
        self.assertIn(
            "sqlite_journal_requires_maintenance",
            {item["code"] for item in result["warnings"]},
        )
        self.assertNotIn("counts", result["databases"][0])
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)
        writer.close()

    def test_doctor_runs_quick_check_without_changing_source_files(self) -> None:
        path = _healthy_database(self.home)
        before = _file_state(_database_paths(path) + (self.config,))

        result = doctor(self.home, self.config, "personal")

        self.assertEqual(result["checks"]["config"], "ok")
        self.assertEqual(result["checks"]["databases"], [{"id": "personal", "integrity": "ok"}])
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)

    def test_doctor_reports_a_bounded_quick_check_as_incomplete(self) -> None:
        path = _healthy_database(self.home)
        before = _file_state(_database_paths(path) + (self.config,))

        with (
            mock.patch("mailcrawl.control._CHECK_PROGRESS_INTERVAL", 1),
            mock.patch("mailcrawl.control._CHECK_PROGRESS_CALL_LIMIT", 0),
        ):
            result = doctor(self.home, self.config, "personal")

        self.assertEqual(result["state"], "needs_maintenance")
        self.assertEqual(result["checks"]["databases"][0]["integrity"], "incomplete")
        self.assertIn(
            "database_integrity_check_incomplete",
            {item["code"] for item in result["warnings"]},
        )
        self.assertEqual(_file_state(_database_paths(path) + (self.config,)), before)

    def test_config_validation_does_not_echo_parse_errors_or_values(self) -> None:
        self.config.write_text(
            '[accounts.personal]\nemail = "private-person@example.test"\n'
            'invalid = "private-config-sentinel"\n'
            'invalid = "duplicate"\n',
            encoding="utf-8",
        )
        result = doctor(self.home, self.config, "personal")

        self.assertEqual(result["checks"]["config"], "invalid")
        rendered = json.dumps(result)
        self.assertIn("config_invalid", rendered)
        self.assertNotIn("private-config-sentinel", rendered)
        self.assertNotIn("private-person@example.test", rendered)

    def test_doctor_marks_an_uninitialized_configured_account_incomplete(self) -> None:
        _healthy_database(self.home)
        self.config.write_text("[accounts.personal]\n", encoding="utf-8")

        result = doctor(self.home, self.config)

        self.assertEqual(result["checks"]["config"], "incomplete")
        self.assertEqual(result["state"], "needs_setup")


if __name__ == "__main__":
    unittest.main()
