# SPDX-License-Identifier: MIT
"""Drive init and import the way a user does."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "testdata" / "fixtures" / "gog-export" / "v0"
TAXONOMY = ROOT / "testdata" / "catalogue" / "taxonomy.synthetic.json"


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


class CliImportTest(unittest.TestCase):
    def test_init_and_import_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            init = run_mailcrawl(
                home,
                "--json",
                "init",
                "--account",
                "pessoal",
                "--email",
                "ada@example.com",
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            init_doc = json.loads(init.stdout)
            self.assertTrue(init_doc["ok"])
            self.assertEqual(init_doc["account"], "personal")
            self.assertTrue(str(init_doc["database"]).endswith("personal.db"))
            data = home / ".local" / "share" / "mailcrawl"
            names = {path.name for path in data.iterdir()}
            self.assertNotIn("index.db", names)
            self.assertNotIn("reads.db", names)
            self.assertNotIn("pessoal.db", names)

            imported = run_mailcrawl(
                home,
                "--json",
                "import",
                "gog-export",
                "--account",
                "personal",
                "--dir",
                str(EXPORT),
                "--taxonomy",
                str(TAXONOMY),
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            doc = json.loads(imported.stdout)
            manifest = json.loads((EXPORT / "export.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["n_seen"], manifest["example_manifest"]["messages"])
            self.assertEqual(doc["n_new"], 2)

            again = run_mailcrawl(
                home,
                "--json",
                "import",
                "gog-export",
                "--account",
                "personal",
                "--dir",
                str(EXPORT),
                "--taxonomy",
                str(TAXONOMY),
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout)["n_dup"], 2)

            found = run_mailcrawl(
                home,
                "--json",
                "search",
                "reuniao",
                "--account",
                "personal",
            )
            self.assertEqual(found.returncode, 0, found.stderr)
            hits = json.loads(found.stdout)["hits"]
            self.assertEqual(len(hits), 1)
            self.assertIn("reunião", hits[0]["snippet"])
            self.assertEqual(hits[0]["gmail_message_id"], "fixture-002")

            broken_dir = home / "broken"
            broken_dir.mkdir()
            broken = json.loads((EXPORT / "export.json").read_text(encoding="utf-8"))
            del broken["example_messages"][0]["example_date"]
            (broken_dir / "export.json").write_text(json.dumps(broken), encoding="utf-8")
            failed = run_mailcrawl(
                home,
                "--json",
                "import",
                "gog-export",
                "--account",
                "personal",
                "--dir",
                str(broken_dir),
            )
            self.assertEqual(failed.returncode, 3, failed.stdout)
            self.assertEqual(json.loads(failed.stdout)["error"], "unsupported_schema")


class CliOperationalErrorTest(unittest.TestCase):
    def test_malformed_database_has_sanitized_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            archive = home / "private-path"
            archive.mkdir()
            database = archive / "personal.db"
            database.write_bytes(b"not a sqlite database")
            failed = run_mailcrawl(home, "--json", "--home", str(archive), "stats", "--account", "personal")
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(json.loads(failed.stdout), {
                "ok": False, "error": "database_error", "detail": "database operation failed",
            })
            self.assertEqual(failed.stderr, "")
            self.assertNotIn(str(home), failed.stdout)
            self.assertEqual(database.read_bytes(), b"not a sqlite database")

    def test_human_output_escapes_untrusted_controls_and_json_preserves_data(self) -> None:
        import argparse
        import contextlib
        import io
        from mailcrawl.cli import _emit

        malicious = "external\x1b[2J\r\nforged\x85line"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            _emit(argparse.Namespace(json=False), {"detail": malicious, "error": malicious})
            _emit(argparse.Namespace(json=False), {}, text=malicious)
        for output in (stdout.getvalue(), stderr.getvalue()):
            self.assertNotIn("\x1b", output)
            self.assertNotIn("\r", output)
            self.assertNotIn("\x85", output)
        self.assertEqual(len(stdout.getvalue().splitlines()), 2)
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            _emit(argparse.Namespace(json=True), {"text": malicious})
        self.assertEqual(json.loads(stdout.getvalue())["text"], malicious)

    def test_reevaluate_csv_escapes_formula_fields(self) -> None:
        import csv
        from mailcrawl.cli import _write_reevaluate

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reevaluate.csv"
            _write_reevaluate(path, [{"gmail_thread_id": "=1+1", "reason": "@SUM(1,2)", "state": "reply"}])
            with path.open(newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["gmail_thread_id"], "'=1+1")
            self.assertEqual(row["reason"], "'@SUM(1,2)")
            self.assertEqual(row["state"], "reply")
