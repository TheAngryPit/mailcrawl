# SPDX-License-Identifier: MIT
"""Archive and config files are private, and existing directories keep their mode."""
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from mailcrawl.configfile import write_account
from mailcrawl.store import connect, init_account


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class PrivateFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.umask = os.umask(0o022)

    def tearDown(self):
        os.umask(self.umask)
        self.tmp.cleanup()

    def test_a_new_archive_is_private_including_wal_and_shm(self):
        db = self.root / "new" / "personal.db"
        conn = connect(db)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS probe(x)")
            conn.execute("INSERT INTO probe VALUES (1)")
            conn.commit()
            names = sorted(path.name for path in db.parent.iterdir())
            self.assertEqual(names, ["personal.db", "personal.db-shm", "personal.db-wal"])
            for name in names:
                self.assertEqual(_mode(db.parent / name), 0o600, name)
            self.assertEqual(_mode(db.parent), 0o700)
        finally:
            conn.close()

    def test_loose_sidecars_from_an_older_archive_are_tightened(self):
        db = self.root / "old" / "personal.db"
        db.parent.mkdir(mode=0o700)
        legacy = sqlite3.connect(db)
        legacy.execute("PRAGMA journal_mode = WAL")
        legacy.execute("CREATE TABLE probe(x)")
        legacy.commit()
        os.chmod(db, 0o644)
        self.assertEqual(_mode(Path(f"{db}-wal")), 0o644)
        conn = connect(db)
        try:
            for suffix in ("", "-wal", "-shm"):
                self.assertEqual(_mode(Path(f"{db}{suffix}")), 0o600, suffix)
        finally:
            conn.close()
            legacy.close()

    def test_an_existing_directory_keeps_its_mode_but_its_files_are_private(self):
        shared = self.root / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        path = init_account(shared, "personal", "ada@example.com")
        self.assertEqual(_mode(shared), 0o755)
        self.assertEqual(_mode(path), 0o600)

    def test_config_parent_is_tightened_only_when_it_is_new(self):
        shared = self.root / "etc"
        shared.mkdir()
        os.chmod(shared, 0o755)
        write_account(shared / "config.toml", "personal", "ada@example.com")
        self.assertEqual(_mode(shared), 0o755)
        self.assertEqual(_mode(shared / "config.toml"), 0o600)
        write_account(self.root / "fresh" / "config.toml", "personal", "ada@example.com")
        self.assertEqual(_mode(self.root / "fresh"), 0o700)


if __name__ == "__main__":
    unittest.main()
