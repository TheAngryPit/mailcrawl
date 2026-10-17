"""Time facts veto a copied or scored state. The clock stays in one module."""

import calendar
import json
import os
import unittest
from pathlib import Path

from mailcrawl.clock import as_of
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.inference import invoke
from mailcrawl.jev import MODEL_ID, FakeJev, jev_decide
from mailcrawl.review import undo_decisions
from mailcrawl.store import connect
from mailcrawl.timefacts import TEXT_CAP, TimePolicy, apply_time, thread_time_facts
from mailcrawl.vectors import FakeEmbedder, embed_threads, knn_decide

ROOT = Path(__file__).resolve().parents[1]
AS_OF = calendar.timegm((2026, 10, 17, 12, 0, 0))
_DECISION_FILES = (
    "decisions.py",
    "review.py",
    "vectors.py",
    "jev.py",
    "rules.py",
    "timefacts.py",
    "cli.py",
)


def _stamp(year: int, month: int, day: int) -> int:
    return calendar.timegm((year, month, day, 12, 0, 0))


class TimeFactsTest(unittest.TestCase):
    def test_decision_code_does_not_read_the_wall_clock(self) -> None:
        for name in _DECISION_FILES:
            text = (ROOT / "mailcrawl" / name).read_text(encoding="utf-8")
            self.assertNotIn("time.time(", text, name)
            self.assertNotIn("datetime.now(", text, name)
            self.assertNotIn("date.today(", text, name)
        clock = (ROOT / "mailcrawl" / "clock.py").read_text(encoding="utf-8")
        self.assertIn("time.time(", clock)
        self.assertEqual(as_of(AS_OF, 1), AS_OF)
        self.assertEqual(as_of(None, AS_OF), AS_OF)

    def test_date_scan_is_bounded_and_has_no_eval(self) -> None:
        source = (ROOT / "mailcrawl" / "timefacts.py").read_text(encoding="utf-8")
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)
        self.assertNotIn(".*", source)
        self.assertNotIn(".+", source)
        with self._db() as conn:
            late = "a" * (TEXT_CAP + 50) + " deadline 2 Sep 2026"
            self._message(conn, 1, "other@example.com", 0, _stamp(2026, 10, 1), "Form", late)
            facts = thread_time_facts(conn, 1, AS_OF)
        self.assertEqual(facts.found, ())
        self.assertLess(len(late[:TEXT_CAP]), len(late))

    def test_four_closed_threads_move_and_a_fresh_request_stays(self) -> None:
        cases = (
            (
                "past meeting",
                _stamp(2026, 9, 1),
                "other@example.com",
                0,
                "Meeting on 2 Sep 2026. please reply",
                "reference",
                "paper_trail",
                "past date",
                "reply",
            ),
            (
                "owner closed",
                _stamp(2025, 12, 15),
                "owner@example.com",
                1,
                "please reply",
                "resolved",
                "paper_trail",
                "waiting is stale",
                "reply",
            ),
            (
                "past form",
                _stamp(2026, 10, 1),
                "other@example.com",
                0,
                "Deadline 15 Mar 2025. please reply",
                "reference",
                "paper_trail",
                "past date",
                "reply",
            ),
            (
                "acknowledged quote",
                _stamp(2025, 2, 10),
                "supplier@example.com",
                0,
                "Quote acknowledged 10 Feb 2025. please reply stated deadline",
                "reference",
                "paper_trail",
                "last activity is old",
                "action",
            ),
            (
                "fresh request",
                _stamp(2026, 10, 16),
                "other@example.com",
                0,
                "please reply",
                "reply",
                "imbox",
                None,
                "reply",
            ),
        )
        table = []
        with self._db() as conn:
            for index, (name, when, sender, mine, body, state, lane, reason, before) in enumerate(cases, start=1):
                self._message(conn, index, sender, mine, when, name, body)
                stored = jev_decide(
                    conn,
                    index,
                    FakeJev(),
                    {"jev-battery": True},
                    AS_OF,
                    owners=frozenset({"owner@example.com"}),
                )
                self.assertIsNotNone(stored)
                self.assertEqual(stored["origin"], "model")
                self.assertEqual(stored["source_detail"], MODEL_ID)
                self.assertEqual(stored["next_state"], state)
                self.assertEqual(stored["lane"], lane)
                evidence = json.loads(stored["evidence"])
                if reason is None:
                    self.assertEqual(len(evidence), 1)
                    time_reason = None
                else:
                    self.assertIn(reason, evidence[1]["time_reason"])
                    time_reason = evidence[1]["time_reason"]
                table.append((name, before, stored["next_state"], stored["lane"], time_reason))
        self.assertEqual(
            table,
            [
                ("past meeting", "reply", "reference", "paper_trail", ["past date"]),
                ("owner closed", "reply", "resolved", "paper_trail", ["owner sent last", "waiting is stale"]),
                ("past form", "reply", "reference", "paper_trail", ["past date"]),
                (
                    "acknowledged quote",
                    "action",
                    "reference",
                    "paper_trail",
                    ["last activity is old"],
                ),
                ("fresh request", "reply", "reply", "imbox", None),
            ],
        )
        root = os.environ.get("PROOF_DIR")
        if root:
            rows = [
                {
                    "name": name,
                    "before": before,
                    "after": after,
                    "lane": lane,
                    "time_reason": reason,
                }
                for name, before, after, lane, reason in table
            ]
            dest = Path(root) / "time-decisions.json"
            dest.write_text(
                json.dumps({"as_of": AS_OF, "rows": rows}, indent=2) + "\n",
                encoding="utf-8",
            )

    def test_knn_does_not_copy_a_reply_onto_an_old_thread(self) -> None:
        if __import__("importlib").util.find_spec("sqlite_vec") is None:
            self.skipTest("sqlite-vec is not installed")
        with self._db() as conn:
            self._message(conn, 1, "owner@example.com", 0, _stamp(2026, 10, 1), "Near", "same words")
            self._message(conn, 2, "other@example.com", 0, _stamp(2025, 2, 10), "Old", "same words")
            flags = {"embeddings": True}
            self.assertEqual(
                invoke("embeddings", lambda: embed_threads(conn, FakeEmbedder(), flags), flags),
                2,
            )
            decide_once(
                conn,
                1,
                AS_OF,
                lambda: DecisionDraft(
                    next_state="reply",
                    lane="imbox",
                    rule_id="else",
                    rules_version="rules-v4",
                    origin="human",
                    source_detail="review",
                ),
            )
            stored = knn_decide(conn, 2, FakeEmbedder(), flags, AS_OF)
            self.assertEqual(stored["origin"], "knn")
            self.assertEqual(stored["source_detail"], "fake-embedder")
            self.assertEqual(stored["next_state"], "reference")
            self.assertEqual(stored["lane"], "paper_trail")
            evidence = json.loads(stored["evidence"])
            self.assertEqual(evidence[0]["neighbour_thread_pk"], 1)
            self.assertIn("last activity is old", evidence[1]["time_reason"])

    def test_undo_of_knn_rolls_back_when_one_previous_row_is_human(self) -> None:
        with self._db() as conn:
            self._message(conn, 1, "other@example.com", 0, AS_OF, "One", "synthetic")
            self._message(conn, 2, "other@example.com", 0, AS_OF, "Two", "synthetic")
            self._decision(conn, 1, "rule", "rule", "else", "else", None)
            conn.execute("UPDATE decisions SET superseded_by = decision_id WHERE decision_id = 1")
            self._decision(conn, 1, "knn", "embedding-cluster", "neighbour", "else", 1)
            self._decision(conn, 2, "human", "human", "person", "0", None)
            conn.execute("UPDATE decisions SET superseded_by = decision_id WHERE decision_id = 3")
            self._decision(conn, 2, "knn", "embedding-cluster", "neighbour", "else", 3)
            conn.commit()
            with self.assertRaises(Exception) as caught:
                undo_decisions(conn, thread_id=None, source_detail="neighbour", now=AS_OF)
            self.assertEqual(str(caught.exception), "a human decision stays")
            current = conn.execute(
                """
                SELECT thread_pk, origin FROM decisions
                WHERE superseded_by IS NULL
                ORDER BY thread_pk
                """
            ).fetchall()
            self.assertEqual([(int(row[0]), str(row[1])) for row in current], [(1, "knn"), (2, "knn")])

    def test_queue_exports_time_facts(self) -> None:
        from mailcrawl.review import ReviewScope, review_queue

        with self._db() as conn:
            self._message(
                conn,
                1,
                "Mailsuite Alert <alerts@mailsuite.example>",
                0,
                _stamp(2026, 10, 16),
                "Tracked",
                "opened",
            )
            self._message(conn, 1, "owner@example.com", 1, _stamp(2026, 10, 16), "Tracked", "done")
            items = review_queue(
                conn,
                "personal",
                ReviewScope(everything=True),
                as_of=AS_OF,
                owners=frozenset({"owner@example.com"}),
            )
            self.assertEqual(items[0].time["last_human"], "owner")
            self.assertEqual(items[0].document()["time"]["last_human"], "owner")

    def test_owner_waiting_needs_a_review_date(self) -> None:
        draft = DecisionDraft(
            next_state="reply",
            lane="imbox",
            rule_id="knn",
            rules_version="rules-v4",
            origin="knn",
            source_detail="fake-embedder",
        )
        facts = thread_time_facts_from(
            last_human="owner",
            age_days=1,
        )
        adjusted = apply_time(draft, facts, TimePolicy())
        self.assertEqual(adjusted.next_state, "waiting")
        self.assertEqual(adjusted.review_date, AS_OF)
        self.assertEqual(adjusted.origin, "knn")
        self.assertEqual(adjusted.source_detail, "fake-embedder")

    def _db(self):
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def opened():
            with tempfile.TemporaryDirectory() as tmp:
                conn = connect(Path(tmp) / "personal.db")
                try:
                    conn.execute(
                        "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
                    )
                    conn.commit()
                    yield conn
                finally:
                    conn.close()

        return opened()

    def _message(self, conn, thread_pk: int, sender: str, mine: int, when: int, subject: str, body: str) -> None:
        account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        conn.execute(
            """
            INSERT INTO threads(thread_pk, account_id, gmail_thread_id, msg_count, image_only)
            VALUES (?, ?, ?, 1, 0)
            ON CONFLICT(thread_pk) DO UPDATE SET msg_count = msg_count + 1
            """,
            (thread_pk, account_id, f"thread-{thread_pk}"),
        )
        conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, from_hdr, subject,
              body_text, is_from_me, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                account_id,
                f"m-{thread_pk}-{when}-{mine}",
                thread_pk,
                when,
                sender,
                subject,
                body,
                mine,
                f"sha-{thread_pk}-{when}",
                f"prov-{thread_pk}-{when}",
            ),
        )
        conn.commit()

    def _decision(self, conn, thread_pk, origin, source, detail, rule_id, supersedes) -> None:
        conn.execute(
            """
            INSERT INTO decisions(
              thread_pk, next_state, lane, source, origin, source_detail,
              rule_id, rules_version, evidence, created_at
            ) VALUES (?, 'reply', 'imbox', ?, ?, ?, ?, 'rules-v4', '[]', 1)
            """,
            (thread_pk, source, origin, detail, rule_id),
        )
        if supersedes is not None:
            new_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "UPDATE decisions SET superseded_by = ? WHERE decision_id = ?",
                (new_id, supersedes),
            )


def thread_time_facts_from(last_human: str, age_days: int):
    from mailcrawl.timefacts import FoundDate, TimeFacts

    return TimeFacts(
        as_of=AS_OF,
        first_at=AS_OF - age_days * 86400,
        last_at=AS_OF - age_days * 86400,
        age_days=age_days,
        last_human=last_human,
        found=(),
        past_deadline=False,
        past_meeting=False,
    )
