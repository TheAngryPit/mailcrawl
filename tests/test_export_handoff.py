import importlib.util
import os
import signal
import sys
import time
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("refresh_mailcrawl", ROOT / "scripts/refresh-mailcrawl.py")
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)


class ExportHandoffTest(unittest.TestCase):
    def test_failed_or_timed_out_export_does_not_start_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            producer = root / "synthetic-producer"
            producer.write_text("#!/bin/sh\nexit 1\n")
            producer.chmod(0o700)
            config = root / "config.toml"
            config.write_text('[sources.daily]\naccount="thehive"\nkind="gog-export"\npath="export"\n')
            with patch.object(handoff, "sync_sources") as sync:
                self.assertFalse(handoff.refresh(root, config, "daily", producer)["ok"])
                sync.assert_not_called()
            producer.write_text("#!/bin/sh\nsleep 5\n")
            with patch.object(handoff, "sync_sources") as sync:
                self.assertFalse(handoff.refresh(root, config, "daily", producer, 1)["ok"])
                sync.assert_not_called()

    def test_successful_producer_uses_argv_and_precedes_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            producer = root / "synthetic producer"
            marker = root / "producer-called"
            producer.write_text('#!/bin/sh\ntest "$1" = --out || exit 2\n'
                                'touch "' + str(marker) + '"\n')
            producer.chmod(0o700)
            config = root / "config.toml"
            config.write_text('[sources.daily]\naccount="thehive"\nkind="gog-export"\npath="export"\n')
            def imported(*args, **kwargs):
                self.assertTrue(marker.exists())
                return {"ok": True}
            with patch.object(handoff, "sync_sources", side_effect=imported) as sync:
                self.assertTrue(handoff.refresh(root, config, "daily", producer)["ok"])
                sync.assert_called_once_with(root, config, source_id="daily",
                                             expected_source=handoff.configured_sources(config)[0])

    def test_changed_source_during_export_cannot_import_other_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('[sources.daily]\naccount="thehive"\nkind="gog-export"\npath="export"\n')
            producer = root / "producer"
            producer.write_text("#!/bin/sh\nsed 's/path=\"export\"/path=\"other\"/' '" + str(config) +
                                "' > '" + str(config) + ".new'\nmv '" + str(config) +
                                ".new' '" + str(config) + "'\n")
            producer.chmod(0o700)
            with patch("mailcrawl.sources.import_gog_export") as importer:
                result = handoff.refresh(root, config, "daily", producer)
            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "handoff")
            importer.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "wrapper uses POSIX process groups")
    def test_sigterm_closes_owned_producer_group(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('[sources.daily]\naccount="thehive"\nkind="gog-export"\npath="export"\n')
            marker = root / "producer-pid"
            producer = root / "producer"
            producer.write_text('#!/bin/sh\nsleep 30 &\necho "$$ $!" > "' + str(marker) +
                                '"\nwait\n')
            producer.chmod(0o700)
            process = subprocess.Popen([sys.executable, str(ROOT / "scripts/refresh-mailcrawl.py"),
                                        "--source", "daily", "--producer", str(producer),
                                        "--config", str(config), "--home", str(root / "archive")],
                                       env=dict(os.environ, PYTHONPATH=str(ROOT)),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            group = None
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                owned_pids = [int(value) for value in marker.read_text().split()]
                group = owned_pids[0]
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
                def running(pid):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return False
                    if sys.platform.startswith("linux"):
                        try:
                            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                        except FileNotFoundError:
                            return False
                        return state != "Z"
                    return True

                deadline = time.monotonic() + 3
                while any(running(pid) for pid in owned_pids) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(any(running(pid) for pid in owned_pids))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if group is not None:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
