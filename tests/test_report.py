"""stats and dry-run print counts and do not write."""

import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mailcrawl.advise import Advice, AdviceCase
from mailcrawl.cli import main
from mailcrawl.report import dry_run
from mailcrawl.rules import load_rules
from mailcrawl.store import connect, init_account

NOW = 1_700_000_000
ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "testdata" / "rules" / "rules-v0.json"


class StatsDryRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old_home = os.environ.get("HOME")
        self.old_taxonomy = os.environ.get("MAILCRAWL_TAXONOMY")
        os.environ["HOME"] = str(self.home)
        os.environ.pop("MAILCRAWL_TAXONOMY", None)
        self.db = init_account(self.home, "vcl", "ada@example.com")
        conn = connect(self.db)
        try:
            account = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
            self._seed(conn, account)
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        if self.old_taxonomy is None:
            os.environ.pop("MAILCRAWL_TAXONOMY", None)
        else:
            os.environ["MAILCRAWL_TAXONOMY"] = self.old_taxonomy
        self.tmp.cleanup()

    def test_stats_and_dry_run_print_counts_only(self) -> None:
        rules = self._run(
            [
                "--json",
                "--home",
                str(self.home),
                "rules",
                "--account",
                "vcl",
                "--rules",
                str(RULES),
                "--now",
                str(NOW),
            ]
        )
        self.assertEqual(rules["by_state"], {"needs_review": 1, "reply": 1})
        before = self._decision_count()

        stats = self._run(
            ["--json", "--home", str(self.home), "stats", "--account", "vcl"]
        )
        dry = self._run(
            ["--json", "--home", str(self.home), "dry-run", "--account", "vcl"]
        )

        categories = {
            "CATEGORY_FORUMS": 0,
            "CATEGORY_PERSONAL": 0,
            "CATEGORY_PROMOTIONS": 0,
            "CATEGORY_SOCIAL": 0,
            "CATEGORY_UPDATES": 0,
            "CHAT": 0,
            "DRAFT": 0,
            "IMPORTANT": 0,
            "INBOX": 0,
            "SENT": 0,
            "SPAM": 0,
            "STARRED": 1,
            "TRASH": 0,
            "UNREAD": 0,
        }
        self.assertEqual(
            stats,
            {
                "by_category": categories,
                "by_month": {"2023-11": 2},
                "messages": 2,
                "missing_labels": [],
                "needs_review_else": 0,
                "needs_review_rule0": 1,
                "ok": True,
                "senders": 2,
                "taxonomy_warnings": 0,
                "threads": 2,
                "uncategorized": 2,
            },
        )
        self.assertEqual(
            dry,
            {
                "by_category": categories,
                "by_label": {"STARRED": 1},
                "by_month": {"2023-11": 2},
                "by_rule": {"0": 1, "5": 1},
                "by_sender": {"1": 1, "2": 1},
                "by_state": {"needs_review_rule0": 1, "reply": 1},
                "decisions_by_category": {"STARRED": {"0": 1}},
                "inactive_reason": "gmail_default",
                "inactive_rules": ["2", "2-read", "3", "4"],
                "missing_labels": [],
                "ok": True,
                "taxonomy_warnings": 0,
                "undecided": 0,
            },
        )
        self.assertEqual(self._decision_count(), before)
        self.assertFalse(self._has_plans())
        blob = json.dumps({"stats": stats, "dry": dry})
        self.assertNotIn("@", blob)
        self.assertNotIn("secretbodyqx", blob)

    def test_advise_fills_else_rows_and_leaves_the_rule(self) -> None:
        self._else_thread()
        plain_path = self.home / "plain.csv"
        plain = self._run(
            [
                "--json",
                "--home",
                str(self.home),
                "dry-run",
                "--account",
                "vcl",
                "--now",
                str(NOW),
                "--rules",
                str(RULES),
                "--export-decisions",
                str(plain_path),
            ]
        )
        self.assertNotIn("advice", plain)
        self.assertEqual(plain["by_rule"]["else"], 1)
        self.assertEqual(plain["by_rule"]["5"], 1)
        plain_rows = {row["gmail_thread_id"]: row for row in self._csv(plain_path)}
        self.assertEqual(plain_rows["rules-else"]["rule"], "else")
        self.assertEqual(plain_rows["rules-else"]["suggested_rule"], "")
        self.assertEqual(plain_rows["rules-else"]["suggested_lane"], "")
        self.assertEqual(plain_rows["rules-else"]["suggested_confidence"], "")
        self.assertEqual(plain_rows["rules-2"]["suggested_rule"], "")
        before = self._decision_count()

        advised_path = self.home / "advised.csv"
        flagged = self._run(
            [
                "--json",
                "--home",
                str(self.home),
                "dry-run",
                "--account",
                "vcl",
                "--now",
                str(NOW),
                "--rules",
                str(RULES),
                "--advise",
                "--export-decisions",
                str(advised_path),
            ]
        )
        self.assertEqual(
            flagged["advice"],
            {"applied": False, "else_threads": 1, "model": "por definir"},
        )
        self.assertEqual(flagged["by_rule"]["else"], 1)
        advised_rows = {row["gmail_thread_id"]: row for row in self._csv(advised_path)}
        self.assertEqual(advised_rows["rules-else"]["rule"], "else")
        self.assertEqual(advised_rows["rules-else"]["lane"], "imbox")
        self.assertEqual(advised_rows["rules-else"]["suggested_rule"], "5")
        self.assertEqual(advised_rows["rules-else"]["suggested_lane"], "imbox")
        self.assertEqual(advised_rows["rules-else"]["suggested_confidence"], "0.00")
        self.assertEqual(advised_rows["rules-1"]["rule"], "0")
        self.assertEqual(advised_rows["rules-1"]["suggested_rule"], "")
        self.assertEqual(advised_rows["rules-2"]["rule"], "5")
        self.assertEqual(advised_rows["rules-2"]["suggested_confidence"], "")
        self.assertEqual(self._decision_count(), before)
        blob = advised_path.read_text(encoding="utf-8")
        self.assertNotIn("secretbodyqx", blob)
        self.assertNotIn("@", blob)

    def test_a_caller_can_replace_the_stub(self) -> None:
        self._else_thread()
        seen: list[str] = []

        class Fixed:
            def advise(self, case: AdviceCase) -> Advice:
                seen.append(case.rule_id)
                return Advice("7", "paper_trail", "0.10")

        conn = connect(self.db)
        try:
            report = dry_run(
                conn,
                now=NOW,
                config=load_rules(RULES),
                advisor=Fixed(),
            )
        finally:
            conn.close()
        self.assertEqual(seen, ["else"])
        self.assertEqual(report["by_rule"]["else"], 1)
        self.assertEqual(report["advice"]["applied"], False)
        self.assertEqual(self._decision_count(), 0)

    def _else_thread(self) -> None:
        conn = connect(self.db)
        try:
            account = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
            conn.execute(
                "INSERT INTO senders(account_id, address_norm, domain) VALUES (?, ?, 'example.com')",
                (account, "no-reply@example.com"),
            )
            sender = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO threads(account_id, gmail_thread_id) VALUES (?, ?)",
                (account, "rules-else"),
            )
            thread = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, sender_id,
                  body_text, is_from_me, raw_sha256, provenance_hash, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'raw', 'prov', ?)
                """,
                (account, "rules-msg-else", thread, NOW - 86400, sender, "hello there", NOW),
            )
            conn.commit()
        finally:
            conn.close()

    def _csv(self, path: Path) -> list[dict[str, str]]:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def _run(self, argv: list[str]) -> dict[str, object]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def _decision_count(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return int(conn.execute("SELECT count(*) FROM decisions").fetchone()[0])
        finally:
            conn.close()

    def _has_plans(self) -> bool:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plans'"
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def _seed(self, conn: sqlite3.Connection, account: int) -> None:
        def add(number: int, body: str, starred: bool) -> None:
            conn.execute(
                "INSERT INTO senders(account_id, address_norm, domain) VALUES (?, ?, 'example.com')",
                (account, f"bao{number}@example.com"),
            )
            sender = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO threads(account_id, gmail_thread_id) VALUES (?, ?)",
                (account, f"rules-{number}"),
            )
            thread = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, sender_id,
                  body_text, is_from_me, raw_sha256, provenance_hash, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'raw', 'prov', ?)
                """,
                (account, f"rules-msg-{number}", thread, NOW - 86400, sender, body, NOW),
            )
            if starred:
                msg = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
                    VALUES (?, 'STARRED', 'STARRED', 'system', ?)
                    """,
                    (account, NOW),
                )
                conn.execute(
                    """
                    INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                    VALUES (?, ?, 'STARRED', ?)
                    """,
                    (msg, account, NOW),
                )

        add(1, "secretbodyqx", True)
        add(2, "hello there", False)
