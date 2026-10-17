"""Time facts veto a copied or scored state. The clock stays in one module."""

import ast
import calendar
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from mailcrawl.clock import AS_OF_MAX, as_of
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.inference import invoke
from mailcrawl.jev import MODEL_ID, FakeJev, jev_decide
from mailcrawl.paths import UsageError
from mailcrawl.review import undo_decisions
from mailcrawl.rules import apply_rules, load_rules
from mailcrawl.store import connect
from mailcrawl.timefacts import TEXT_CAP, TimeFacts, TimePolicy, apply_time, thread_time_facts
from mailcrawl.vectors import FakeEmbedder, embed_threads, knn_decide

ROOT = Path(__file__).resolve().parents[1]
AS_OF = calendar.timegm((2026, 10, 17, 12, 0, 0))


def _stamp(year: int, month: int, day: int) -> int:
    return calendar.timegm((year, month, day, 12, 0, 0))


class TimeFactsTest(unittest.TestCase):
    def test_decision_code_does_not_read_the_wall_clock(self) -> None:
        for path in sorted((ROOT / "mailcrawl").glob("*.py")):
            if path.name == "clock.py":
                continue
            self.assertFalse(_reads_wall_clock(path.read_text(encoding="utf-8")), path.name)
        clock = (ROOT / "mailcrawl" / "clock.py").read_text(encoding="utf-8")
        self.assertIn("time.time(", clock)
        self.assertEqual(as_of(AS_OF, 1), AS_OF)
        self.assertEqual(as_of(None, AS_OF), AS_OF)
        for sample in (
            "from time import time\n",
            "import time as t\n",
            "import datetime\ndatetime.datetime.utcnow()\n",
            "from datetime import datetime\ndatetime.now()\n",
            "from datetime import date\ndate.today()\n",
            "import time\ntime.time()\n",
        ):
            self.assertTrue(_reads_wall_clock(sample), sample)
        self.assertFalse(_reads_wall_clock("import time\ntime.sleep(1)\n"))
        self.assertFalse(_reads_wall_clock("x = 'time.time()'\n"))

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
                "needs_review",
                "imbox",
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
                "needs_review",
                "imbox",
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
                ("past meeting", "reply", "needs_review", "imbox", ["past date"]),
                ("owner closed", "reply", "resolved", "paper_trail", ["owner sent last", "waiting is stale"]),
                ("past form", "reply", "needs_review", "imbox", ["past date"]),
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
            )
            self.assertEqual(items[0].time["last_human"], "owner")
            self.assertEqual(items[0].document()["time"]["last_human"], "owner")

    def test_display_name_does_not_hide_a_person_and_a_forged_from_is_not_the_owner(self) -> None:
        with self._db() as conn:
            self._message(conn, 1, "owner@example.com", 1, _stamp(2026, 10, 10), "Ask", "what do you think?")
            self._message(
                conn,
                1,
                '"Ana noreply" <ana@client.com>',
                0,
                _stamp(2026, 10, 11),
                "Contract",
                "Please reply with the contract",
            )
            facts = thread_time_facts(conn, 1, AS_OF)
            self.assertEqual(facts.last_human, "other")
            draft = DecisionDraft(
                next_state="reply",
                lane="imbox",
                rule_id="5",
                rules_version="rules-v4",
                origin="knn",
                source_detail="x",
            )
            self.assertEqual(apply_time(draft, facts).next_state, "reply")
            self._message(conn, 2, '"Ev" <owner@example.com>', 0, _stamp(2026, 10, 11), "Spoof", "hello")
            spoofed = thread_time_facts(conn, 2, AS_OF)
            self.assertEqual(spoofed.last_human, "other")

    def test_a_cue_inside_another_word_and_unicode_digits_do_not_count(self) -> None:
        draft = DecisionDraft(
            next_state="reply",
            lane="imbox",
            rule_id="5",
            rules_version="rules-v4",
            origin="knn",
            source_detail="x",
        )
        with self._db() as conn:
            self._message(
                conn,
                1,
                "other@example.com",
                0,
                _stamp(2026, 10, 16),
                "Recall",
                "Please reply today. Recall: we spoke on 2026-10-01.",
            )
            recalled = thread_time_facts(conn, 1, AS_OF)
            self.assertFalse(recalled.past_meeting)
            self.assertEqual(apply_time(draft, recalled).next_state, "reply")
            self._message(
                conn,
                2,
                "other@example.com",
                0,
                _stamp(2026, 10, 16),
                "Digits",
                "deadline ٢٠٢٥-٠١-٠١ please reply",
            )
            digits = thread_time_facts(conn, 2, AS_OF)
            self.assertEqual(digits.found, ())
            self.assertEqual(apply_time(draft, digits).next_state, "reply")
            self._message(
                conn,
                3,
                "other@example.com",
                0,
                _stamp(2026, 10, 16),
                "Due",
                "Can you approve this? due 2026-01-01 was last year's form",
            )
            due = thread_time_facts(conn, 3, AS_OF)
            adjusted = apply_time(draft, due)
            self.assertEqual(adjusted.next_state, "needs_review")
            self.assertEqual(adjusted.lane, "imbox")

    def test_needs_review_stays_needs_review(self) -> None:
        draft = DecisionDraft(
            next_state="needs_review",
            lane="imbox",
            rule_id="else",
            rules_version="rules-v4",
            origin="rule",
            source_detail="else",
        )
        with self._db() as conn:
            self._message(conn, 1, "other@example.com", 0, _stamp(2026, 10, 16), "Call", "see call 2026-10-01")
            facts = thread_time_facts(conn, 1, AS_OF)
            self.assertTrue(facts.past_meeting)
            adjusted = apply_time(draft, facts)
            self.assertIs(adjusted, draft)

    def test_ate_dia_before_the_message_day_is_next_month(self) -> None:
        with self._db() as conn:
            self._message(
                conn,
                1,
                "other@example.com",
                0,
                _stamp(2026, 10, 20),
                "Pedido",
                "Por favor responde até dia 5.",
            )
            october = thread_time_facts(conn, 1, _stamp(2026, 10, 21))
            self.assertEqual([item.at for item in october.found], [_stamp(2026, 11, 5) // 86400 * 86400])
            self.assertFalse(october.past_deadline)
            self._message(
                conn,
                2,
                "other@example.com",
                0,
                _stamp(2026, 12, 20),
                "Ano",
                "até dia 5",
            )
            december = thread_time_facts(conn, 2, _stamp(2026, 12, 21))
            self.assertEqual([item.at for item in december.found], [calendar.timegm((2027, 1, 5, 0, 0, 0))])

    def test_as_of_rejects_an_out_of_range_clock_and_a_null_date_does_not_overflow(self) -> None:
        for stamp in (-1, 10, 10**12, AS_OF_MAX + 1):
            with self.assertRaises(UsageError):
                as_of(stamp)
        with self._db() as conn:
            self._message(conn, 1, "other@example.com", 0, None, "Empty", "até dia 5")
            facts = thread_time_facts(conn, 1, AS_OF)
            self.assertIsNone(facts.last_at)
            self.assertEqual(facts.found[0].at, calendar.timegm((2026, 11, 5, 0, 0, 0)))
            self._message(conn, 2, "other@example.com", 0, 10**18, "Wild", "até dia 5")
            wild = thread_time_facts(conn, 2, AS_OF)
            self.assertEqual(wild.found[0].at, calendar.timegm((2026, 11, 5, 0, 0, 0)))

    def test_rules_record_time_facts_and_leave_human_and_needs_review(self) -> None:
        rules = load_rules(ROOT / "testdata" / "rules" / "rules-v0.json")
        with self._db() as conn:
            self._message(conn, 1, "other@example.com", 0, AS_OF - 86400, "Fresh", "hello there")
            self._message(conn, 2, "other@example.com", 0, AS_OF - 86400, "Due", "hello there due 2020-01-02")
            self._message(conn, 3, "other@example.com", 0, _stamp(2025, 1, 1), "Old", "hello there")
            self._message(conn, 4, "owner@example.com", 1, _stamp(2025, 1, 1), "Mine", "note")
            self._message(conn, 5, "other@example.com", 0, AS_OF - 86400, "Kept", "hello there due 2020-01-02")
            account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
            conn.execute(
                "INSERT INTO senders(account_id, address_norm, domain) VALUES (?, 'other@example.com', 'example.com')",
                (account_id,),
            )
            sender = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "UPDATE messages SET sender_id = ? WHERE thread_pk != 4",
                (sender,),
            )
            self._decision(conn, 5, "human", "human", "person", "5", None)
            conn.commit()
            apply_rules(conn, now=AS_OF, config=rules)
            fresh = _current(conn, 1)
            due = _current(conn, 2)
            old = _current(conn, 3)
            mine = _current(conn, 4)
            kept = _current(conn, 5)
        self.assertEqual((fresh["origin"], fresh["next_state"], fresh["rule_id"]), ("rule", "reply", "5"))
        self.assertNotIn("time_reason", fresh["evidence"])
        self.assertEqual((due["origin"], due["next_state"], due["lane"], due["rule_id"]), ("rule", "needs_review", "imbox", "5"))
        self.assertIn("past date", due["evidence"])
        self.assertEqual((old["origin"], old["next_state"], old["lane"]), ("rule", "reference", "paper_trail"))
        self.assertIn("last activity is old", old["evidence"])
        self.assertEqual((mine["origin"], mine["next_state"]), ("rule", "needs_review"))
        self.assertEqual((kept["origin"], kept["next_state"]), ("human", "reply"))
        root = os.environ.get("PROOF_DIR")
        if root:
            rows = [
                {"name": "fresh rule", "before": "reply", "after": fresh["next_state"], "lane": fresh["lane"]},
                {"name": "past text date", "before": "reply", "after": due["next_state"], "lane": due["lane"]},
                {"name": "old rule reply", "before": "reply", "after": old["next_state"], "lane": old["lane"]},
                {"name": "owner needs review", "before": "needs_review", "after": mine["next_state"], "lane": mine["lane"]},
                {"name": "human row", "before": "reply", "after": kept["next_state"], "lane": kept["lane"]},
            ]
            (Path(root) / "rules-time.json").write_text(
                json.dumps({"as_of": AS_OF, "rows": rows}, indent=2) + "\n",
                encoding="utf-8",
            )

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


def _current(conn, thread_pk: int):
    return conn.execute(
        """
        SELECT origin, next_state, lane, rule_id, evidence
        FROM decisions
        WHERE thread_pk = ? AND superseded_by IS NULL
        """,
        (thread_pk,),
    ).fetchone()


def _reads_wall_clock(source: str) -> bool:
    tree = ast.parse(source)
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name] = alias.name
                if alias.name == "time" and alias.asname:
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                if node.module == "time" and alias.name == "time":
                    return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _clock_call(node.func, modules):
            return True
    return False


def _clock_call(func: ast.AST, modules: dict[str, str]) -> bool:
    if isinstance(func, ast.Name):
        return modules.get(func.id) == "time.time"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in {"time", "now", "utcnow", "today"}:
        return False
    if isinstance(func.value, ast.Name):
        origin = modules.get(func.value.id, func.value.id)
        if func.attr == "time" and origin == "time":
            return True
        if func.attr in {"now", "utcnow", "today"} and origin in {"datetime", "datetime.datetime"}:
            return True
        if func.attr == "today" and origin in {"date", "datetime.date"}:
            return True
        return False
    if isinstance(func.value, ast.Attribute) and isinstance(func.value.value, ast.Name):
        root = modules.get(func.value.value.id, func.value.value.id)
        if root == "datetime" and func.value.attr == "datetime" and func.attr in {"now", "utcnow", "today"}:
            return True
        if root == "datetime" and func.value.attr == "date" and func.attr == "today":
            return True
    return False


def thread_time_facts_from(last_human: str, age_days: int):
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
