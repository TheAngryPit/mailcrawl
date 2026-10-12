"""An archive from before category keys can be filled without a reimport."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mailcrawl.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "fixtures" / "old-taxonomy" / "pre-category.sql"
NOW = 1_700_000_000


class OldTaxonomyBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old_home = os.environ.get("HOME")
        self.old_taxonomy = os.environ.get("MAILCRAWL_TAXONOMY")
        os.environ["HOME"] = str(self.home)
        os.environ.pop("MAILCRAWL_TAXONOMY", None)

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

    def test_backfill_command_fills_gmail_keys_and_dry_run_does_not_write(self) -> None:
        db = self._old_db()
        before_messages = self._rows(db, "messages")
        filled = self._run(["--json", "--home", str(self.home), "backfill", "--account", "vcl"])
        self.assertEqual(filled, {"account": "vcl", "ok": True, "updated": 2})
        self.assertEqual(self._rows(db, "messages"), before_messages)
        self.assertEqual(self._keys(db), {
            "CATEGORY_PERSONAL": "CATEGORY_PERSONAL",
            "CATEGORY_PROMOTIONS": "newsletter",
            "Example/Custom": None,
            "STARRED": "STARRED",
            "pseudo:archived": None,
        })
        again = self._run(["--json", "--home", str(self.home), "backfill", "--account", "vcl"])
        self.assertEqual(again["updated"], 0)
        self.assertEqual(self._decision_count(db), 0)

        dry = self._run(
            [
                "--json",
                "--home",
                str(self.home),
                "dry-run",
                "--account",
                "vcl",
                "--now",
                str(NOW),
            ]
        )
        self.assertEqual(dry["by_rule"], {"0": 1, "5": 2, "else": 1})
        self.assertEqual(dry["by_state"], {"needs_review": 2, "reply": 2})
        self.assertEqual(dry["undecided"], 1)
        self.assertEqual(
            dry["decisions_by_category"],
            {"CATEGORY_PERSONAL": {"5": 1}, "STARRED": {"0": 1}},
        )
        self.assertEqual(dry["by_category"]["CATEGORY_PERSONAL"], 1)
        self.assertEqual(dry["by_category"]["STARRED"], 1)
        self.assertEqual(dry["by_category"]["CATEGORY_PROMOTIONS"], 0)
        self.assertEqual(dry["inactive_reason"], "gmail_default")
        self.assertEqual(dry["inactive_rules"], ["2", "2-read", "3", "4"])
        self.assertEqual(self._decision_count(db), 0)
        self.assertNotIn("@", json.dumps(dry))
        self.assertNotIn("promo note", json.dumps(dry))

    def test_stats_fills_a_pre_category_archive(self) -> None:
        db = self._old_db()
        stats = self._run(["--json", "--home", str(self.home), "stats", "--account", "vcl"])
        self.assertEqual(stats["messages"], 4)
        self.assertEqual(stats["threads"], 4)
        self.assertEqual(stats["by_category"]["CATEGORY_PERSONAL"], 1)
        self.assertEqual(stats["by_category"]["STARRED"], 1)
        self.assertEqual(stats["by_category"]["CATEGORY_PROMOTIONS"], 0)
        self.assertEqual(stats["uncategorized"], 3)
        self.assertEqual(self._keys(db)["CATEGORY_PERSONAL"], "CATEGORY_PERSONAL")
        self.assertEqual(self._keys(db)["CATEGORY_PROMOTIONS"], "newsletter")
        self.assertEqual(self._decision_count(db), 0)

    def _old_db(self) -> Path:
        self._run(
            [
                "--json",
                "--home",
                str(self.home),
                "init",
                "--account",
                "vcl",
                "--email",
                "ada@example.com",
            ]
        )
        db = self.home / "vcl.db"
        self.assertTrue(db.is_file())
        conn = sqlite3.connect(db)
        try:
            account = conn.execute("SELECT account_id FROM accounts").fetchone()[0]
            self.assertEqual(account, 1)
            conn.executescript(FIXTURE.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()
        return db

    def _run(self, argv: list[str]) -> dict[str, object]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        self.assertEqual(code, 0, buf.getvalue())
        return json.loads(buf.getvalue())

    def _rows(self, db: Path, table: str) -> list[tuple[object, ...]]:
        conn = sqlite3.connect(db)
        try:
            return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        finally:
            conn.close()

    def _keys(self, db: Path) -> dict[str, str | None]:
        conn = sqlite3.connect(db)
        try:
            return dict(conn.execute("SELECT gmail_label_id, catalogue_key FROM labels"))
        finally:
            conn.close()

    def _decision_count(self, db: Path) -> int:
        conn = sqlite3.connect(db)
        try:
            return int(conn.execute("SELECT count(*) FROM decisions").fetchone()[0])
        finally:
            conn.close()
