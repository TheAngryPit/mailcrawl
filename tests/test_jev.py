"""Jev stays off unless config turns the battery or the reranker on."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mailcrawl.inference import BoundaryError, scan_source
from mailcrawl.jev import (
    BATTERY,
    LOCAL_VERIFY,
    MODEL_ID,
    Answer,
    FakeJev,
    HttpJev,
    Question,
    jev_decide,
    open_client,
    request_body,
)
from mailcrawl.paths import UsageError
from mailcrawl.search import search_messages
from mailcrawl.store import connect, index_message

ROOT = Path(__file__).resolve().parents[1]


class CountingJev:
    def __init__(self) -> None:
        self.inner = FakeJev()
        self.calls = 0
        self.model_id = self.inner.model_id
        self.left_machine = self.inner.left_machine

    def ask(
        self,
        state: str,
        questions: tuple[Question, ...],
    ) -> tuple[dict[str, Answer], str]:
        self.calls += 1
        return self.inner.ask(state, questions)


def _seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute(
        "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
    )
    account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
    rows = (
        ("reply", "t-reply", "m-reply", "Reply note", "please reply shared token shared token"),
        ("late", "t-late", "m-late", "Late note", "shared token answers-query"),
        ("grey", "t-grey", "m-grey", "Grey note", "verify your password shared token"),
        ("bulk", "t-bulk", "m-bulk", "Bulk note", "bulk marketing shared token"),
        ("bill", "t-bill", "m-bill", "Bill note", "invoice receipt shared token"),
    )
    ids: dict[str, int] = {}
    for offset, (key, thread_id, message_id, subject, body) in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO threads(account_id, gmail_thread_id, msg_count)
            VALUES (?, ?, 1)
            """,
            (account_id, thread_id),
        )
        thread_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, subject,
              body_text, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, message_id, thread_pk, offset, subject, body, f"sha-{key}", f"prov-{key}", 1),
        )
        msg_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        index_message(conn, msg_pk, subject, body, "ada@example.com", "")
        ids[key] = thread_pk
    conn.commit()
    return ids


class JevBatteryTest(unittest.TestCase):
    def test_off_stays_on_review_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                client = CountingJev()
                self.assertIsNone(jev_decide(conn, ids["reply"], client, None, 10))
                self.assertEqual(client.calls, 0)
                count = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_fake_decision_is_stored_once_with_the_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                client = CountingJev()
                flags = {"jev-battery": True}
                first = jev_decide(conn, ids["reply"], client, flags, 10)
                second = jev_decide(conn, ids["reply"], client, flags, 11)
                self.assertEqual(client.calls, 1)
                self.assertEqual(first["decision_id"], second["decision_id"])
                self.assertEqual(first["origin"], "model")
                self.assertEqual(first["source"], "llm")
                self.assertEqual(first["source_detail"], MODEL_ID)
                self.assertEqual(first["next_state"], "reply")
                self.assertEqual(first["lane"], "imbox")
                evidence = json.loads(first["evidence"])
                self.assertEqual(evidence[0]["grey"], False)
                self.assertEqual(evidence[0]["left_machine"], False)
                self.assertEqual(evidence[0]["model"], "jev-1.13.0")
                conn.execute("DELETE FROM decisions")
                conn.commit()
                third = jev_decide(conn, ids["reply"], client, flags, 12)
                self.assertEqual(client.calls, 1)
                self.assertEqual(third["source_detail"], MODEL_ID)
                self.assertEqual(third["created_at"], 12)
            finally:
                conn.close()

    def test_grey_band_and_marketing_and_image_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                client = CountingJev()
                flags = {"jev-battery": True}
                grey = jev_decide(conn, ids["grey"], client, flags, 10)
                self.assertEqual(grey["next_state"], "needs_review")
                self.assertEqual(grey["origin"], "model")
                self.assertEqual(json.loads(grey["evidence"])[0]["grey"], True)
                again = jev_decide(conn, ids["grey"], client, flags, 11)
                self.assertEqual(again["decision_id"], grey["decision_id"])
                bulk = jev_decide(conn, ids["bulk"], client, flags, 10)
                self.assertEqual((bulk["next_state"], bulk["lane"]), ("noise", "feed"))
                bill = jev_decide(conn, ids["bill"], client, flags, 10)
                self.assertEqual((bill["next_state"], bill["lane"]), ("reference", "paper_trail"))
                conn.execute("UPDATE threads SET image_only = 1 WHERE thread_pk = ?", (ids["reply"],))
                conn.commit()
                before = client.calls
                self.assertIsNone(jev_decide(conn, ids["reply"], client, flags, 13))
                self.assertEqual(client.calls, before)
            finally:
                conn.close()

    def test_pin_request_and_missing_key(self) -> None:
        body = request_body("synthetic", BATTERY)
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertNotIn("jev-latest", json.dumps(body))
        self.assertEqual(body["questions"]["asks-reply"]["type"], "noul")
        self.assertEqual(body["questions"]["category"]["type"], "choice")
        self.assertIn("other", body["questions"]["category"]["criteria"])
        self.assertEqual(body["questions"]["urgency"]["criteria"], ["none", "low", "high"])
        self.assertNotIn("db-v2", LOCAL_VERIFY)
        spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
        self.assertIn(LOCAL_VERIFY, spec)
        self.assertIsInstance(open_client("http"), HttpJev)
        with self.assertRaises(UsageError):
            jev_decide(
                sqlite3.connect(":memory:"),
                1,
                type("Alias", (), {"model_id": "jev-latest", "left_machine": False})(),
                {"jev-battery": True},
                1,
            )
        os.environ.pop("JEV_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                with self.assertRaises(UsageError) as missing:
                    jev_decide(conn, ids["reply"], HttpJev(), {"jev-battery": True}, 1)
                self.assertIn("JEV_API_KEY", str(missing.exception))
                self.assertIn(LOCAL_VERIFY, str(missing.exception))
                count = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.close()

    def test_rerank_orders_the_top_hits_and_keeps_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                _seed(conn)
                client = CountingJev()
                plain = search_messages(conn, "shared", jev=client, config=None)
                self.assertEqual(client.calls, 0)
                self.assertEqual(plain[0]["gmail_message_id"], "m-reply")
                self.assertNotIn("jev", [hit["source"] for hit in plain])
                flags = {"search-reranker": True}
                ranked = search_messages(conn, "shared", jev=client, config=flags)
                self.assertEqual(client.calls, 1)
                self.assertEqual(len(ranked), len(plain))
                self.assertEqual(ranked[0]["gmail_message_id"], "m-late")
                self.assertEqual(ranked[0]["source"], "jev")
                self.assertEqual([hit["source"] for hit in ranked], ["jev"] * len(ranked))
                search_messages(conn, "shared", jev=client, config=flags)
                self.assertEqual(client.calls, 1)
            finally:
                conn.close()

    def test_ask_outside_the_gate_does_not_open_http(self) -> None:
        path = ROOT / "mailcrawl" / "jev.py"
        self.assertEqual(scan_source(path.read_text(encoding="utf-8"), str(path)), [])
        with self.assertRaises(BoundaryError):
            FakeJev().ask("synthetic", BATTERY)
        self.assertNotIn("httpx", __import__("sys").modules)
        self.assertNotIn("requests", __import__("sys").modules)
