import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mailcrawl.control import metadata

ROOT = Path(__file__).resolve().parents[1]


class CrawlBarContractTest(unittest.TestCase):
    def test_checked_manifest_matches_runtime_metadata(self):
        document = json.loads((ROOT / "integrations/crawlbar/mailcrawl.json").read_text())
        self.assertEqual(document, metadata())

    def test_manifest_commands_run_against_synthetic_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            home = root / "archive"
            config = root / "config.toml"
            export = root / "export"
            shutil.copytree(ROOT / "testdata/fixtures/gog-export/native", export)
            config.write_text('[accounts.thehive]\nemail="ada@example.com"\n'
                              '[sources.daily]\naccount="thehive"\nkind="gog-export"\n'
                              'path="export"\n')
            base = [sys.executable, "-m", "mailcrawl", "--home", str(home), "--config", str(config)]
            init = subprocess.run(base + ["init", "--account", "thehive", "--email", "ada@example.com"], env=env,
                                  cwd=root, capture_output=True, text=True)
            self.assertEqual(init.returncode, 0, init.stderr)
            for name in ("metadata", "sync", "status", "doctor", "search"):
                command = metadata()["commands"][name]["argv"][1:]
                command = [arg.replace("{config:account}", "thehive") for arg in command]
                if name == "search":
                    command += ["reuniao"]
                run = subprocess.run(base + command, env=env, cwd=root,
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, (name, run.stdout, run.stderr))
                result = json.loads(run.stdout)
                if name in ("status", "doctor"):
                    self.assertEqual(result["state"], "ready")
                    self.assertEqual(result["databases"][0]["id"], "thehive")
                    self.assertEqual(result["freshness"]["sources"][0]["state"], "current_local")
                if name == "sync":
                    self.assertEqual(result["sources"][0]["n_new"], 3)
                if name == "search":
                    self.assertTrue(result["read_only"])
                    self.assertEqual(result["hits"][0]["gmail_message_id"], "msg-plain")
