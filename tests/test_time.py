"""Time facts veto a copied or scored state. The clock stays in one module."""

import ast
import calendar
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from mailcrawl.clock import AS_OF_MAX, as_of
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.inference import invoke
from mailcrawl.jev import MODEL_ID, FakeJev, jev_decide
from mailcrawl.paths import UsageError
from mailcrawl.review import apply_agent_decision, parse_agent_decision, undo_decisions
from mailcrawl.contract import CONTRACT_V0
from mailcrawl.ingest import import_gog_export, import_mbox
from mailcrawl.mbox import gmail_hex
from mailcrawl.rules import apply_rules, bundled_rules_path, load_rules
from mailcrawl.schema import SCHEMA_SQL, USER_VERSION
from mailcrawl.store import connect, init_account, migrate
from mailcrawl.textnorm import parse_date
from mailcrawl.time_cues import CUES, MONTHS
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
            self.assertFalse(
                _reads_wall_clock(path.read_text(encoding="utf-8"), path.name),
                path.name,
            )
        clock = (ROOT / "mailcrawl" / "clock.py").read_text(encoding="utf-8")
        self.assertIn("time.time(", clock)
        self.assertEqual(as_of(AS_OF, 1), AS_OF)
        self.assertEqual(as_of(None, AS_OF), AS_OF)
        for sample in (
            "from time import time\n",
            "import time as t\n",
            "from time import *\n",
            "import datetime\ndatetime.datetime.utcnow()\n",
            "from datetime import datetime\ndatetime.now()\n",
            "from datetime import date\ndate.today()\n",
            "import time\ntime.time()\n",
            "import time\ntime.time_ns()\n",
            "import time\ntime.monotonic()\n",
            "import time\ntime.monotonic_ns()\n",
            "import time\ntime.gmtime()\n",
            "import time\ntime.localtime()\n",
            "import time\ntime.ctime()\n",
            "import time\ntime.asctime()\n",
            "import time\ntime.strftime('%Y')\n",
            "import time\ngetattr(time, 'time')()\n",
            "__import__('time').time()\n",
            "import datetime\ndatetime.datetime.fromtimestamp()\n",
            "import datetime, time\ndatetime.datetime.fromtimestamp(time.time())\n",
            "import time\ntime.perf_counter()\n",
            "import datetime\nn = datetime.datetime.now\n",
            "from datetime import datetime\nn = datetime.now\n",
            "import importlib\nimportlib.import_module('time')\n",
            "from importlib import import_module\nimport_module('time')\n",
            "from importlib import import_module as load\nload('time')\n",
            "from importlib import import_module\nimport_module('datetime').datetime.now()\n",
            "x = \"SELECT datetime('now')\"\n",
            "x = \"unixepoch('now')\"\n",
            "x = 'CURRENT_TIMESTAMP'\n",
            "x = 'CURRENT_DATE'\n",
            "x = 'CURRENT_TIME'\n",
            "x = \"SELECT 'now'\"\n",
        ):
            self.assertTrue(_reads_wall_clock(sample), sample)
        self.assertFalse(_reads_wall_clock("import time\ntime.sleep(1)\n", "jev.py"))
        self.assertTrue(_reads_wall_clock("import time\ntime.sleep(1)\n", "rules.py"))
        self.assertFalse(_reads_wall_clock("import time\ntime.perf_counter()\n", "bench.py"))
        self.assertFalse(_reads_wall_clock("x = 'time.time()'\n"))
        self.assertFalse(
            _reads_wall_clock(
                "import datetime\ndatetime.datetime.fromtimestamp(1, datetime.timezone.utc)\n",
                "report.py",
            )
        )

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

    def test_non_latin_months_match_cldr_and_hindi_keeps_marks(self) -> None:
        import unicodedata

        source = json.loads((ROOT / "testdata" / "fixtures" / "cldr-months.json").read_text(encoding="utf-8"))
        expected: dict[str, int] = {}
        for names in source.values():
            for index, name in enumerate(names, start=1):
                folded = " ".join(unicodedata.normalize("NFC", name).casefold().split())
                key = unicodedata.normalize("NFC", folded)
                if key[:1].isdigit():
                    continue
                if all(ord(char) < 128 for char in key):
                    continue
                expected[key] = index
        for key, month in expected.items():
            self.assertEqual(MONTHS.get(key), month, key)
        for key, month in MONTHS.items():
            if all(ord(char) < 128 for char in key):
                continue
            self.assertEqual(unicodedata.normalize("NFC", key), key, key)
            self.assertEqual(expected.get(key), month, key)
        self.assertEqual(CUES.get("बैठक"), "meeting")
        self.assertNotIn("बठक", CUES)

    def test_cited_help_names_include_the_review_gaps(self) -> None:
        from mailcrawl.takeout_labels import system_id

        self.assertEqual(system_id("Recibidos"), "INBOX")
        self.assertEqual(system_id("Notificaciones"), "CATEGORY_UPDATES")
        self.assertEqual(system_id("Benachrichtigungen"), "CATEGORY_UPDATES")

    def test_multilingual_cues_and_the_new_patterns_finish(self) -> None:
        cases = (
            (11, "plazo 15 de marzo de 2025", "deadline", (2025, 3, 15)),
            (12, "échéance 15 mars 2025", "deadline", (2025, 3, 15)),
            (13, "frist 15. märz 2020", "deadline", (2020, 3, 15)),
            (14, "期限 2025年3月15日", "deadline", (2025, 3, 15)),
            (15, "会議 2025年3月15日", "meeting", (2025, 3, 15)),
            (16, "plazox 15 de marzo de 2025", "date", (2025, 3, 15)),
        )
        with self._db() as conn:
            for thread_pk, body, kind, ymd in cases:
                self._message(conn, thread_pk, "other@example.com", 0, _stamp(2026, 10, 1), "Form", body)
                facts = thread_time_facts(conn, thread_pk, AS_OF)
                self.assertEqual(facts.found[0].kind, kind, body)
                self.assertEqual(facts.found[0].at, calendar.timegm((*ymd, 0, 0, 0)), body)
            started = time.perf_counter()
            for thread_pk, body in (
                (21, "1 de " * 2000),
                (22, "a" * TEXT_CAP),
                (23, "期限" * 2000),
                (24, "1. " * 2000),
                (25, "2025年" * 500),
            ):
                self._message(conn, thread_pk, "other@example.com", 0, _stamp(2026, 10, 1), "Form", body)
                thread_time_facts(conn, thread_pk, AS_OF)
            self.assertLess(time.perf_counter() - started, 1.0)

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
                "needs_review",
                "imbox",
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
                "needs_review",
                "imbox",
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
                    if reason in ("waiting is stale", "last activity is old"):
                        self.assertEqual(
                            evidence[1]["time_closure"],
                            {
                                "lane": "paper_trail",
                                "state": "resolved" if reason == "waiting is stale" else "reference",
                            },
                        )
                table.append((name, before, stored["next_state"], stored["lane"], time_reason))
        self.assertEqual(
            table,
            [
                ("past meeting", "reply", "needs_review", "imbox", ["past date"]),
                ("owner closed", "reply", "needs_review", "imbox", ["owner sent last", "waiting is stale"]),
                ("past form", "reply", "needs_review", "imbox", ["past date"]),
                (
                    "acknowledged quote",
                    "action",
                    "needs_review",
                    "imbox",
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

    def test_knn_does_not_copy_an_unsettled_time_closure(self) -> None:
        if __import__("importlib").util.find_spec("sqlite_vec") is None:
            self.skipTest("sqlite-vec is not installed")
        with self._db() as conn:
            self._message(conn, 1, "other@example.com", 0, _stamp(2025, 2, 10), "Old", "same words")
            self._message(conn, 2, "other@example.com", 0, _stamp(2026, 10, 16), "Young", "same words")
            self._message(conn, 3, "other@example.com", 0, _stamp(2025, 2, 10), "Held", "same words")
            mark = json.dumps(
                [
                    {
                        "time_closure": {"lane": "paper_trail", "state": "reference"},
                        "time_reason": ["last activity is old"],
                    }
                ]
            )
            conn.execute(
                """
                INSERT INTO decisions(
                  thread_pk, next_state, lane, source, origin, source_detail,
                  rule_id, rules_version, evidence, created_at
                ) VALUES (1, 'reference', 'paper_trail', 'rule', 'rule', '5', '5', 'rules-v4', ?, 1)
                """,
                (mark,),
            )
            conn.execute(
                """
                INSERT INTO decisions(
                  thread_pk, next_state, lane, source, origin, source_detail,
                  rule_id, rules_version, evidence, created_at
                ) VALUES (3, 'needs_review', 'imbox', 'rule', 'rule', '5', '5', 'rules-v4', ?, 1)
                """,
                (mark,),
            )
            conn.commit()
            flags = {"embeddings": True}
            invoke("embeddings", lambda: embed_threads(conn, FakeEmbedder(), flags), flags)
            stored = knn_decide(conn, 2, FakeEmbedder(), flags, AS_OF, review=True)
            self.assertIsNone(stored)
            copied = conn.execute(
                "SELECT next_state FROM decisions WHERE thread_pk = 2"
            ).fetchone()
            self.assertIsNone(copied)

    def test_knn_does_not_copy_a_confirmed_keep(self) -> None:
        if __import__("importlib").util.find_spec("sqlite_vec") is None:
            self.skipTest("sqlite-vec is not installed")
        young = _stamp(2026, 10, 15)
        with self._db() as conn:
            self._message(conn, 1, "other@example.com", 0, _stamp(2025, 2, 10), "Old", "same words")
            self._message(conn, 2, "other@example.com", 0, young, "Young", "same words")
            self._message(conn, 3, "other@example.com", 0, young, "Young", "same words")
            self._held_closure(conn, 1)
            kept, changed = apply_agent_decision(
                conn,
                parse_agent_decision(
                    {"action": "keep", "source_detail": "model-b", "thread_id": "thread-1"}
                ),
                AS_OF,
            )
            self.assertTrue(changed)
            self.assertEqual(
                json.loads(kept["evidence"]),
                [{"time_closure": {"lane": "paper_trail", "state": "reference"}}],
            )
            flags = {"embeddings": True}
            invoke("embeddings", lambda: embed_threads(conn, FakeEmbedder(), flags), flags)
            quiet = knn_decide(conn, 2, FakeEmbedder(), flags, AS_OF, review=False)
            loud = knn_decide(conn, 3, FakeEmbedder(), flags, AS_OF, review=True)
            self.assertIsNone(quiet)
            self.assertIsNone(loud)
            copied = conn.execute(
                "SELECT thread_pk, next_state, lane FROM decisions WHERE thread_pk IN (2, 3)"
            ).fetchall()
            self.assertEqual(list(copied), [])

    def test_knn_copies_a_revived_neighbour_and_skips_a_keep(self) -> None:
        if __import__("importlib").util.find_spec("sqlite_vec") is None:
            self.skipTest("sqlite-vec is not installed")
        young = _stamp(2026, 10, 15)
        with self._db() as conn:
            self._message(conn, 1, "other@example.com", 0, _stamp(2025, 2, 10), "Revived", "same words")
            self._message(conn, 2, "other@example.com", 0, young, "Young", "same words")
            self._message(conn, 4, "other@example.com", 0, young, "Young", "same words")
            self._message(conn, 5, "other@example.com", 0, _stamp(2025, 2, 10), "Kept", "same words")
            self._held_closure(conn, 1)
            self._held_closure(conn, 5)
            apply_agent_decision(
                conn,
                parse_agent_decision(
                    {
                        "action": "revive",
                        "kind": "forgotten",
                        "reason": "still owed",
                        "source_detail": "model-c",
                        "state": "reply",
                        "thread_id": "thread-1",
                    }
                ),
                AS_OF,
            )
            apply_agent_decision(
                conn,
                parse_agent_decision(
                    {"action": "keep", "source_detail": "model-b", "thread_id": "thread-5"}
                ),
                AS_OF,
            )
            flags = {"embeddings": True}
            invoke("embeddings", lambda: embed_threads(conn, FakeEmbedder(), flags), flags)
            quiet = knn_decide(conn, 2, FakeEmbedder(), flags, AS_OF, review=False)
            loud = knn_decide(conn, 4, FakeEmbedder(), flags, AS_OF, review=True)
            for stored in (quiet, loud):
                self.assertEqual(stored["origin"], "knn")
                self.assertEqual(stored["next_state"], "reply")
                self.assertEqual(stored["lane"], "imbox")
                self.assertNotIn("time_closure", stored["evidence"])

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
            self.assertEqual(stored["next_state"], "needs_review")
            self.assertEqual(stored["lane"], "imbox")
            evidence = json.loads(stored["evidence"])
            self.assertEqual(evidence[0]["neighbour_thread_pk"], 1)
            self.assertIn("last activity is old", evidence[1]["time_reason"])
            self.assertEqual(evidence[1]["time_closure"], {"lane": "paper_trail", "state": "reference"})

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
                '"Ana noreply" <ana@example.org>',
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
            for thread_pk, body in (
                (4, "plazo ١٥ de marzo de ٢٠٢٥"),
                (5, "deadline १५ marzo २०२५"),
                (6, "plazo １５ de marzo de ２０２５"),
                (7, "期限 ２０२５年３月１５日"),
            ):
                self._message(conn, thread_pk, "other@example.com", 0, _stamp(2026, 10, 16), "Digits", body)
                facts = thread_time_facts(conn, thread_pk, AS_OF)
                self.assertEqual(facts.found, (), body)
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
        self.assertEqual((old["origin"], old["next_state"], old["lane"]), ("rule", "needs_review", "imbox"))
        self.assertIn("last activity is old", old["evidence"])
        self.assertIn('"time_closure":{"lane":"paper_trail","state":"reference"}', old["evidence"])
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

    def test_sent_label_is_the_owner_through_each_importer(self) -> None:
        rules = load_rules(bundled_rules_path())
        body = "Please reply with the signed contract."
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conn = connect(init_account(home, "personal", "ada@example.com"))
            try:
                self._gog(
                    conn,
                    home / "gog",
                    [
                        ("m-forged", "thr", ["INBOX"], _stamp(2026, 10, 16), '"Ada" <ada@example.com>', body),
                        ("m-sent", "thr2", ["SENT"], _stamp(2026, 10, 16), "Mallory <ada@example.com>", "Can you confirm?"),
                    ],
                )
                forged = conn.execute(
                    "SELECT is_from_me, date_trusted FROM messages WHERE gmail_message_id = 'm-forged'"
                ).fetchone()
                sent = conn.execute(
                    "SELECT is_from_me FROM messages WHERE gmail_message_id = 'm-sent'"
                ).fetchone()
                self.assertEqual(int(forged["is_from_me"]), 0)
                self.assertEqual(int(sent["is_from_me"]), 1)
                forged_pk = int(
                    conn.execute("SELECT thread_pk FROM messages WHERE gmail_message_id = 'm-forged'").fetchone()[0]
                )
                facts = thread_time_facts(conn, forged_pk, AS_OF)
                self.assertEqual(facts.last_human, "other")
                self.assertEqual(apply_time(self._reply_draft(), facts).next_state, "reply")
                apply_rules(
                    conn,
                    now=AS_OF,
                    config=rules,
                    owners=frozenset({"ada@example.com"}),
                    domains=frozenset({"example.com"}),
                )
                self.assertEqual(_current(conn, forged_pk)["next_state"], "reply")
                self.assertEqual(_current(conn, forged_pk)["rule_id"], "5")
                self._mbox_sent(conn, home / "takeout.mbox")
                inbox = conn.execute(
                    "SELECT is_from_me FROM messages WHERE gmail_message_id = '2'"
                ).fetchone()
                sent_mbox = conn.execute(
                    "SELECT is_from_me, internal_date, date_trusted FROM messages WHERE gmail_message_id = '1'"
                ).fetchone()
                self.assertEqual(int(inbox["is_from_me"]), 0)
                self.assertEqual(int(sent_mbox["is_from_me"]), 1)
                self.assertEqual(int(sent_mbox["date_trusted"]), 1)
                self.assertEqual(int(sent_mbox["internal_date"]), _stamp(2026, 10, 10))
                self._native_forged(home / "native")
                import_gog_export(
                    conn,
                    alias="personal",
                    export_dir=home / "native",
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
                native = conn.execute(
                    "SELECT is_from_me, date_trusted FROM messages WHERE gmail_message_id = 'native-forged'"
                ).fetchone()
                self.assertEqual((int(native["is_from_me"]), int(native["date_trusted"])), (0, 1))
            finally:
                conn.close()

    def test_forged_date_zero_and_future_only_reach_review(self) -> None:
        rules = load_rules(bundled_rules_path())
        body = "Please reply with the signed contract today."
        cases = (
            ("old", _stamp(2020, 1, 2), None, "needs_review"),
            ("zero", 0, None, "needs_review"),
            ("future", _stamp(2090, 1, 1), None, "reply"),
            ("receipt", _stamp(2020, 1, 2), _stamp(2026, 10, 16), "reply"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conn = connect(init_account(home, "personal", "ada@example.com"))
            try:
                messages = []
                for name, header, receipt, _expected in cases:
                    messages.append(
                        (f"m-{name}", f"thr-{name}", ["INBOX"], header, "Bob <bob@example.org>", body, receipt)
                    )
                self._gog(conn, home / "dates", messages)
                for name, _header, _receipt, expected in cases:
                    row = conn.execute(
                        """
                        SELECT thread_pk, date_trusted, internal_date
                        FROM messages WHERE gmail_message_id = ?
                        """,
                        (f"m-{name}",),
                    ).fetchone()
                    facts = thread_time_facts(conn, int(row["thread_pk"]), AS_OF)
                    adjusted = apply_time(self._reply_draft(), facts)
                    self.assertEqual(adjusted.next_state, expected, name)
                    self.assertNotEqual(adjusted.lane, "paper_trail", name)
                    if name == "receipt":
                        self.assertEqual(int(row["date_trusted"]), 1)
                        self.assertEqual(int(row["internal_date"]), _stamp(2026, 10, 16))
                    elif name == "future":
                        self.assertEqual(int(row["date_trusted"]), 0)
                        self.assertLessEqual(int(row["internal_date"]), as_of(None))
                    else:
                        self.assertEqual(int(row["date_trusted"]), 0)
                apply_rules(
                    conn,
                    now=AS_OF,
                    config=rules,
                    owners=frozenset({"ada@example.com"}),
                )
                for name, _header, _receipt, expected in cases:
                    thread_pk = int(
                        conn.execute(
                            "SELECT thread_pk FROM messages WHERE gmail_message_id = ?",
                            (f"m-{name}",),
                        ).fetchone()[0]
                    )
                    current = _current(conn, thread_pk)
                    self.assertEqual(current["next_state"], expected, name)
                    self.assertNotEqual(current["lane"], "paper_trail", name)
            finally:
                conn.close()

    def test_exact_automated_tokens_and_sent_migration(self) -> None:
        with self._db() as conn:
            self._message(conn, 1, "ada@example.com", 1, _stamp(2026, 10, 10), "Ask", "what do you think?")
            self._message(
                conn,
                1,
                "bob@example.net",
                0,
                _stamp(2026, 10, 16),
                "Reply",
                "Please reply: I need your approval",
            )
            self._message(
                conn,
                2,
                "ana.noreplyfan@example.org",
                0,
                _stamp(2026, 10, 16),
                "Reply",
                "Please reply: approve?",
            )
            mixed = thread_time_facts(conn, 1, AS_OF)
            fan = thread_time_facts(conn, 2, AS_OF)
            self.assertEqual(mixed.last_human, "other")
            self.assertEqual(apply_time(self._reply_draft(), mixed).next_state, "reply")
            self.assertEqual(fan.last_human, "other")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(SCHEMA_SQL)
                conn.execute("ALTER TABLE messages DROP COLUMN date_trusted")
                conn.execute("PRAGMA user_version = 7")
                conn.execute(
                    "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
                )
                conn.execute(
                    "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (1, 't', 2, 0)"
                )
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, internal_date, is_from_me,
                      raw_sha256, provenance_hash, imported_at
                    ) VALUES (1, 'forged', 1, ?, 1, 'a', 'a', 1)
                    """,
                    (_stamp(2026, 10, 16),),
                )
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, internal_date, is_from_me,
                      raw_sha256, provenance_hash, imported_at
                    ) VALUES (1, 'sent', 1, 0, 0, 'b', 'b', 1)
                    """
                )
                sent_pk = int(conn.execute("SELECT msg_pk FROM messages WHERE gmail_message_id = 'sent'").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                    VALUES (?, 1, 'SENT', 1)
                    """,
                    (sent_pk,),
                )
                conn.commit()
                migrate(conn)
                forged = conn.execute(
                    "SELECT is_from_me, date_trusted FROM messages WHERE gmail_message_id = 'forged'"
                ).fetchone()
                sent = conn.execute(
                    "SELECT is_from_me, date_trusted FROM messages WHERE gmail_message_id = 'sent'"
                ).fetchone()
                self.assertEqual((int(forged["is_from_me"]), int(forged["date_trusted"])), (0, 0))
                self.assertEqual((int(sent["is_from_me"]), int(sent["date_trusted"])), (1, 0))
                self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), USER_VERSION)
                conn.execute("PRAGMA user_version = 7")
                conn.execute("UPDATE messages SET is_from_me = 1 WHERE gmail_message_id = 'forged'")
                conn.commit()
                migrate(conn)
                again = conn.execute(
                    "SELECT is_from_me, date_trusted FROM messages WHERE gmail_message_id = 'forged'"
                ).fetchone()
                self.assertEqual((int(again["is_from_me"]), int(again["date_trusted"])), (0, 0))
            finally:
                conn.close()

    def _reply_draft(self) -> DecisionDraft:
        return DecisionDraft(
            next_state="reply",
            lane="imbox",
            rule_id="5",
            rules_version="rules-v4",
            origin="knn",
            source_detail="x",
        )

    def _gog(self, conn, directory: Path, messages: list[tuple]) -> None:
        directory.mkdir()
        payload = []
        for item in messages:
            if len(item) == 6:
                message_id, thread_id, labels, when, sender, body = item
                receipt = None
            else:
                message_id, thread_id, labels, when, sender, body, receipt = item
            row = {
                "example_message_id": message_id,
                "example_thread_id": thread_id,
                "example_label_ids": labels,
                "example_date": when,
                "example_from": sender,
                "example_subject": "Contrato",
                "example_body": body,
            }
            if receipt is not None:
                row["example_internal_date"] = receipt
            payload.append(row)
        document = {
            "schema": CONTRACT_V0,
            "example_manifest": {"messages": len(payload)},
            "example_messages": payload,
        }
        (directory / "export.json").write_text(json.dumps(document), encoding="utf-8")
        import_gog_export(
            conn,
            alias="personal",
            export_dir=directory,
            taxonomy_path=None,
            account_email="ada@example.com",
        )

    def _mbox_sent(self, conn, path: Path) -> None:
        path.write_bytes(
            b"From 1@xxx Sat Oct 10 12:00:00 +0000 2026\n"
            b"X-GM-THRID: 1\n"
            b"X-Gmail-Labels: Sent\n"
            b"Message-ID: <mbox-owner@example.com>\n"
            b"From: Mallory <ada@example.com>\n"
            b"Date: Thu, 02 Jan 2020 00:00:00 +0000\n"
            b"Subject: q\n\n"
            b"Can you confirm?\n"
            b"\n"
            b"From 2@xxx Sat Oct 16 12:00:00 +0000 2026\n"
            b"X-GM-THRID: 2\n"
            b"X-Gmail-Labels: Inbox\n"
            b"Message-ID: <mbox-sent@example.com>\n"
            b"From: Mallory <ada@example.com>\n"
            b"Date: Thu, 02 Jan 2020 00:00:00 +0000\n"
            b"Subject: q\n\n"
            b"Please reply with the signed contract.\n"
        )
        import_mbox(
            conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )

    def _native_forged(self, root: Path) -> None:
        account = root / "gmail" / "acct_synthetic"
        messages = account / "messages"
        messages.mkdir(parents=True)
        (account / "labels.json").write_text(
            json.dumps([{"id": "INBOX", "name": "INBOX", "type": "system"}, {"id": "SENT", "name": "SENT", "type": "system"}]),
            encoding="utf-8",
        )
        eml = messages / "forged.eml"
        eml.write_text(
            "From: Mallory <ada@example.com>\nTo: Bob <bob@example.org>\n"
            "Subject: Contrato\nDate: Thu, 01 Jan 2020 00:00:00 +0000\n"
            "Message-ID: <native-forged@example.com>\n\nPlease reply with the signed contract.\n",
            encoding="utf-8",
        )
        record = {
            "id": "native-forged",
            "threadId": "thr-native",
            "internalDate": _stamp(2026, 10, 16) * 1000,
            "labelIds": ["INBOX"],
            "from": "Mallory <ada@example.com>",
            "date": "Thu, 01 Jan 2020 00:00:00 +0000",
            "eml": "gmail/acct_synthetic/messages/forged.eml",
        }
        (messages / "index.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_sender_gmail_headers_do_not_set_sent_or_the_thread(self) -> None:
        attacker = gmail_hex("1760000000000000001", decimal_only=True)
        self.assertIsNotNone(attacker)
        base = (
            "From: Bob <bob@example.org>\n"
            "To: Ada <ada@example.com>\n"
            "Subject: Contrato\n"
            "Content-Type: text/plain; charset=utf-8\n"
        )
        files = {
            "after-from": (
                "From 1760000000000000002@xxx Wed Oct 15 10:00:00 +0000 2026\n"
                "X-GM-THRID: 1760000000000000002\n"
                + base
                + "X-Gmail-Labels: Sent,Inbox\n"
                "Date: Wed, 15 Oct 2026 10:00:00 +0000\n"
                "Message-ID: <a2@example.org>\n"
                "\nPlease reply with the signed contract today.\n"
            ),
            "plain": (
                "From bob@example.org Wed Oct 15 10:00:00 2026\n"
                + base
                + "X-GMAIL-LABELS: Sent\n"
                "Date: Wed, 15 Oct 2026 10:00:00 +0000\n"
                "Message-ID: <a3@example.org>\n"
                "\nPlease reply with the signed contract today.\n"
            ),
            "thrid": (
                "From 1760000000000000004@xxx Wed Oct 15 10:00:00 +0000 2026\n"
                "X-Gmail-Labels: Inbox\n"
                + base
                + "X-GM-THRID: 1760000000000000001\n"
                "Date: Wed, 15 Oct 2026 10:00:00 +0000\n"
                "Message-ID: <a4@example.org>\n"
                "\nPlease reply with the signed contract today.\n"
            ),
        }
        for name, text in files.items():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                path = home / "x.mbox"
                path.write_text(text, encoding="utf-8")
                conn = connect(init_account(home, "personal", "ada@example.com"))
                try:
                    import_mbox(
                        conn,
                        alias="personal",
                        mbox_path=path,
                        labels_path=None,
                        taxonomy_path=None,
                        account_email="ada@example.com",
                    )
                    row = conn.execute(
                        """
                        SELECT messages.is_from_me, threads.gmail_thread_id, messages.msg_pk
                        FROM messages
                        JOIN threads ON threads.thread_pk = messages.thread_pk
                        """
                    ).fetchone()
                    labels = [
                        item[0]
                        for item in conn.execute(
                            "SELECT gmail_label_id FROM message_labels WHERE msg_pk = ?",
                            (row["msg_pk"],),
                        )
                    ]
                    self.assertEqual(int(row["is_from_me"]), 0, name)
                    self.assertNotIn("SENT", labels, name)
                    self.assertNotEqual(row["gmail_thread_id"], attacker, name)
                    if name == "after-from":
                        self.assertEqual(labels, [], name)
                    if name == "plain":
                        self.assertEqual(labels, [], name)
                    if name == "thrid":
                        self.assertEqual(labels, ["INBOX"], name)
                        self.assertNotIn(str(attacker), str(row["gmail_thread_id"]))
                finally:
                    conn.close()

    def test_leading_gmail_label_needs_a_takeout_separator(self) -> None:
        planted = "186cc6acd4b0004d"
        headers = (
            "X-Gmail-Labels: Sent\n"
            "X-GM-THRID: 1760000000000000077\n"
            "From: Bob <bob@example.org>\n"
            "To: Ada <ada@example.com>\n"
            "Subject: Contrato\n"
            "Date: Wed, 15 Oct 2026 10:00:00 +0000\n"
        )
        files = {
            "plain": (
                "From bob@example.org Wed Oct 15 10:00:00 2026\n"
                + headers
                + "Message-ID: <lead-plain@example.org>\n"
                "\nPlease reply with the signed contract today.\n"
            ),
            "takeout": (
                "From 1760000000000000001@xxx Wed Oct 15 10:00:00 +0000 2026\n"
                + headers
                + "Message-ID: <lead-takeout@example.org>\n"
                "\nPlease reply with the signed contract today.\n"
            ),
            "thrid-only": (
                "From 1760000000000000008@xxx Wed Oct 15 10:00:00 +0000 2026\n"
                "X-GM-THRID: 1760000000000000077\n"
                "From: Bob <bob@example.org>\n"
                "To: Ada <ada@example.com>\n"
                "Subject: Contrato\n"
                "X-Gmail-Labels: Sent\n"
                "Date: Wed, 15 Oct 2026 10:00:00 +0000\n"
                "Message-ID: <lead-later@example.org>\n"
                "\nPlease reply with the signed contract today.\n"
            ),
        }
        for name, text in files.items():
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                path = home / "x.mbox"
                path.write_text(text, encoding="utf-8")
                conn = connect(init_account(home, "personal", "ada@example.com"))
                try:
                    import_mbox(
                        conn,
                        alias="personal",
                        mbox_path=path,
                        labels_path=None,
                        taxonomy_path=None,
                        account_email="ada@example.com",
                    )
                    row = conn.execute(
                        """
                        SELECT messages.is_from_me, threads.gmail_thread_id, messages.msg_pk
                        FROM messages
                        JOIN threads ON threads.thread_pk = messages.thread_pk
                        """
                    ).fetchone()
                    labels = [
                        item[0]
                        for item in conn.execute(
                            "SELECT gmail_label_id FROM message_labels WHERE msg_pk = ?",
                            (row["msg_pk"],),
                        )
                    ]
                    if name == "takeout":
                        self.assertEqual(int(row["is_from_me"]), 1, name)
                        self.assertEqual(labels, ["SENT"], name)
                        self.assertEqual(row["gmail_thread_id"], planted, name)
                    else:
                        self.assertEqual(int(row["is_from_me"]), 0, name)
                        self.assertNotIn("SENT", labels, name)
                        self.assertEqual(labels, [], name)
                        self.assertNotEqual(row["gmail_thread_id"], planted, name)
                finally:
                    conn.close()

    def test_plain_mbox_ignores_a_leading_gmail_message_id(self) -> None:
        planted = "186cc6acd4b0004d"
        rfc822 = "rfc822:8f5a568cbb706cb105b96c4ae70da3090507161c571c7b268f50c7eec598bc08"
        text = (
            "From bob@example.org Wed Oct 15 10:00:00 2026\n"
            "X-GM-MSGID: 1760000000000000077\n"
            "From: Bob <bob@example.org>\n"
            "To: Ada <ada@example.com>\n"
            "Subject: Contrato\n"
            "Date: Wed, 15 Oct 2026 10:00:00 +0000\n"
            "Message-ID: <plain-msgid@example.org>\n"
            "\nPlease reply with the signed contract today.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "x.mbox"
            path.write_text(text, encoding="utf-8")
            conn = connect(init_account(home, "personal", "ada@example.com"))
            try:
                import_mbox(
                    conn,
                    alias="personal",
                    mbox_path=path,
                    labels_path=None,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
                row = conn.execute("SELECT gmail_message_id, is_from_me FROM messages").fetchone()
                self.assertEqual(row["gmail_message_id"], rfc822)
                self.assertNotEqual(row["gmail_message_id"], planted)
                self.assertEqual(int(row["is_from_me"]), 0)
            finally:
                conn.close()

    def test_v7_gog_header_only_date_is_untrusted(self) -> None:
        header = "Thu, 02 Jan 2020 10:00:00 +0000"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            export = home / "x"
            export.mkdir()
            document = {
                "schema": CONTRACT_V0,
                "example_manifest": {"messages": 1},
                "example_messages": [
                    {
                        "example_message_id": "hdr-only",
                        "example_thread_id": "t1",
                        "example_label_ids": ["INBOX"],
                        "example_date": header,
                        "example_from": "Bob <bob@example.org>",
                        "example_subject": "Contrato",
                        "example_body": "Please reply with the signed contract today.",
                    }
                ],
            }
            (export / "export.json").write_text(json.dumps(document), encoding="utf-8")
            loaded = json.loads((export / "export.json").read_text(encoding="utf-8"))
            stamp = parse_date(loaded["example_messages"][0]["example_date"])
            self.assertIsInstance(stamp, int)
            self.assertGreater(stamp, 0)
            path = home / "old.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(SCHEMA_SQL)
                conn.execute("ALTER TABLE messages DROP COLUMN date_trusted")
                conn.execute("PRAGMA user_version = 7")
                conn.execute(
                    "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
                )
                conn.execute(
                    "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (1, 't1', 2, 0)"
                )
                rows = (
                    ("hdr-only", stamp, None),
                    ("missing", stamp, str((home / "gone.eml").resolve())),
                )
                for message_id, when, raw_path in rows:
                    conn.execute(
                        """
                        INSERT INTO messages(
                          account_id, gmail_message_id, thread_pk, internal_date, is_from_me,
                          raw_path, raw_sha256, provenance_hash, imported_at
                        ) VALUES (1, ?, 1, ?, 0, ?, 'a', 'a', 1)
                        """,
                        (message_id, when, raw_path),
                    )
                conn.commit()
                migrate(conn)

                def flags() -> dict[str, tuple[int, int]]:
                    return {
                        str(row[0]): (int(row[1]), int(row[2]))
                        for row in conn.execute(
                            "SELECT gmail_message_id, internal_date, date_trusted FROM messages ORDER BY msg_pk"
                        )
                    }

                got = flags()
                self.assertEqual(got["hdr-only"], (stamp, 0))
                self.assertEqual(got["missing"], (stamp, 0))
                conn.execute("PRAGMA user_version = 7")
                conn.commit()
                migrate(conn)
                self.assertEqual(flags(), got)
            finally:
                conn.close()

    def test_v7_header_dates_stay_untrusted(self) -> None:
        header = "Thu, 02 Jan 2020 10:00:00 +0000"
        receipt = "Wed Oct 15 10:00:00 +0000 2026"
        header_stamp = parse_date(header)
        receipt_stamp = parse_date(receipt)
        self.assertIsInstance(header_stamp, int)
        self.assertIsInstance(receipt_stamp, int)
        self.assertNotEqual(header_stamp, receipt_stamp)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mbox = home / "x.mbox"
            mbox.write_text(
                f"From 1760000000000000005@xxx {receipt}\n"
                "X-GM-THRID: 1760000000000000005\n"
                "X-Gmail-Labels: Inbox\n"
                "From: Bob <bob@example.org>\n"
                f"Date: {header}\n"
                "Message-ID: <a5@example.org>\n"
                "\n"
                "Please reply with the signed contract today.\n"
                "\n"
                f"From 1760000000000000009@xxx {receipt}\n"
                "X-GM-THRID: 1760000000000000009\n"
                "X-Gmail-Labels: Sent\n"
                "From: Ada <ada@example.com>\n"
                "Date: Thu, 16 Oct 2026 11:00:00 +0000\n"
                "Message-ID: <a9@example.com>\n"
                "\n"
                "Here it is.\n",
                encoding="utf-8",
            )
            raw = mbox.read_bytes()
            second = raw.index(b"From 1760000000000000009")
            eml = home / "note.eml"
            eml.write_text(f"From: Bob <bob@example.org>\nDate: {header}\n\nPlease reply.\n", encoding="utf-8")
            other = home / "receipt.eml"
            other.write_text(
                "From: Ada <ada@example.com>\nDate: Thu, 01 Jan 2020 00:00:00 +0000\n\nHere.\n",
                encoding="utf-8",
            )
            path = home / "old.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(SCHEMA_SQL)
                conn.execute("ALTER TABLE messages DROP COLUMN date_trusted")
                conn.execute("PRAGMA user_version = 7")
                conn.execute(
                    "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
                )
                conn.execute(
                    "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (1, 't', 5, 0)"
                )
                header_id = gmail_hex("1760000000000000005", decimal_only=True)
                receipt_id = gmail_hex("1760000000000000009", decimal_only=True)
                rows = (
                    (header_id, header_stamp, f"mbox:{mbox.resolve()}#0"),
                    (receipt_id, receipt_stamp, f"mbox:{mbox.resolve()}#{second}"),
                    ("missing", header_stamp, "mbox:/tmp/does-not-exist-mailcrawl.mbox#0"),
                    ("eml-header", header_stamp, str(eml.resolve())),
                    ("eml-receipt", receipt_stamp, str(other.resolve())),
                )
                for message_id, when, raw_path in rows:
                    conn.execute(
                        """
                        INSERT INTO messages(
                          account_id, gmail_message_id, thread_pk, internal_date, is_from_me,
                          raw_path, raw_sha256, provenance_hash, imported_at
                        ) VALUES (1, ?, 1, ?, 0, ?, 'a', 'a', 1)
                        """,
                        (message_id, when, raw_path),
                    )
                kept = [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT gmail_message_id, is_from_me, internal_date, raw_path
                        FROM messages ORDER BY msg_pk
                        """
                    )
                ]
                conn.commit()
                migrate(conn)

                def flags() -> dict[str, tuple[int, int, int]]:
                    return {
                        str(row[0]): (int(row[1]), int(row[2]), int(row[3]))
                        for row in conn.execute(
                            """
                            SELECT gmail_message_id, is_from_me, internal_date, date_trusted
                            FROM messages ORDER BY msg_pk
                            """
                        )
                    }

                got = flags()
                self.assertEqual(got[header_id], (0, receipt_stamp, 1))
                self.assertEqual(got[receipt_id], (0, receipt_stamp, 1))
                self.assertEqual(got["missing"], (0, header_stamp, 1))
                self.assertEqual(got["eml-header"], (0, header_stamp, 0))
                self.assertEqual(got["eml-receipt"], (0, receipt_stamp, 0))
                conn.execute("PRAGMA user_version = 7")
                conn.commit()
                migrate(conn)
                self.assertEqual(flags(), got)
                after = [
                    tuple(row)
                    for row in conn.execute(
                        """
                        SELECT gmail_message_id, is_from_me, internal_date, raw_path
                        FROM messages ORDER BY msg_pk
                        """
                    )
                ]
                expected = []
                for message_id, mine, stamp, raw_path in kept:
                    if message_id == header_id:
                        stamp = receipt_stamp
                    expected.append((message_id, mine, stamp, raw_path))
                self.assertEqual(after, expected)
            finally:
                conn.close()

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

    def _held_closure(self, conn, thread_pk: int) -> None:
        mark = json.dumps(
            [
                {
                    "time_closure": {"lane": "paper_trail", "state": "reference"},
                    "time_reason": ["last activity is old"],
                }
            ]
        )
        conn.execute(
            """
            INSERT INTO decisions(
              thread_pk, next_state, lane, source, origin, source_detail,
              rule_id, rules_version, evidence, created_at
            ) VALUES (?, 'needs_review', 'imbox', 'rule', 'rule', '5', '5', 'rules-v4', ?, 1)
            """,
            (thread_pk, mark),
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


_TIME_ALLOW = {
    "jev.py": frozenset({"sleep"}),
    "bench.py": frozenset({"perf_counter", "perf_counter_ns"}),
}
_NOW_ATTRS = frozenset({"now", "utcnow", "today"})
_BARE_TIME = frozenset({"gmtime", "localtime", "ctime", "asctime", "strftime"})
_SQL_CLOCK = re.compile(
    r"current_timestamp|current_date|current_time|unixepoch\s*\(\s*['\"]now['\"]|datetime\s*\(\s*['\"]now['\"]|['\"]now['\"]",
    re.IGNORECASE,
)


def _reads_wall_clock(source: str, name: str = "") -> bool:
    if name == "clock.py":
        return False
    tree = ast.parse(source)
    allowed = _TIME_ALLOW.get(name, frozenset())
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                modules[bound] = alias.name
                if alias.name == "time" and not allowed:
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            if any(alias.name == "*" for alias in node.names):
                return True
            for alias in node.names:
                modules[alias.asname or alias.name] = f"time.{alias.name}"
                if alias.name not in allowed:
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Attribute) and _blocked_attr(node, modules, allowed):
            return True
        elif isinstance(node, ast.Call) and _forbidden_call(node, modules, allowed):
            return True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _SQL_CLOCK.search(node.value):
            return True
    return False


def _blocked_attr(node: ast.Attribute, modules: dict[str, str], allowed: frozenset[str]) -> bool:
    if _now_family(node, modules):
        return True
    return isinstance(node.value, ast.Name) and modules.get(node.value.id) == "time" and node.attr not in allowed


def _import_module_name(node: ast.AST, modules: dict[str, str]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    bound = ""
    if isinstance(func, ast.Name):
        bound = modules.get(func.id, func.id)
    elif isinstance(func, ast.Attribute) and func.attr == "import_module" and isinstance(func.value, ast.Name):
        bound = modules.get(func.value.id, func.value.id)
    if bound not in {"importlib", "import_module", "importlib.import_module"}:
        return None
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return None
    return node.args[0].value


def _dynamic_time_import(node: ast.Call, modules: dict[str, str]) -> bool:
    return _import_module_name(node, modules) == "time"


def _forbidden_call(node: ast.Call, modules: dict[str, str], allowed: frozenset[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "__import__":
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "time":
            return True
    if _dynamic_time_import(node, modules):
        return True
    if isinstance(func, ast.Name) and modules.get(func.id, "").startswith("time."):
        used = modules[func.id].split(".", 1)[1]
        return used not in allowed
    if isinstance(func, ast.Attribute) and func.attr == "fromtimestamp":
        if not node.args:
            return True
        return _wall_expr(node.args[0], modules)
    if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
        if len(func.args) >= 2 and isinstance(func.args[1], ast.Constant):
            target = func.args[0]
            if isinstance(target, ast.Name) and modules.get(target.id) == "time":
                return str(func.args[1].value) not in allowed
    if not isinstance(func, ast.Attribute):
        return False
    if isinstance(func.value, ast.Name) and modules.get(func.value.id) == "time":
        if func.attr in allowed:
            return False
        if func.attr in _BARE_TIME and node.args and not (func.attr == "strftime" and len(node.args) < 2):
            if func.attr == "strftime":
                return len(node.args) < 2
            return len(node.args) == 0
        return True
    return _now_family(func, modules)


def _now_family(func: ast.Attribute, modules: dict[str, str]) -> bool:
    if func.attr not in _NOW_ATTRS:
        return False
    if isinstance(func.value, ast.Name):
        origin = modules.get(func.value.id, func.value.id)
        if func.attr in _NOW_ATTRS and origin in {"datetime", "datetime.datetime", "date", "datetime.date"}:
            return True
    if isinstance(func.value, ast.Attribute) and isinstance(func.value.value, ast.Name):
        root = modules.get(func.value.value.id, func.value.value.id)
        if root == "datetime" and func.value.attr == "datetime" and func.attr in _NOW_ATTRS:
            return True
        if root == "datetime" and func.value.attr == "date" and func.attr == "today":
            return True
    if isinstance(func.value, ast.Attribute) and _import_module_name(func.value.value, modules) == "datetime":
        if func.value.attr == "datetime" and func.attr in _NOW_ATTRS:
            return True
        if func.value.attr == "date" and func.attr == "today":
            return True
    return False


def _wall_expr(node: ast.AST, modules: dict[str, str]) -> bool:
    if isinstance(node, ast.Call):
        return _forbidden_call(node, modules, frozenset())
    if isinstance(node, ast.Name):
        return modules.get(node.id, "").startswith("time.")
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return modules.get(node.value.id) == "time"
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
        date_trusted=True,
    )
