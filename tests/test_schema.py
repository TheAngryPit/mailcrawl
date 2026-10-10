"""Schema v1 creates ingest and FTS tables and nothing later."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mailcrawl.schema import TABLES, USER_VERSION
from mailcrawl.store import UnsupportedSchema, connect, migrate

BANNED = (
    "decisions",
    "plans",
    "proposals",
    "embeddings",
    "clips",
    "sender_decisions",
    "approvals",
    "applied_changes",
    "thread_mutes",
)


class SchemaV1Test(unittest.TestCase):
    def test_migrate_creates_ingest_and_fts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "personal.db"
            conn = connect(path)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, USER_VERSION)
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                for table in TABLES:
                    self.assertIn(table, names)
                for banned in BANNED:
                    self.assertNotIn(banned, names)
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'msg_fts'"
                ).fetchone()[0]
                self.assertIn("unicode61 remove_diacritics 2", sql)
                conn.execute(
                    "INSERT INTO accounts(alias, email_hash, created_at) VALUES (?,?,?)",
                    ("personal", "abc", 1),
                )
                conn.execute(
                    """
                    INSERT INTO threads(account_id, gmail_thread_id)
                    VALUES (1, 'thread-1')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, raw_sha256,
                      provenance_hash, imported_at, subject, body_text, from_hdr
                    ) VALUES (1, 'm1', 1, 'raw', 'prov', 1, 'Reunião', 'A reunião', 'ada@example.com')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO msg_fts(rowid, subject, body_text, from_hdr, attachment_text)
                    VALUES (1, 'Reunião', 'A reunião', 'ada@example.com', '')
                    """
                )
                hit = conn.execute(
                    "SELECT rowid FROM msg_fts WHERE msg_fts MATCH 'reuniao'"
                ).fetchone()
                self.assertEqual(hit[0], 1)
            finally:
                conn.close()

    def test_second_migrate_keeps_user_version(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            migrate(conn)
            migrate(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            count = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'messages'"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_later_user_version_fails_closed(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA user_version = 2")
            with self.assertRaises(UnsupportedSchema):
                migrate(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("messages", names)
        finally:
            conn.close()
