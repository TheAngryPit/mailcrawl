"""Jev stays off unless config turns the battery or the reranker on."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from mailcrawl.decisions import DecisionDraft, decide_once
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
    score_grey_band,
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
    return _threads(conn, account_id)


def _threads(conn: sqlite3.Connection, account_id: int) -> dict[str, int]:
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


def _reply_document() -> bytes:
    return json.dumps(
        {
            "model": "jev-1.13.0",
            "answers": {
                "asks-reply": {"type": "noul", "noul": 0.95},
                "asks-deadline": {"type": "noul", "noul": 0.05},
                "is-receipt": {"type": "noul", "noul": 0.05},
                "is-bulk": {"type": "noul", "noul": 0.05},
                "category": {
                    "type": "choice",
                    "choice": "CATEGORY_PERSONAL",
                    "confidence": 0.90,
                    "probabilities": {"CATEGORY_PERSONAL": 0.90},
                },
                "urgency": {
                    "type": "score",
                    "score": 0.0,
                    "confidence": 0.90,
                    "probabilities": {"none": 0.90},
                },
            },
        }
    ).encode()


class _Ok:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Ok:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _http_error(code: int, retry_after: str | None, body: bytes = b"") -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://api.typesafe.ai/v1/systemone",
        code,
        "error",
        headers,
        io.BytesIO(body),
    )


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

    def test_rule_needs_review_is_eligible_and_a_human_row_stays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                flags = {"jev-battery": True}
                client = CountingJev()
                rule = decide_once(
                    conn,
                    ids["reply"],
                    10,
                    lambda: DecisionDraft(
                        next_state="needs_review",
                        lane="imbox",
                        rule_id="else",
                        rules_version="rules-v4",
                        origin="rule",
                        source_detail="else",
                    ),
                )
                human = decide_once(
                    conn,
                    ids["bulk"],
                    10,
                    lambda: DecisionDraft(
                        next_state="needs_review",
                        lane="imbox",
                        rule_id="else",
                        rules_version="rules-v4",
                        origin="human",
                        source_detail="review",
                    ),
                )
                settled = decide_once(
                    conn,
                    ids["bill"],
                    10,
                    lambda: DecisionDraft(
                        next_state="reference",
                        lane="paper_trail",
                        rule_id="3",
                        rules_version="rules-v4",
                        origin="rule",
                        source_detail="3",
                    ),
                )
                self.assertIsNone(jev_decide(conn, ids["reply"], client, None, 11))
                stayed = conn.execute(
                    "SELECT origin, next_state FROM decisions WHERE decision_id = ?",
                    (rule["decision_id"],),
                ).fetchone()
                self.assertEqual((stayed[0], stayed[1]), ("rule", "needs_review"))
                replaced = jev_decide(conn, ids["reply"], client, flags, 12)
                self.assertEqual(replaced["origin"], "model")
                self.assertEqual(replaced["next_state"], "reply")
                self.assertNotEqual(int(replaced["decision_id"]), int(rule["decision_id"]))
                old = conn.execute(
                    "SELECT superseded_by FROM decisions WHERE decision_id = ?",
                    (rule["decision_id"],),
                ).fetchone()
                self.assertEqual(int(old[0]), int(replaced["decision_id"]))
                kept_human = jev_decide(conn, ids["bulk"], client, flags, 13)
                self.assertEqual(int(kept_human["decision_id"]), int(human["decision_id"]))
                self.assertEqual(kept_human["origin"], "human")
                kept_rule = jev_decide(conn, ids["bill"], client, flags, 14)
                self.assertEqual(int(kept_rule["decision_id"]), int(settled["decision_id"]))
                self.assertEqual((kept_rule["origin"], kept_rule["next_state"]), ("rule", "reference"))
                self.assertEqual(client.calls, 1)
                passed = score_grey_band(conn, client, flags, 15)
                self.assertGreaterEqual(passed.eligible, 1)
                self.assertEqual(
                    conn.execute(
                        "SELECT origin FROM decisions WHERE decision_id = ?",
                        (human["decision_id"],),
                    ).fetchone()[0],
                    "human",
                )
            finally:
                conn.close()

    def test_model_and_knn_rows_stay_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                flags = {"jev-battery": True}
                client = CountingJev()
                model = decide_once(
                    conn,
                    ids["reply"],
                    10,
                    lambda: DecisionDraft(
                        next_state="needs_review",
                        lane="imbox",
                        rule_id="harness",
                        rules_version="harness",
                        origin="model",
                        source_detail="harness-fixture",
                    ),
                )
                knn = decide_once(
                    conn,
                    ids["bulk"],
                    10,
                    lambda: DecisionDraft(
                        next_state="noise",
                        lane="feed",
                        rule_id="3",
                        rules_version="rules-v4",
                        origin="knn",
                        source_detail="fake-embedder",
                    ),
                )
                stayed_model = jev_decide(conn, ids["reply"], client, flags, 11)
                stayed_knn = jev_decide(conn, ids["bulk"], client, flags, 11)
                self.assertEqual(client.calls, 0)
                self.assertEqual(int(stayed_model["decision_id"]), int(model["decision_id"]))
                self.assertEqual(
                    (stayed_model["origin"], stayed_model["rule_id"], stayed_model["next_state"]),
                    ("model", "harness", "needs_review"),
                )
                self.assertEqual(int(stayed_knn["decision_id"]), int(knn["decision_id"]))
                self.assertEqual((stayed_knn["origin"], stayed_knn["rule_id"]), ("knn", "3"))
                passed = score_grey_band(conn, client, flags, 12)
                self.assertEqual(passed.eligible, 3)
                self.assertEqual(passed.decided + passed.review, 3)
                self.assertEqual(client.calls, 3)
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM decisions WHERE thread_pk = ?",
                        (ids["reply"],),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM decisions WHERE thread_pk = ?",
                        (ids["bulk"],),
                    ).fetchone()[0],
                    1,
                )
                current_model = conn.execute(
                    """
                    SELECT origin, rule_id FROM decisions
                    WHERE thread_pk = ? AND superseded_by IS NULL
                    """,
                    (ids["reply"],),
                ).fetchone()
                current_knn = conn.execute(
                    """
                    SELECT origin, rule_id FROM decisions
                    WHERE thread_pk = ? AND superseded_by IS NULL
                    """,
                    (ids["bulk"],),
                ).fetchone()
                self.assertEqual((current_model[0], current_model[1]), ("model", "harness"))
                self.assertEqual((current_knn[0], current_knn[1]), ("knn", "3"))
            finally:
                conn.close()

    def test_pin_request_and_missing_key(self) -> None:
        body = request_body("synthetic", BATTERY)
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertNotIn("jev-latest", json.dumps(body))
        self.assertEqual(body["questions"]["asks-reply"]["type"], "noul")
        self.assertEqual(body["questions"]["category"]["type"], "choice")
        criteria = body["questions"]["category"]["criteria"]
        self.assertEqual(criteria["CATEGORY_PERSONAL"], "Mail from a person to the recipient.")
        self.assertNotEqual(criteria["CATEGORY_PERSONAL"], "CATEGORY_PERSONAL")
        self.assertEqual(criteria["CATEGORY_SOCIAL"], "A social network notification.")
        self.assertEqual(criteria["CATEGORY_PROMOTIONS"], "Marketing or a promotion.")
        self.assertEqual(criteria["CATEGORY_UPDATES"], "A receipt, a bill, or an automated update.")
        self.assertEqual(criteria["CATEGORY_FORUMS"], "A list or a forum.")
        self.assertEqual(criteria["other"], "None of the category keys fit.")
        self.assertEqual(body["questions"]["urgency"]["criteria"], ["none", "low", "high"])
        self.assertNotIn("db-v2", LOCAL_VERIFY)
        self.assertNotIn("--thread", LOCAL_VERIFY)
        spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
        self.assertIn(LOCAL_VERIFY, spec)
        self.assertIn("TYPESAFE_API_KEY", spec)
        self.assertIn("Retry-After", spec)
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
        os.environ.pop("TYPESAFE_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                with self.assertRaises(UsageError) as missing:
                    jev_decide(conn, ids["reply"], HttpJev(), {"jev-battery": True}, 1)
                self.assertIn("TYPESAFE_API_KEY", str(missing.exception))
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

    def test_local_verify_command_scores_grey_threads(self) -> None:
        import shlex
        from io import StringIO

        from mailcrawl.cli import main
        from mailcrawl.store import init_account

        parts = shlex.split(LOCAL_VERIFY)
        self.assertEqual(parts[:3], ["python3", "-m", "mailcrawl"])
        self.assertNotIn("--thread", parts)
        self.assertEqual(parts[parts.index("--client") + 1], "http")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            home = root / "home"
            argv = parts[3:]
            argv[argv.index("--db") + 1] = str(data)
            argv[argv.index("--client") + 1] = "fake"
            joined = " ".join(argv)
            self.assertNotIn("MailVault", joined)
            self.assertNotIn("db-v2", joined)
            saved = {name: os.environ.get(name) for name in ("HOME", "JEV_API_KEY", "TYPESAFE_API_KEY")}
            os.environ["HOME"] = str(home)
            os.environ.pop("JEV_API_KEY", None)
            os.environ.pop("TYPESAFE_API_KEY", None)
            try:
                cfg = home / ".config" / "mailcrawl"
                cfg.mkdir(parents=True)
                (cfg / "config.toml").write_text("[inference]\njev-battery = true\n", encoding="utf-8")
                path = init_account(data, "thehive", "ada@example.com")
                conn = connect(path)
                try:
                    account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
                    ids = _threads(conn, account_id)
                    conn.execute(
                        "UPDATE threads SET image_only = 1 WHERE thread_pk = ?",
                        (ids["grey"],),
                    )
                    conn.commit()
                finally:
                    conn.close()
                first_out = StringIO()
                with patch("sys.stdout", first_out):
                    first = main(argv)
                self.assertEqual(first, 0)
                self.assertEqual(first_out.getvalue(), "ok\n")
                conn = connect(path)
                try:
                    stored = conn.execute(
                        """
                        SELECT threads.gmail_thread_id, decisions.next_state, decisions.source_detail,
                               decisions.decision_id
                        FROM decisions JOIN threads ON threads.thread_pk = decisions.thread_pk
                        WHERE decisions.superseded_by IS NULL
                        ORDER BY threads.gmail_thread_id
                        """
                    ).fetchall()
                    self.assertEqual(
                        [(row[0], row[1], row[2]) for row in stored],
                        [
                            ("t-bill", "reference", "jev-1.13.0"),
                            ("t-bulk", "noise", "jev-1.13.0"),
                            ("t-late", "reference", "jev-1.13.0"),
                            ("t-reply", "reply", "jev-1.13.0"),
                        ],
                    )
                    grey = conn.execute(
                        "SELECT count(*) FROM decisions WHERE thread_pk = ?",
                        (ids["grey"],),
                    ).fetchone()[0]
                    self.assertEqual(grey, 0)
                    answers = conn.execute("SELECT count(*) FROM jev_answers").fetchone()[0]
                    self.assertEqual(answers, 24)
                    ids_before = [row[3] for row in stored]
                finally:
                    conn.close()
                second_out = StringIO()
                with patch("sys.stdout", second_out):
                    second = main(argv)
                self.assertEqual(second, 0)
                self.assertEqual(second_out.getvalue(), "ok\n")
                conn = connect(path)
                try:
                    again = conn.execute(
                        """
                        SELECT decision_id FROM decisions
                        WHERE superseded_by IS NULL
                        ORDER BY decision_id
                        """
                    ).fetchall()
                    self.assertEqual(sorted(row[0] for row in again), sorted(ids_before))
                    answers = conn.execute("SELECT count(*) FROM jev_answers").fetchone()[0]
                    self.assertEqual(answers, 24)
                finally:
                    conn.close()
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_http_429_retries_once_then_reviews(self) -> None:
        saved = {name: os.environ.get(name) for name in ("JEV_API_KEY", "TYPESAFE_API_KEY")}
        os.environ.pop("JEV_API_KEY", None)
        os.environ["TYPESAFE_API_KEY"] = "test-key"
        slept: list[float] = []
        posted: list[dict[str, object]] = []

        def urlopen(req: urllib.request.Request, timeout: int = 30) -> _Ok:
            posted.append(json.loads(req.data.decode()))
            self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
            self.assertEqual(timeout, 30)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        queue: list[object] = [
            _http_error(429, "120"),
            _Ok(_reply_document()),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                with patch("urllib.request.urlopen", urlopen), patch(
                    "time.sleep",
                    side_effect=lambda seconds: slept.append(seconds),
                ):
                    decided = jev_decide(conn, ids["reply"], HttpJev(), {"jev-battery": True}, 10)
                    self.assertEqual(slept, [60])
                    self.assertEqual(posted[0]["model"], "jev-1.13.0")
                    self.assertEqual(decided["next_state"], "reply")
                    self.assertEqual(decided["source_detail"], "jev-1.13.0")
                    os.environ.pop("TYPESAFE_API_KEY", None)
                    os.environ["JEV_API_KEY"] = "test-key"
                    slept.clear()
                    queue[:] = [_http_error(429, None), _http_error(429, "0")]
                    reviewed = jev_decide(conn, ids["late"], HttpJev(), {"jev-battery": True}, 11)
                    self.assertEqual(slept, [0])
                    self.assertEqual(reviewed["next_state"], "needs_review")
                    self.assertEqual(reviewed["source_detail"], "jev-1.13.0")
                answers = conn.execute(
                    "SELECT count(*) FROM jev_answers WHERE thread_pk = ?",
                    (ids["late"],),
                ).fetchone()[0]
                self.assertEqual(answers, 0)
            finally:
                conn.close()
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
