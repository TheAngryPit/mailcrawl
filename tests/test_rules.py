"""Rules 0–8 are deterministic, synthetic, and first-match."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from mailcrawl.rules import RULES_VERSION, apply_rules, load_rules
from mailcrawl.store import connect, init_account

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "testdata" / "rules" / "rules-v0.json"
NOW = 1_700_000_000
DAY = 86400


class RulesEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_account(Path(self.tmp.name), "vcl", "ada@example.com")
        self.conn = connect(self.db)
        self.config = load_rules(RULES)
        self.account = int(
            self.conn.execute("SELECT account_id FROM accounts").fetchone()[0]
        )
        self.n = 0

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_starred_forces_needs_review_without_the_body(self) -> None:
        thread = self._thread("secretbodyqx", labels=[("STARRED", "STARRED", None)])
        apply_rules(self.conn, now=NOW, config=self.config)
        row = self._current(thread)
        self.assertEqual(row["next_state"], "needs_review")
        self.assertEqual(row["rule_id"], "0")
        self.assertEqual(row["rules_version"], RULES_VERSION)
        evidence = json.loads(row["evidence"])
        blob = json.dumps(evidence)
        self.assertNotIn("secretbodyqx", blob)
        self.assertIn("STARRED", blob)

    def test_muted_thread_is_noise_and_finance_is_not(self) -> None:
        muted = self._thread("hello", mute=True)
        finance = self._thread(
            "hello",
            mute=True,
            labels=[("L1", "Example type/Receipt", "type.receipt")],
        )
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(muted)["next_state"], "noise")
        self.assertEqual(self._current(muted)["rule_id"], "1")
        self.assertEqual(self._current(muted)["lane"], "screened_out")
        self.assertEqual(self._current(finance)["next_state"], "reference")
        self.assertEqual(self._current(finance)["rule_id"], "3")

    def test_newsletter_noise_and_read_list(self) -> None:
        noise = self._thread(
            "bulletin",
            labels=[
                ("L2", "Example origin/Machine", "origin.machine"),
                ("L3", "Example type/Bulletin", "type.bulletin"),
            ],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        listed = self._thread(
            "bulletin",
            labels=[
                ("L2", "Example origin/Machine", "origin.machine"),
                ("L3", "Example type/Bulletin", "type.bulletin"),
            ],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
            sender_decision=("approved", "read", None),
        )
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(noise)["next_state"], "noise")
        self.assertEqual(self._current(noise)["rule_id"], "2")
        self.assertEqual(self._current(listed)["next_state"], "read_later")
        self.assertEqual(self._current(listed)["rule_id"], "2-read")
        apply_rules(self.conn, now=NOW + 30 * DAY, config=self.config)
        self.assertEqual(self._current(listed)["next_state"], "noise")
        self.assertEqual(self._current(listed)["rule_id"], "t-expiry")

    def test_account_security_age_and_human_reply_priority(self) -> None:
        fresh = self._thread(
            "check the login",
            when=NOW - DAY,
            labels=[("L6", "Example type/Account", "type.account")],
        )
        stale = self._thread(
            "check the login",
            when=NOW - 8 * DAY,
            labels=[("L6", "Example type/Account", "type.account")],
        )
        reply = self._thread("hello there", sender_decision=("approved", "imbox", 2))
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(fresh)["next_state"], "needs_review")
        self.assertEqual(self._current(fresh)["rule_id"], "4")
        self.assertEqual(self._current(stale)["next_state"], "reference")
        self.assertEqual(self._current(reply)["next_state"], "reply")
        self.assertEqual(self._current(reply)["priority"], 2)
        self.assertEqual(self._current(reply)["rule_id"], "5")

    def test_waiting_task_closing_and_transitions(self) -> None:
        waiting = self._thread("Can you confirm?", from_me=1)
        task = self._thread(
            "please send the file",
            labels=[("L8", "Example origin/Machine", "origin.machine")],
        )
        closed = self._thread("Thanks, all set.", from_me=1)
        guarded = self._thread(
            "Thanks, all set.",
            from_me=1,
            labels=[("L9", "Example type/Agreement", "type.agreement")],
        )
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(waiting)["next_state"], "waiting")
        self.assertEqual(self._current(waiting)["review_date"], NOW + 7 * DAY)
        self.assertEqual(self._current(task)["next_state"], "action")
        self.assertEqual(self._current(task)["rule_id"], "7")
        self.assertEqual(self._current(closed)["next_state"], "resolved")
        self.assertEqual(self._current(closed)["rule_id"], "8")
        self.assertEqual(self._current(guarded)["next_state"], "needs_review")
        self.assertEqual(self._current(guarded)["rule_id"], "else")

        self._message(waiting, "A new note.", NOW + 10, from_me=0)
        apply_rules(self.conn, now=NOW + 10, config=self.config)
        self.assertEqual(self._current(waiting)["next_state"], "reply")
        self.assertEqual(self._current(waiting)["rule_id"], "t-reply")

        overdue = self._thread("Could you look?", from_me=1)
        apply_rules(self.conn, now=NOW, config=self.config)
        apply_rules(self.conn, now=NOW + 7 * DAY, config=self.config)
        self.assertEqual(self._current(overdue)["next_state"], "needs_review")
        self.assertEqual(self._current(overdue)["rule_id"], "t-overdue")

    def test_second_run_does_not_add_a_decision(self) -> None:
        self._thread("hello there")
        apply_rules(self.conn, now=NOW, config=self.config)
        before = self.conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        apply_rules(self.conn, now=NOW, config=self.config)
        after = self.conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        self.assertEqual(before, after)
        report = apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(report.threads, 1)
        self.assertEqual(report.covered, 1)

    def test_doubtful_line_is_not_a_question(self) -> None:
        thread = self._thread("On the meeting we decided.", from_me=1)
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(thread)["rule_id"], "else")

    def _current(self, thread_pk: int):
        return self.conn.execute(
            """
            SELECT next_state, rule_id, lane, priority, review_date, rules_version, evidence
            FROM decisions
            WHERE thread_pk = ? AND superseded_by IS NULL
            """,
            (thread_pk,),
        ).fetchone()

    def _thread(
        self,
        body: str,
        *,
        labels: list[tuple[str, str, str | None]] | None = None,
        headers: list[tuple[str, str]] | None = None,
        from_me: int = 0,
        when: int = NOW - DAY,
        sender_decision: tuple[str, str, int | None] | None = None,
        mute: bool = False,
    ) -> int:
        self.n += 1
        sender_id = self._sender(f"bao{self.n}@example.com", sender_decision)
        thread_pk = self._insert_thread(f"thread-{self.n}")
        self._message(thread_pk, body, when, from_me=from_me, sender_id=sender_id, labels=labels, headers=headers)
        if mute:
            self.conn.execute(
                "INSERT INTO thread_mutes(thread_pk, muted_at) VALUES (?, ?)",
                (thread_pk, when),
            )
        return thread_pk

    def _message(
        self,
        thread_pk: int,
        body: str,
        when: int,
        *,
        from_me: int,
        sender_id: int | None = None,
        labels: list[tuple[str, str, str | None]] | None = None,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.n += 1
        if sender_id is None:
            sender_id = self._sender(f"bao{self.n}@example.com", None)
        msg = self.conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, sender_id,
              body_text, is_from_me, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', 'prov', ?)
            RETURNING msg_pk
            """,
            (
                self.account,
                f"msg-{self.n}",
                thread_pk,
                when,
                sender_id,
                body,
                from_me,
                when,
            ),
        ).fetchone()[0]
        for label_id, name, key in labels or []:
            self.conn.execute(
                """
                INSERT INTO labels(
                  account_id, gmail_label_id, name, kind, catalogue_key, seen_at
                ) VALUES (?, ?, ?, 'user', ?, ?)
                ON CONFLICT(account_id, gmail_label_id) DO NOTHING
                """,
                (self.account, label_id, name, key, when),
            )
            self.conn.execute(
                """
                INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                VALUES (?, ?, ?, ?)
                """,
                (msg, self.account, label_id, when),
            )
        for name, value in headers or []:
            self.conn.execute(
                "INSERT INTO headers(msg_pk, name, value) VALUES (?, ?, ?)",
                (msg, name, value),
            )

    def _insert_thread(self, gmail_id: str) -> int:
        return int(
            self.conn.execute(
                """
                INSERT INTO threads(account_id, gmail_thread_id)
                VALUES (?, ?)
                RETURNING thread_pk
                """,
                (self.account, gmail_id),
            ).fetchone()[0]
        )

    def _sender(
        self,
        address: str,
        decision: tuple[str, str, int | None] | None,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO senders(account_id, address_norm, domain)
            VALUES (?, ?, 'example.com')
            RETURNING sender_id
            """,
            (self.account, address),
        ).fetchone()
        sender_id = int(row[0])
        if decision is not None:
            status, route, priority = decision
            self.conn.execute(
                """
                INSERT INTO sender_decisions(sender_id, status, route, priority)
                VALUES (?, ?, ?, ?)
                """,
                (sender_id, status, route, priority),
            )
        return sender_id


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
