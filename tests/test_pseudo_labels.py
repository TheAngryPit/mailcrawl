"""Takeout pseudo-labels never act as Gmail labels in rules or plans."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from mailcrawl.contract import GMAIL_LABEL_SQL, is_pseudo_label
from mailcrawl.ingest import import_gog_export, import_mbox
from mailcrawl.report import dry_run
from mailcrawl.rules import apply_rules, load_rules
from mailcrawl.store import connect, init_account

ROOT = Path(__file__).resolve().parents[1]
TAKEOUT = ROOT / "testdata/fixtures/mbox/takeout.mbox"
RULES = load_rules(ROOT / "testdata/rules/rules-v0.json")
NOW = 1767700000
WRITERS = {"ingest.py", "store.py", "schema.py", "takeout_repair.py"}


def without_pseudo_names(text: str) -> str:
    text = text.replace("X-Gmail-Labels: Aberto\n", "")
    for name in (",Opened", "Arquivada,", ",Archived"):
        assert name in text, name
        text = text.replace(name, "")
    return text


class PseudoLabelRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_rules(self, alias: str, mbox: Path, poison: bool = False):
        conn = connect(init_account(self.home, alias, "ada@example.com"))
        try:
            import_mbox(
                conn,
                alias=alias,
                mbox_path=mbox,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.com",
            )
            if poison:
                conn.execute(
                    "UPDATE labels SET catalogue_key = 'type.account', in_catalogue = 1 "
                    "WHERE gmail_label_id LIKE 'pseudo:%'"
                )
            pseudo_links = conn.execute(
                "SELECT count(*) FROM message_labels WHERE gmail_label_id LIKE 'pseudo:%'"
            ).fetchone()[0]
            report = apply_rules(conn, now=NOW, config=RULES)
            decisions = conn.execute(
                """
                SELECT threads.gmail_thread_id, next_state, rule_id, evidence
                FROM decisions JOIN threads ON threads.thread_pk = decisions.thread_pk
                ORDER BY threads.gmail_thread_id
                """
            ).fetchall()
            return pseudo_links, report, [tuple(row) for row in decisions], dry_run(conn)
        finally:
            conn.close()

    def test_rules_and_dry_run_ignore_pseudo_labels(self) -> None:
        plain = self.home / "plain.mbox"
        plain.write_text(without_pseudo_names(TAKEOUT.read_text(encoding="utf-8")), encoding="utf-8")
        links, report, decisions, counts = self.run_rules("personal", TAKEOUT, poison=True)
        base_links, base_report, base_decisions, base_counts = self.run_rules("vcl", plain)
        self.assertEqual((links, base_links), (3, 0))
        self.assertEqual(report, base_report)
        self.assertEqual(decisions, base_decisions)
        self.assertEqual(counts, base_counts)
        self.assertFalse(any("pseudo:" in evidence for *_, evidence in decisions))
        self.assertFalse(any(is_pseudo_label(name) for name in counts["by_label"]))

    def test_a_gog_export_cannot_write_pseudo_labels(self) -> None:
        export = self.home / "export"
        messages = export / "gmail/acct/messages"
        messages.mkdir(parents=True)
        (export / "gmail/acct/labels.json").write_text(
            json.dumps([{"id": "pseudo:opened", "name": "pseudo:opened", "type": "system"}]),
            encoding="utf-8",
        )
        (messages / "x.eml").write_bytes(b"From: Ada Fixture <ada@example.com>\n\nApi copy.\n")
        (messages / "index.jsonl").write_text(
            json.dumps(
                {
                    "id": "abc123",
                    "threadId": "abc123",
                    "internalDate": 1,
                    "labelIds": ["pseudo:opened"],
                    "eml": "gmail/acct/messages/x.eml",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        conn = connect(init_account(self.home, "thehive", "ada@example.com"))
        try:
            result = import_gog_export(
                conn,
                alias="thehive",
                export_dir=export,
                taxonomy_path=None,
                account_email="ada@example.com",
            )
            self.assertEqual(result.n_new, 1)
            pseudo = conn.execute(
                "SELECT (SELECT count(*) FROM labels WHERE gmail_label_id LIKE 'pseudo:%'),"
                " (SELECT count(*) FROM message_labels WHERE gmail_label_id LIKE 'pseudo:%')"
            ).fetchone()
            self.assertEqual(tuple(pseudo), (0, 0))
        finally:
            conn.close()

    def test_every_label_read_outside_the_writer_excludes_pseudo_labels(self) -> None:
        for path in sorted((ROOT / "mailcrawl").glob("*.py")):
            if path.name in WRITERS:
                continue
            source = path.read_text(encoding="utf-8")
            reads = len(re.findall(r"\bFROM message_labels\b|\bJOIN message_labels\b", source))
            guarded = source.count("{GMAIL_LABEL_SQL}")
            with self.subTest(module=path.name):
                self.assertGreaterEqual(guarded, reads)
        self.assertEqual(GMAIL_LABEL_SQL, "labels.gmail_label_id NOT LIKE 'pseudo:%'")


if __name__ == "__main__":
    unittest.main()
