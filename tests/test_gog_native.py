"""Import a synthetic gog backup export with no export.json."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mailcrawl.gognative import ExportInvalid
from mailcrawl.ingest import import_gog_export
from mailcrawl.store import connect, init_account, ordered_row_dump_hash

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "testdata" / "fixtures" / "gog-export" / "native"
TAXONOMY = ROOT / "testdata" / "catalogue" / "taxonomy.synthetic.json"
ACCOUNT = NATIVE / "gmail" / "acct_synthetic"


def run_mailcrawl(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["http_proxy"] = "http://127.0.0.1:9"
    env["https_proxy"] = "http://127.0.0.1:9"
    env.pop("MAILCRAWL_HOME", None)
    return subprocess.run(
        [sys.executable, "-m", "mailcrawl", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class NativeImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = init_account(self.home, "thehive", "ada@example.com")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_native_layout_maps_labels_mime_and_search(self) -> None:
        conn = connect(self.db)
        try:
            first = import_gog_export(
                conn,
                alias="thehive",
                export_dir=NATIVE,
                taxonomy_path=TAXONOMY,
                account_email="ada@example.com",
            )
            self.assertEqual(first.n_seen, 3)
            self.assertEqual(first.n_new, 3)
            self.assertEqual(first.n_dup, 0)
            self.assertEqual(first.n_error, 0)
            digest = ordered_row_dump_hash(conn)

            plain = conn.execute(
                """
                SELECT subject, body_text, from_hdr, to_hdr, cc_hdr, internal_date,
                       gmail_history_id, snippet, size_bytes, rfc822_message_id
                FROM messages WHERE gmail_message_id = 'msg-plain'
                """
            ).fetchone()
            self.assertEqual(plain["subject"], "Fwd: Reunião de exemplo")
            self.assertIn("A reunião sintética fica no índice.", plain["body_text"])
            self.assertEqual(plain["from_hdr"], "Ada Fixture <ada@example.com>")
            self.assertEqual(plain["to_hdr"], "Bao Fixture <bao@example.com>")
            self.assertEqual(plain["cc_hdr"], "Cam Fixture <cam@example.com>")
            self.assertEqual(plain["internal_date"], 1775124000)
            self.assertEqual(plain["gmail_history_id"], "1001")
            self.assertIsNone(plain["snippet"])
            self.assertEqual(plain["size_bytes"], 400)
            self.assertEqual(plain["rfc822_message_id"], "<msg-plain@example.com>")

            names = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT headers.name FROM headers
                    JOIN messages ON messages.msg_pk = headers.msg_pk
                    WHERE messages.gmail_message_id = 'msg-plain'
                    """
                )
            }
            self.assertIn("x-mailer", names)
            self.assertIn("list-unsubscribe", names)
            self.assertIn("message-id", names)
            self.assertNotIn("x-not-stored", names)

            label = conn.execute(
                """
                SELECT name, kind, in_catalogue FROM labels
                WHERE gmail_label_id = 'Label_synthetic'
                """
            ).fetchone()
            self.assertEqual(label["name"], "Example/Synthetic")
            self.assertEqual(label["kind"], "user")
            self.assertEqual(label["in_catalogue"], 1)
            starred = conn.execute(
                "SELECT name, kind FROM labels WHERE gmail_label_id = 'STARRED'"
            ).fetchone()
            self.assertEqual(starred["name"], "STARRED")
            self.assertEqual(starred["kind"], "system")

            html = conn.execute(
                """
                SELECT body_text, to_hdr, gmail_history_id, in_reply_to
                FROM messages WHERE gmail_message_id = 'msg-html'
                """
            ).fetchone()
            self.assertIn("boletim", html["body_text"])
            self.assertIn("citação fica", html["body_text"])
            self.assertEqual(html["to_hdr"], "Ada Fixture <ada@example.com>")
            self.assertIsNone(html["gmail_history_id"])
            self.assertEqual(html["in_reply_to"], "<msg-plain@example.com>")

            parts = conn.execute(
                """
                SELECT filename, mime_type, attachments.size_bytes, extract_status,
                       extracted_text, file_sha256, extract_method
                FROM attachments
                JOIN messages ON messages.msg_pk = attachments.msg_pk
                WHERE messages.gmail_message_id = 'msg-parts'
                ORDER BY filename
                """
            ).fetchall()
            self.assertEqual([row["filename"] for row in parts], ["blob.bin", "note.txt"])
            binary = parts[0]
            text = parts[1]
            self.assertEqual(binary["mime_type"], "application/octet-stream")
            self.assertEqual(binary["size_bytes"], 17)
            self.assertEqual(binary["extract_status"], "unsupported")
            self.assertIsNone(binary["extracted_text"])
            self.assertIsNone(binary["extract_method"])
            self.assertEqual(len(binary["file_sha256"]), 64)
            self.assertEqual(len(text["file_sha256"]), 64)
            self.assertEqual(text["extract_status"], "ok")
            self.assertIn("attachmenttoken", text["extracted_text"])
            body = conn.execute(
                "SELECT body_text, raw_json FROM messages WHERE gmail_message_id = 'msg-parts'"
            ).fetchone()
            self.assertIn("Plain body stays.", body["body_text"])
            self.assertNotIn("ZZBINMARKERZZ", body["body_text"])
            self.assertEqual(json.loads(body["raw_json"]), {"vendorExtra": "keep-raw"})

            self.assertEqual(
                conn.execute(
                    "SELECT subject_norm FROM threads WHERE gmail_thread_id = 'thread-plain'"
                ).fetchone()[0],
                "Reunião de exemplo",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM msg_fts WHERE msg_fts MATCH 'reuniao'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM msg_fts WHERE msg_fts MATCH 'boletim'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM msg_fts WHERE msg_fts MATCH 'attachmenttoken'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM msg_fts WHERE msg_fts MATCH 'ZZBINMARKERZZ'"
                ).fetchone()[0],
                0,
            )

            second = import_gog_export(
                conn,
                alias="thehive",
                export_dir=ACCOUNT,
                taxonomy_path=TAXONOMY,
                account_email="ada@example.com",
            )
            self.assertEqual(second.n_seen, 3)
            self.assertEqual(second.n_new, 0)
            self.assertEqual(second.n_dup, 3)
            self.assertEqual(second.n_error, 0)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM messages").fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM message_labels").fetchone()[0],
                5,
            )
            self.assertEqual(ordered_row_dump_hash(conn), digest)
        finally:
            conn.close()

    def test_changed_eml_keeps_the_stored_row(self) -> None:
        conn = connect(self.db)
        try:
            import_gog_export(
                conn,
                alias="thehive",
                export_dir=NATIVE,
                taxonomy_path=None,
                account_email="ada@example.com",
            )
            changed = self.home / "changed"
            shutil.copytree(NATIVE, changed)
            eml = changed / "gmail/acct_synthetic/messages/2026/04/20260402T100000Z-msg-plain.eml"
            eml.write_bytes(eml.read_bytes() + b"\nA different synthetic body.\n")
            result = import_gog_export(
                conn,
                alias="thehive",
                export_dir=changed,
                taxonomy_path=None,
                account_email="ada@example.com",
            )
            self.assertEqual(result.n_error, 1)
            self.assertEqual(result.n_new, 0)
            body = conn.execute(
                "SELECT body_text FROM messages WHERE gmail_message_id = 'msg-plain'"
            ).fetchone()[0]
            self.assertIn("A reunião sintética fica no índice.", body)
            self.assertNotIn("different synthetic body", body)
        finally:
            conn.close()

    def test_missing_unsafe_and_malformed_exports_fail_clearly(self) -> None:
        conn = connect(self.db)
        try:
            missing = self._rewrite_eml("gmail/acct_synthetic/messages/2026/04/missing.eml")
            with self.assertRaises(ExportInvalid) as missing_exc:
                import_gog_export(
                    conn,
                    alias="thehive",
                    export_dir=missing,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
            self.assertEqual(missing_exc.exception.error, "missing_file")
            self.assertIn("msg-plain", str(missing_exc.exception))
            self.assertNotIn("@", str(missing_exc.exception))
            self.assertNotIsInstance(missing_exc.exception, FileNotFoundError)

            unsafe = self._rewrite_eml("../outside.eml")
            with self.assertRaises(ExportInvalid) as unsafe_exc:
                import_gog_export(
                    conn,
                    alias="thehive",
                    export_dir=unsafe,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
            self.assertEqual(unsafe_exc.exception.error, "unsafe_path")
            self.assertNotIn("@", str(unsafe_exc.exception))

            escaped = self._rewrite_eml("gmail/acct_synthetic/../../outside.eml")
            with self.assertRaises(ExportInvalid) as escaped_exc:
                import_gog_export(
                    conn,
                    alias="thehive",
                    export_dir=escaped,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
            self.assertEqual(escaped_exc.exception.error, "unsafe_path")

            outside = self.home / "outside-secret.eml"
            outside.write_text("not a message\n", encoding="utf-8")
            linked = self._rewrite_eml("gmail/acct_synthetic/messages/2026/04/link.eml")
            link = linked / "gmail/acct_synthetic/messages/2026/04/link.eml"
            link.symlink_to(outside)
            with self.assertRaises(ExportInvalid) as link_exc:
                import_gog_export(
                    conn,
                    alias="thehive",
                    export_dir=linked,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
            self.assertEqual(link_exc.exception.error, "unsafe_path")

            malformed = self.home / "malformed"
            shutil.copytree(NATIVE, malformed)
            index = malformed / "gmail/acct_synthetic/messages/index.jsonl"
            rows = [
                json.loads(line)
                for line in index.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            rows[0]["to"] = "Bao Fixture <bao@example.com>"
            index.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ExportInvalid) as bad_exc:
                import_gog_export(
                    conn,
                    alias="thehive",
                    export_dir=malformed,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
            self.assertEqual(bad_exc.exception.error, "malformed_record")
            self.assertEqual(bad_exc.exception.exit_code, 3)
            self.assertIn("not an array of strings", str(bad_exc.exception))
            self.assertNotIn("@", str(bad_exc.exception))
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        finally:
            conn.close()

    def test_empty_directory_is_an_unsupported_layout(self) -> None:
        empty = self.home / "empty"
        empty.mkdir()
        conn = connect(self.db)
        try:
            with self.assertRaises(ExportInvalid) as exc:
                import_gog_export(
                    conn,
                    alias="thehive",
                    export_dir=empty,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
            self.assertEqual(exc.exception.error, "unsupported_schema")
            self.assertEqual(exc.exception.exit_code, 3)
            self.assertNotIsInstance(exc.exception, FileNotFoundError)
        finally:
            conn.close()

    def _rewrite_eml(self, eml: str) -> Path:
        folder = self.home / "rewrite" / str(len(list(self.home.glob("rewrite/*"))))
        shutil.copytree(NATIVE, folder)
        index = folder / "gmail/acct_synthetic/messages/index.jsonl"
        lines = index.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["eml"] = eml
        lines[0] = json.dumps(first, ensure_ascii=False)
        index.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return folder


class NativeCliTest(unittest.TestCase):
    def test_cli_imports_native_export_and_rejects_a_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            init = run_mailcrawl(
                home,
                "--json",
                "init",
                "--account",
                "thehive",
                "--email",
                "ada@example.com",
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            imported = run_mailcrawl(
                home,
                "--json",
                "import",
                "gog-export",
                "--account",
                "thehive",
                "--dir",
                str(NATIVE),
                "--taxonomy",
                str(TAXONOMY),
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            doc = json.loads(imported.stdout)
            self.assertEqual(doc["n_seen"], 3)
            self.assertEqual(doc["n_new"], 3)
            found = run_mailcrawl(
                home,
                "--json",
                "search",
                "reuniao",
                "--account",
                "thehive",
            )
            self.assertEqual(found.returncode, 0, found.stderr)
            hits = json.loads(found.stdout)["hits"]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["gmail_message_id"], "msg-plain")
            self.assertIn("reunião", hits[0]["snippet"])
            again = run_mailcrawl(
                home,
                "--json",
                "import",
                "gog-export",
                "--account",
                "thehive",
                "--dir",
                str(NATIVE),
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout)["n_dup"], 3)

            failed = run_mailcrawl(
                home,
                "--json",
                "import",
                "gog-export",
                "--account",
                "thehive",
                "--dir",
                str(home / "missing-export"),
            )
            self.assertEqual(failed.returncode, 3, failed.stdout)
            body = json.loads(failed.stdout)
            self.assertEqual(body["error"], "unsupported_schema")
            self.assertIn("detail", body)
            self.assertNotIn("Traceback", failed.stderr)
            self.assertNotIn("Traceback", failed.stdout)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
