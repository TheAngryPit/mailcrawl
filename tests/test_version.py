# SPDX-License-Identifier: MIT
"""Package and runtime versions share one release identity."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailcrawl import version


class VersionTest(unittest.TestCase):
    def test_installed_package_does_not_probe_surrounding_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installed_path = Path(temporary) / "site-packages" / "mailcrawl" / "version.py"
            with mock.patch.object(version, "__file__", str(installed_path)):
                with mock.patch.object(version.subprocess, "check_output") as git:
                    self.assertEqual(version.mailcrawl_version(), version.VERSION)
                    git.assert_not_called()
