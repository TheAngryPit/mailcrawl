"""A stored decision is not inferred again."""

import sqlite3
import unittest

from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.store import migrate


class DecideOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO accounts(alias, email_hash, created_at) VALUES (?,?,?)",
            ("personal", "abc", 1),
        )
        self.conn.execute(
            "INSERT INTO threads(account_id, gmail_thread_id) VALUES (1, 'thread-1')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_second_call_does_not_infer(self) -> None:
        calls: list[int] = []

        def produce() -> DecisionDraft:
            calls.append(1)
            return DecisionDraft(
                next_state="reply",
                lane="imbox",
                rule_id="5",
                rules_version="rules-v4",
                origin="model",
                source_detail="synthetic-model",
            )

        first = decide_once(self.conn, 1, 100, produce)
        second = decide_once(self.conn, 1, 200, produce)
        self.assertEqual(calls, [1])
        self.assertEqual(int(first["decision_id"]), int(second["decision_id"]))
        self.assertEqual(first["origin"], "model")
        self.assertEqual(first["source_detail"], "synthetic-model")
        self.assertEqual(int(first["created_at"]), 100)
        count = self.conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_v3_source_maps_onto_origin(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                CREATE TABLE decisions (
                  decision_id INTEGER PRIMARY KEY,
                  thread_pk INTEGER NOT NULL,
                  next_state TEXT NOT NULL,
                  priority INTEGER,
                  owner TEXT,
                  review_date INTEGER,
                  due_date INTEGER,
                  lane TEXT,
                  pinned_aside INTEGER NOT NULL DEFAULT 0,
                  muted INTEGER NOT NULL DEFAULT 0,
                  confidence REAL,
                  source TEXT NOT NULL,
                  rule_id TEXT,
                  rules_version TEXT NOT NULL,
                  evidence TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  superseded_by INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO decisions(
                  thread_pk, next_state, source, rule_id, rules_version, evidence, created_at
                ) VALUES (1, 'reference', 'embedding-cluster', '5', 'rules-v4', '[]', 10)
                """
            )
            conn.execute("PRAGMA user_version = 3")
            migrate(conn)
            row = conn.execute(
                "SELECT origin, source_detail, created_at FROM decisions"
            ).fetchone()
            self.assertEqual(row["origin"], "knn")
            self.assertEqual(row["source_detail"], "5")
            self.assertEqual(int(row["created_at"]), 10)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 4)
        finally:
            conn.close()
