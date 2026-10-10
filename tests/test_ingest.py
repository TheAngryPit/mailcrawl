"""Import the synthetic v0 fixture once, then again."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from mailcrawl.contract import CONTRACT_V0, ExportRejected
from mailcrawl.ingest import import_gog_export
from mailcrawl.store import connect, init_account, ordered_row_dump_hash
from mailcrawl.textnorm import subject_norm
from mailcrawl.version import mailcrawl_version, provenance_hash

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "testdata" / "fixtures" / "gog-export" / "v0"
TAXONOMY = ROOT / "testdata" / "catalogue" / "taxonomy.synthetic.json"


class ImportFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = init_account(self.home, "pessoal", "ada@example.com")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pessoal_creates_personal_db_only(self) -> None:
        names = {path.name for path in self.home.iterdir()}
        self.assertIn("personal.db", names)
        self.assertNotIn("pessoal.db", names)
        self.assertNotIn("index.db", names)
        self.assertNotIn("reads.db", names)
        mode = stat.S_IMODE(self.db.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_import_is_idempotent_and_keeps_body(self) -> None:
        conn = connect(self.db)
        try:
            first = import_gog_export(
                conn,
                alias="personal",
                export_dir=EXPORT,
                taxonomy_path=TAXONOMY,
                account_email="ada@example.com",
            )
            self.assertEqual(first.n_seen, 2)
            self.assertEqual(first.n_new, 2)
            self.assertEqual(first.n_dup, 0)
            digest = ordered_row_dump_hash(conn)
            second = import_gog_export(
                conn,
                alias="personal",
                export_dir=EXPORT,
                taxonomy_path=TAXONOMY,
                account_email="ada@example.com",
            )
            self.assertEqual(second.n_seen, 2)
            self.assertEqual(second.n_new, 0)
            self.assertEqual(second.n_dup, 2)
            self.assertEqual(ordered_row_dump_hash(conn), digest)

            body = conn.execute(
                "SELECT body_text FROM messages WHERE gmail_message_id = 'fixture-001'"
            ).fetchone()[0]
            self.assertIn("> quoted line stays", body)
            self.assertIn("-- \nAda Fixture", body)

            norm = conn.execute(
                "SELECT subject_norm FROM threads WHERE gmail_thread_id = 'thread-002'"
            ).fetchone()[0]
            self.assertEqual(norm, "Reunião de exemplo")

            hello = conn.execute(
                "SELECT subject_norm FROM threads WHERE gmail_thread_id = 'thread-001'"
            ).fetchone()[0]
            self.assertEqual(hello, "Synthetic hello from example.com")

            snippet, history = conn.execute(
                """
                SELECT snippet, gmail_history_id FROM messages
                WHERE gmail_message_id = 'fixture-002'
                """
            ).fetchone()
            self.assertIsNone(snippet)
            self.assertIsNone(history)

            raw = conn.execute(
                "SELECT raw_json FROM messages WHERE gmail_message_id = 'fixture-002'"
            ).fetchone()[0]
            self.assertEqual(json.loads(raw)["example_vendor_extra"], "keep-raw")

            stored = conn.execute(
                """
                SELECT name FROM headers
                JOIN messages ON messages.msg_pk = headers.msg_pk
                WHERE messages.gmail_message_id = 'fixture-001'
                """
            ).fetchall()
            names = {row[0] for row in stored}
            self.assertIn("x-mailer", names)
            self.assertIn("message-id", names)
            self.assertNotIn("x-not-stored", names)

            attachment = conn.execute(
                """
                SELECT extract_status, extracted_text, gmail_attachment_id
                FROM attachments
                """
            ).fetchone()
            self.assertEqual(attachment[0], "pending")
            self.assertIsNone(attachment[1])
            self.assertEqual(attachment[2], "att-synthetic-1")

            row = conn.execute(
                """
                SELECT gmail_message_id, raw_sha256, provenance_hash, account_id
                FROM messages WHERE gmail_message_id = 'fixture-002'
                """
            ).fetchone()
            expected = provenance_hash(
                "personal",
                row["gmail_message_id"],
                row["raw_sha256"],
                mailcrawl_version(),
            )
            self.assertEqual(row["provenance_hash"], expected)
            other = hashlib.sha256(
                "\n".join(
                    (
                        "gog",
                        str(row["account_id"]),
                        row["gmail_message_id"],
                        row["raw_sha256"],
                        mailcrawl_version(),
                    )
                ).encode()
            ).hexdigest()
            self.assertNotEqual(row["provenance_hash"], other)

            flagged = conn.execute(
                """
                SELECT in_catalogue, catalogue_key FROM labels
                WHERE gmail_label_id = 'Label_synthetic'
                """
            ).fetchone()
            self.assertEqual(flagged[0], 1)
            self.assertEqual(flagged[1], "Example/Synthetic")
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM messages WHERE gmail_message_id = 'fixture-001'"
                ).fetchone()[0],
                1,
            )
            fts = conn.execute(
                "SELECT count(*) FROM msg_fts WHERE msg_fts MATCH 'reuniao'"
            ).fetchone()[0]
            self.assertEqual(fts, 1)
        finally:
            conn.close()

    def test_missing_key_writes_nothing(self) -> None:
        document = json.loads((EXPORT / "export.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], CONTRACT_V0)
        del document["example_messages"][1]["example_thread_id"]
        broken = self.home / "broken"
        broken.mkdir()
        (broken / "export.json").write_text(json.dumps(document), encoding="utf-8")
        conn = connect(self.db)
        try:
            with self.assertRaises(ExportRejected):
                import_gog_export(
                    conn,
                    alias="personal",
                    export_dir=broken,
                    taxonomy_path=TAXONOMY,
                    account_email="ada@example.com",
                )
            count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_html_body_keeps_quoted_text(self) -> None:
        document = {
            "schema": CONTRACT_V0,
            "example_manifest": {"messages": 1},
            "example_messages": [
                {
                    "example_message_id": "html-001",
                    "example_thread_id": "thread-html",
                    "example_label_ids": ["INBOX"],
                    "example_date": 1760054400,
                    "example_from": "Ada Fixture <ada@example.com>",
                    "example_html": "<p>Linha html</p><blockquote>citação fica</blockquote>",
                }
            ],
        }
        folder = self.home / "html"
        folder.mkdir()
        (folder / "export.json").write_text(json.dumps(document), encoding="utf-8")
        conn = connect(self.db)
        try:
            import_gog_export(
                conn,
                alias="personal",
                export_dir=folder,
                taxonomy_path=None,
                account_email="ada@example.com",
            )
            body = conn.execute(
                "SELECT body_text FROM messages WHERE gmail_message_id = 'html-001'"
            ).fetchone()[0]
            self.assertIn("Linha html", body)
            self.assertIn("citação fica", body)
        finally:
            conn.close()

    def test_changed_bytes_keep_the_stored_row(self) -> None:
        conn = connect(self.db)
        try:
            import_gog_export(
                conn,
                alias="personal",
                export_dir=EXPORT,
                taxonomy_path=TAXONOMY,
                account_email="ada@example.com",
            )
            document = json.loads((EXPORT / "export.json").read_text(encoding="utf-8"))
            document["example_messages"][1]["example_body"] = "A different synthetic body."
            changed = self.home / "changed"
            changed.mkdir()
            shutil.copy(EXPORT / "note.eml", changed / "note.eml")
            (changed / "export.json").write_text(json.dumps(document), encoding="utf-8")
            result = import_gog_export(
                conn,
                alias="personal",
                export_dir=changed,
                taxonomy_path=TAXONOMY,
                account_email="ada@example.com",
            )
            self.assertEqual(result.n_error, 1)
            self.assertEqual(result.n_new, 0)
            body = conn.execute(
                "SELECT body_text FROM messages WHERE gmail_message_id = 'fixture-002'"
            ).fetchone()[0]
            self.assertEqual(body, "A reunião sintética fica no índice.")
        finally:
            conn.close()


class SubjectNormDirectTest(unittest.TestCase):
    def test_prefix_and_space_rules(self) -> None:
        self.assertEqual(subject_norm("Re: Re: Fwd: Reunião"), "Reunião")
        self.assertEqual(subject_norm("  fw:  hello   world "), "hello world")
        self.assertEqual(subject_norm("reunião"), "reunião")
        self.assertIsNone(subject_norm(None))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
