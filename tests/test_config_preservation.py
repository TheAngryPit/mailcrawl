# SPDX-License-Identifier: MIT
"""Account initialization preserves the rest of a local TOML document."""

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from mailcrawl.configfile import inference_config, load_config, taxonomy_path, takeout_label_map, write_account
from mailcrawl.paths import UsageError


class ConfigPreservationTest(unittest.TestCase):
    def test_account_write_preserves_all_other_toml_values(self) -> None:
        source = '''taxonomy = "taxonomy.synthetic.json"
as_of = 1780272000
unknown = [{when = 2026-01-01T12:34:56Z, day = 2026-01-01, clock = 12:34:56, ratio = 1.25}]
"quoted.key" = "line\\nwith\\tcontrols and \\\"quotes\\\""
[time]
stale_days = 10
old_days = 20
future_setting = true
[takeout.label_map]
"Custom/Label" = "INBOX"
[takeout.personal.label_map]
"Other" = "STARRED"
[inference]
embeddings = false
search-embedding = false
[inference.llm]
base_url = "http://localhost:1234/v1"
model = "synthetic"
allow_remote = false
[review]
allow_override = false
llm_review = false
[rules]
path = "rules.synthetic.json"
future_option = [1, "two", false]
[accounts.personal]
email = "old@example.com"
owners = ["ada@example.com"]
custom = { nested = { value = "keep" } }
[accounts.thehive]
email = "team@example.com"
[unknown_section."strange.table"]
empty = []
[[custom_rows]]
name = "first"
[custom_rows.nested]
keep = true
[[custom_rows]]
name = "second"
[empty_section]
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "config.toml"
            path.parent.mkdir()
            path.write_text(source)
            expected = tomllib.loads(source)
            with path.open("a") as handle:
                handle.write('\n[unicode]\nvalue = "synthetic 😀 \\u007f"\n')
            expected["unicode"] = {"value": "synthetic 😀 \x7f"}
            expected["accounts"]["personal"]["email"] = "ada@example.com"
            for _ in range(2):
                write_account(path, "pessoal", "ada@example.com")
                self.assertEqual(load_config(path), expected)
                self.assertEqual(taxonomy_path(path), Path("taxonomy.synthetic.json"))
                self.assertEqual(takeout_label_map(path, "personal"), {"Custom/Label": "INBOX", "Other": "STARRED"})
                self.assertEqual(inference_config(path), {"embeddings": False, "search-embedding": False})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_legacy_rules_path_normalizes_without_losing_other_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text('rules = "rules.synthetic.json"\n[future]\nkeep = true\n')
            write_account(path, "personal", "ada@example.com")
            self.assertEqual(load_config(path), {
                "rules": {"path": "rules.synthetic.json"},
                "future": {"keep": True},
                "accounts": {"personal": {"email": "ada@example.com"}},
            })

    def test_new_account_keeps_inference_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_account(path, "personal", "ada@example.com")
            self.assertEqual(inference_config(path), {})
            self.assertEqual(load_config(path), {"accounts": {"personal": {"email": "ada@example.com"}}})

    def test_failed_replace_keeps_original_and_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            original = b'[accounts.personal]\nemail = "old@example.com"\n'
            path.write_bytes(original)
            replace = os.replace

            def fail_private_replace(source, destination):
                self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.read_bytes(), original)
                raise OSError("synthetic replacement failure")

            with mock.patch("mailcrawl.configfile.os.replace", side_effect=fail_private_replace):
                with self.assertRaises(OSError):
                    write_account(path, "personal", "ada@example.com")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertIs(os.replace, replace)

    def test_failed_sync_keeps_original_and_cleans_private_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            original = b'[accounts.personal]\nemail = "old@example.com"\n'
            path.write_bytes(original)
            with mock.patch("mailcrawl.configfile.os.fsync", side_effect=OSError("synthetic sync failure")):
                with self.assertRaises(OSError):
                    write_account(path, "personal", "ada@example.com")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_account_structure_is_not_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            for source in ('accounts = "keep"\n', '[accounts]\npersonal = "keep"\n'):
                path.write_text(source)
                with self.assertRaises(UsageError):
                    write_account(path, "personal", "ada@example.com")
                self.assertEqual(path.read_text(), source)
