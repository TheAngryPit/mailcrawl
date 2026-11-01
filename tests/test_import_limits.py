# SPDX-License-Identifier: MIT
"""The operator can scale the per-import budgets for a large export."""
import contextlib
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from mailcrawl import inputio
from mailcrawl.cli import main

BUDGETS = ("MAX_IMPORT_BYTES", "MAX_IMPORT_IMAGE_BYTES", "MAX_IMPORT_EXTRACT_BYTES", "MAX_IMPORT_RECORDS")


def _run(argv: list[str]) -> tuple[int, dict[str, object]]:
    out = StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--json", *argv])
    return code, json.loads(out.getvalue())


class ScaledLimitsTest(unittest.TestCase):
    def test_the_default_size_matches_the_shipped_budgets(self):
        shipped = {name: getattr(inputio, name) for name in BUDGETS}
        with inputio.scaled_import_limits(512):
            self.assertEqual({name: getattr(inputio, name) for name in BUDGETS}, shipped)

    def test_budgets_grow_together_and_come_back(self):
        shipped = {name: getattr(inputio, name) for name in BUDGETS}
        with inputio.scaled_import_limits(2048):
            self.assertEqual(inputio.MAX_IMPORT_BYTES, 2048 << 20)
            self.assertEqual(inputio.MAX_IMPORT_IMAGE_BYTES, 1024 << 20)
            self.assertEqual(inputio.MAX_IMPORT_EXTRACT_BYTES, 256 << 20)
            self.assertEqual(inputio.MAX_IMPORT_RECORDS, 400_000)
            self.assertEqual(inputio.MAX_RECORD_BYTES, 1 << 20)
        self.assertEqual({name: getattr(inputio, name) for name in BUDGETS}, shipped)
        with inputio.scaled_import_limits(None):
            self.assertEqual({name: getattr(inputio, name) for name in BUDGETS}, shipped)

    def test_a_failed_import_still_restores_the_budgets(self):
        shipped = inputio.MAX_IMPORT_BYTES
        with self.assertRaises(RuntimeError):
            with inputio.scaled_import_limits(64):
                raise RuntimeError
        self.assertEqual(inputio.MAX_IMPORT_BYTES, shipped)

    def test_a_size_outside_the_range_is_refused(self):
        for bad in (0, -1, 65537):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                with inputio.scaled_import_limits(bad):
                    pass


class ImportFlagTest(unittest.TestCase):
    def test_the_flag_decides_whether_a_large_mbox_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            line = "x" * 70 + "\n"
            mbox = root / "big.mbox"
            mbox.write_text(
                "From 1 Mon Jan 01 00:00:00 2024\n"
                "Message-ID: <big@example.com>\n"
                "From: ada@example.com\nTo: bob@example.com\nSubject: big\n"
                "Date: Mon, 1 Jan 2024 00:00:00 +0000\n\n" + line * 20_000,
                encoding="utf-8",
            )
            self.assertGreater(mbox.stat().st_size, 1 << 20)
            config = str(root / "config.toml")
            for name, mib, code, expect in (
                ("default", None, 0, None),
                ("small", "1", 2, "byte limit exceeded"),
                ("zero", "0", 2, "max-import-mib"),
            ):
                with self.subTest(name):
                    home = str(root / name)
                    init = ["--home", home, "--config", config]
                    self.assertEqual(_run([*init, "init", "--account", "personal", "--email", "ada@example.com"])[0], 0)
                    flag = [] if mib is None else ["--max-import-mib", mib]
                    got, doc = _run([*init, "import", "mbox", str(mbox), "--alias", "personal", *flag])
                    self.assertEqual(got, code)
                    if expect is None:
                        self.assertEqual(doc["n_new"], 1)
                    else:
                        self.assertIn(expect, str(doc["detail"]))


if __name__ == "__main__":
    unittest.main()
