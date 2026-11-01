# SPDX-License-Identifier: MIT
"""The protected archive name is refused however the path reaches it."""
import tempfile
import unittest
from pathlib import Path

from mailcrawl.bench import run_bench
from mailcrawl.paths import UsageError, data_dir
from mailcrawl.store import connect, init_account


class ProtectedPathTest(unittest.TestCase):
    def test_a_symlink_to_the_protected_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "db-v2").mkdir()
            (root / "alias").symlink_to(root / "db-v2")
            (root / "plain").mkdir()
            self.assertEqual(data_dir(str(root / "plain")), root / "plain")
            for path in (root / "db-v2", root / "alias"):
                with self.subTest(path=path.name), self.assertRaisesRegex(UsageError, "db-v2"):
                    data_dir(str(path))
            with self.assertRaisesRegex(UsageError, "db-v2"):
                run_bench(root / "alias" / "personal.db", ["fake"], 1)

    def test_a_database_file_symlinked_into_the_protected_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "db-v2").mkdir()
            protected = root / "db-v2" / "personal.db"
            protected.write_bytes(b"")
            (root / "plain").mkdir()
            (root / "plain" / "personal.db").symlink_to(protected)
            with self.assertRaisesRegex(UsageError, "db-v2"):
                init_account(root / "plain", "personal", "ada@example.com")
            with self.assertRaisesRegex(UsageError, "db-v2"):
                connect(root / "plain" / "personal.db")
            self.assertEqual(protected.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
