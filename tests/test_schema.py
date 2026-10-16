"""Schema v2 creates ingest, FTS, and the empty decision tables."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mailcrawl.schema import TABLES, USER_VERSION
from mailcrawl.store import UnsupportedSchema, connect, migrate

BANNED = (
    "plans",
    "proposals",
    "embeddings",
    "clips",
    "approvals",
    "applied_changes",
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
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(messages)")
                }
                self.assertIn("body_text_raw", columns)
                self.assertIn("body_text", columns)
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
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], USER_VERSION)
            count = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'messages'"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_later_user_version_fails_closed(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA user_version = 5")
            with self.assertRaises(UnsupportedSchema):
                migrate(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("messages", names)
        finally:
            conn.close()

    def test_v1_keeps_raw_text_and_indexes_cleaned_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._assert_v1_upgrade(Path(tmp) / "personal.db")

    def _assert_v1_upgrade(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE messages (
                  msg_pk INTEGER PRIMARY KEY,
                  subject TEXT,
                  body_text TEXT,
                  from_hdr TEXT
                );
                CREATE TABLE attachments (
                  att_pk INTEGER PRIMARY KEY,
                  msg_pk INTEGER,
                  extracted_text TEXT
                );
                CREATE VIRTUAL TABLE msg_fts USING fts5(
                  subject,
                  body_text,
                  from_hdr,
                  attachment_text,
                  content='',
                  tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                """
                INSERT INTO messages(msg_pk, subject, body_text, from_hdr)
                VALUES (1, 'Hi', ?, 'ada@example.com')
                """,
                ("Keep this.\n> quoted secretwordqx\n-- \nAda Fixture",),
            )
            conn.execute(
                """
                INSERT INTO msg_fts(rowid, subject, body_text, from_hdr, attachment_text)
                VALUES (1, 'Hi', 'quoted secretwordqx', 'ada@example.com', '')
                """
            )
            conn.execute(
                "INSERT INTO attachments(msg_pk, extracted_text) VALUES (1, 'attachwordqx')"
            )
            migrate(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], USER_VERSION)
            raw, cleaned = conn.execute(
                "SELECT body_text_raw, body_text FROM messages"
            ).fetchone()
            self.assertEqual(
                raw,
                "Keep this.\n> quoted secretwordqx\n-- \nAda Fixture",
            )
            self.assertEqual(cleaned, "Keep this.")
            self.assertIsNone(
                conn.execute(
                    "SELECT rowid FROM msg_fts WHERE msg_fts MATCH 'secretwordqx'"
                ).fetchone()
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT rowid FROM msg_fts WHERE msg_fts MATCH 'attachwordqx'"
                ).fetchone()
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT rowid FROM msg_fts WHERE msg_fts MATCH 'Keep'"
                ).fetchone()
            )
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("decisions", names)
            self.assertIn("sender_decisions", names)
            self.assertIn("thread_mutes", names)
            for banned in BANNED:
                self.assertNotIn(banned, names)
        finally:
            conn.close()

    def test_v2_gains_the_screener_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "personal.db"
            fresh = connect(path)
            try:
                fresh.execute("PRAGMA user_version = 2")
                fresh.commit()
            finally:
                fresh.close()
            again = connect(path)
            try:
                self.assertEqual(again.execute("PRAGMA user_version").fetchone()[0], USER_VERSION)
                again.execute(
                    "INSERT INTO accounts(alias, email_hash, created_at) VALUES (?,?,?)",
                    ("personal", "abc", 1),
                )
                again.execute(
                    "INSERT INTO threads(account_id, gmail_thread_id) VALUES (1, 'thread-1')"
                )
                again.execute(
                    """
                    INSERT INTO decisions(
                      thread_pk, next_state, source, origin, source_detail,
                      rules_version, evidence, created_at
                    ) VALUES (1, 'screener', 'rule', 'rule', 'screener', 'rules-v3', '[]', 1)
                    """
                )
                state = again.execute("SELECT next_state FROM decisions").fetchone()[0]
                self.assertEqual(state, "screener")
            finally:
                again.close()

    def test_sender_route_read_and_waiting_needs_a_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                conn.execute(
                    "INSERT INTO accounts(alias, email_hash, created_at) VALUES (?,?,?)",
                    ("personal", "abc", 1),
                )
                conn.execute(
                    """
                    INSERT INTO senders(account_id, address_norm, domain)
                    VALUES (1, 'ada@example.com', 'example.com')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO senders(account_id, address_norm, domain)
                    VALUES (1, 'bo@example.com', 'example.com')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO threads(account_id, gmail_thread_id)
                    VALUES (1, 'thread-1')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sender_decisions(sender_id, status, route)
                    VALUES (1, 'approved', 'read')
                    """
                )
                priority, sla = conn.execute(
                    "SELECT priority, client_sla_days FROM sender_decisions"
                ).fetchone()
                self.assertIsNone(priority)
                self.assertIsNone(sla)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO sender_decisions(sender_id, status, route)
                        VALUES (2, 'approved', 'nope')
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO decisions(
                          thread_pk, next_state, source, origin, source_detail,
                          rules_version, evidence, created_at
                        ) VALUES (1, 'waiting', 'rule', 'rule', '0', 'rules-v0', '[]', 1)
                        """
                    )
                conn.execute(
                    """
                    INSERT INTO decisions(
                      thread_pk, next_state, source, origin, source_detail,
                      rules_version, evidence, created_at
                    ) VALUES (1, 'needs_review', 'rule', 'rule', '0', 'rules-v0', '[]', 1)
                    """
                )
            finally:
                conn.close()
