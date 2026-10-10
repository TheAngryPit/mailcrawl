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
