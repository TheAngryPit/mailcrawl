"""Default taxonomy, local override, and category counts."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from mailcrawl.cli import main
from mailcrawl.store import connect

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "testdata" / "fixtures" / "gog-export" / "v0"


def _export(folder: Path, labels: list[dict[str, str]], messages: list[dict[str, object]]) -> None:
    folder.mkdir()
    document = {
        "schema": "mailcrawl.gog-export.v0",
        "synthetic": True,
        "example_manifest": {"messages": len(messages)},
        "example_labels": labels,
        "example_messages": messages,
    }
    (folder / "export.json").write_text(json.dumps(document), encoding="utf-8")


def _message(
    number: int,
    label_ids: list[str],
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "example_message_id": f"tax-{number}",
        "example_thread_id": f"tax-thread-{number}",
        "example_label_ids": label_ids,
        "example_date": 1700000000,
        "example_from": f"Ada Fixture <ada{number}@example.com>",
        "example_body": "synthetic taxonomy body",
    }
    if headers:
        item["example_headers"] = headers
    return item


class DefaultTaxonomyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old_home = os.environ.get("HOME")
        self.old_taxonomy = os.environ.get("MAILCRAWL_TAXONOMY")
        os.environ["HOME"] = str(self.home)
        os.environ.pop("MAILCRAWL_TAXONOMY", None)
        self.stdout: list[str] = []

    def tearDown(self) -> None:
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        if self.old_taxonomy is None:
            os.environ.pop("MAILCRAWL_TAXONOMY", None)
        else:
            os.environ["MAILCRAWL_TAXONOMY"] = self.old_taxonomy
        self.tmp.cleanup()

    def test_default_signals_and_empty_categories(self) -> None:
        export = self.home / "export"
        _export(
            export,
            [
                {"example_label_id": "CATEGORY_PROMOTIONS", "example_name": "CATEGORY_PROMOTIONS", "example_kind": "system"},
                {"example_label_id": "CATEGORY_UPDATES", "example_name": "CATEGORY_UPDATES", "example_kind": "system"},
                {"example_label_id": "CATEGORY_PERSONAL", "example_name": "CATEGORY_PERSONAL", "example_kind": "system"},
                {"example_label_id": "pseudo:archived", "example_name": "pseudo:archived", "example_kind": "system"},
            ],
            [
                _message(1, ["CATEGORY_PROMOTIONS"], {"list-unsubscribe": "<https://example.com/unsub>"}),
                _message(2, ["CATEGORY_UPDATES"], {"list-id": "<news.example.com>"}),
                _message(3, ["CATEGORY_PERSONAL"], {"precedence": "bulk"}),
                _message(4, ["pseudo:archived"], {"list-unsubscribe": "<https://example.com/pseudo>"}),
                _message(5, ["INBOX"], {"precedence": "list"}),
            ],
        )
        self._init("personal")
        imported = self._run("import", "gog-export", "--account", "personal", "--dir", str(export))
        self.assertEqual(imported["taxonomy_warnings"], 0)
        self.assertEqual(imported["missing_labels"], [])
        keys = self._keys("personal")
        self.assertEqual(keys["CATEGORY_PROMOTIONS"], "CATEGORY_PROMOTIONS")
        self.assertEqual(keys["CATEGORY_UPDATES"], "CATEGORY_UPDATES")
        self.assertEqual(keys["CATEGORY_PERSONAL"], "CATEGORY_PERSONAL")
        self.assertEqual(keys["INBOX"], "INBOX")
        self.assertNotIn("pseudo:archived", keys)
        counted = self._run("stats", "--account", "personal")
        preview = self._run("dry-run", "--account", "personal")
        self.assertEqual(counted["by_category"]["CATEGORY_PROMOTIONS"], 1)
        self.assertEqual(counted["by_category"]["CATEGORY_UPDATES"], 1)
        self.assertEqual(counted["by_category"]["CATEGORY_PERSONAL"], 1)
        self.assertEqual(counted["by_category"]["INBOX"], 1)
        self.assertNotIn("newsletter", counted["by_category"])
        self.assertNotIn("automated", counted["by_category"])
        self.assertNotIn("invoice_receipt", counted["by_category"])
        self.assertEqual(preview["by_category"], counted["by_category"])
        self.assertEqual(preview["inactive_rules"], ["2", "2-read", "3", "4"])
        self.assertEqual(preview["inactive_reason"], "gmail_default")
        self.assertNotIn("inactive_rules", counted)

    def test_override_per_account_and_default_and_pessoal(self) -> None:
        export = self.home / "export"
        _export(
            export,
            [
                {"example_label_id": "L1", "example_name": "Example/Shared", "example_kind": "user"},
                {"example_label_id": "L2", "example_name": "Example/HiveOnly", "example_kind": "user"},
                {"example_label_id": "L3", "example_name": "Example/PersonalOnly", "example_kind": "user"},
            ],
            [
                _message(1, ["L1", "L2", "L3"]),
            ],
        )
        override = self.home / "override.json"
        override.write_text(
            json.dumps(
                {
                    "default": {"newsletter": ["Example/Shared"]},
                    "thehive": {"invoice_receipt": ["Example/HiveOnly"]},
                    "pessoal": {"contract": ["Example/PersonalOnly"]},
                }
            ),
            encoding="utf-8",
        )
        self._init("thehive")
        self._init("pessoal")
        hive = self._run(
            "import",
            "gog-export",
            "--account",
            "thehive",
            "--dir",
            str(export),
            "--taxonomy",
            str(override),
        )
        personal = self._run(
            "import",
            "gog-export",
            "--account",
            "pessoal",
            "--dir",
            str(export),
            "--taxonomy",
            str(override),
        )
        self.assertEqual(hive["taxonomy_warnings"], 0)
        self.assertEqual(personal["taxonomy_warnings"], 0)
        self.assertEqual(self._keys("thehive")["L1"], "newsletter")
        self.assertEqual(self._keys("thehive")["L2"], "invoice_receipt")
        self.assertIsNone(self._keys("thehive")["L3"])
        self.assertEqual(self._keys("personal")["L1"], "newsletter")
        self.assertIsNone(self._keys("personal")["L2"])
        self.assertEqual(self._keys("personal")["L3"], "contract")
        counted = self._run("stats", "--account", "personal", "--taxonomy", str(override))
        preview = self._run("dry-run", "--account", "personal", "--taxonomy", str(override))
        self.assertEqual(counted["by_category"]["newsletter"], 1)
        self.assertEqual(counted["by_category"]["contract"], 1)
        self.assertEqual(counted["taxonomy_warnings"], 0)
        self.assertEqual(preview["inactive_rules"], ["2", "3", "4"])
        self.assertEqual(preview["inactive_reason"], "schema")

    def test_validation_errors_exit_2(self) -> None:
        export = self.home / "export"
        _export(export, [], [_message(1, ["INBOX"])])
        self._init("personal")
        cases = {
            "not-json": ("{", "taxonomy file is not valid JSON"),
            "not-object": ("[]", "taxonomy file must be a JSON object"),
            "unknown-account": ('{"work": {}}', "unknown taxonomy account work"),
            "not-a-list": (
                '{"default": {"newsletter": "Example/Shared"}}',
                "taxonomy category newsletter needs a list of label names",
            ),
            "not-strings": (
                '{"default": {"newsletter": [1]}}',
                "taxonomy category newsletter needs a list of label names",
            ),
        }
        for name, (body, detail) in cases.items():
            path = self.home / f"{name}.json"
            path.write_text(body, encoding="utf-8")
            code, doc = self._code(
                "import",
                "gog-export",
                "--account",
                "personal",
                "--dir",
                str(export),
                "--taxonomy",
                str(path),
            )
            self.assertEqual(code, 2, name)
            self.assertIn(detail, str(doc.get("detail")))
            self.assertFalse(doc.get("ok"))

    def test_custom_label_maps_onto_a_gmail_category(self) -> None:
        export = self.home / "export"
        _export(
            export,
            [{"example_label_id": "L1", "example_name": "Example/Bulletin", "example_kind": "user"}],
            [_message(1, ["L1"])],
        )
        override = self.home / "gmail-map.json"
        override.write_text(
            json.dumps({"default": {"CATEGORY_PROMOTIONS": ["Example/Bulletin"]}}),
            encoding="utf-8",
        )
        self._init("personal")
        imported = self._run(
            "import",
            "gog-export",
            "--account",
            "personal",
            "--dir",
            str(export),
            "--taxonomy",
            str(override),
        )
        self.assertEqual(imported["taxonomy_warnings"], 0)
        self.assertEqual(self._keys("personal")["L1"], "CATEGORY_PROMOTIONS")
        preview = self._run("dry-run", "--account", "personal", "--taxonomy", str(override))
        self.assertEqual(preview["inactive_reason"], "gmail_default")
        self.assertEqual(preview["inactive_rules"], ["2", "2-read", "3", "4"])

    def test_example_schema_turns_content_rules_on(self) -> None:
        self._init("vcl")
        preview = self._run(
            "dry-run",
            "--account",
            "vcl",
            "--taxonomy",
            str(ROOT / "testdata" / "catalogue" / "taxonomy.synthetic.json"),
        )
        self.assertEqual(preview["inactive_rules"], [])
        self.assertIsNone(preview["inactive_reason"])
        self.assertGreater(preview["taxonomy_warnings"], 0)

    def test_own_schema_category_is_accepted(self) -> None:
        export = self.home / "export"
        _export(export, [], [_message(1, ["INBOX"])])
        override = self.home / "own.json"
        override.write_text(
            json.dumps({"default": {"not_a_category": ["Example/Shared"]}}),
            encoding="utf-8",
        )
        self._init("personal")
        imported = self._run(
            "import",
            "gog-export",
            "--account",
            "personal",
            "--dir",
            str(export),
            "--taxonomy",
            str(override),
        )
        self.assertEqual(imported["ok"], True)
        self.assertEqual(imported["missing_labels"], ["Example/Shared"])

    def test_missing_label_is_a_warning(self) -> None:
        export = self.home / "export"
        _export(export, [], [_message(1, ["INBOX"])])
        override = self.home / "override.json"
        override.write_text(
            json.dumps({"default": {"invoice_receipt": ["Example type/Missing"]}}),
            encoding="utf-8",
        )
        self._init("personal")
        imported = self._run(
            "import",
            "gog-export",
            "--account",
            "personal",
            "--dir",
            str(export),
            "--taxonomy",
            str(override),
        )
        self.assertEqual(imported["ok"], True)
        self.assertEqual(imported["taxonomy_warnings"], 1)
        self.assertEqual(imported["missing_labels"], ["Example type/Missing"])
        counted = self._run("dry-run", "--account", "personal", "--taxonomy", str(override))
        self.assertEqual(counted["taxonomy_warnings"], 1)
        self.assertEqual(counted["missing_labels"], ["Example type/Missing"])

    def test_lookup_order_is_flag_env_config_then_home(self) -> None:
        export = self.home / "export"
        _export(
            export,
            [{"example_label_id": "L1", "example_name": "Example/Synthetic", "example_kind": "user"}],
            [_message(1, ["L1"])],
        )
        home_file = self.home / ".config" / "mailcrawl" / "taxonomy.json"
        home_file.parent.mkdir(parents=True)
        home_file.write_text(
            json.dumps({"default": {"correspondence": ["Example/Synthetic"]}}),
            encoding="utf-8",
        )
        config_file = self.home / "from-config.json"
        config_file.write_text(
            json.dumps({"default": {"contract": ["Example/Synthetic"]}}),
            encoding="utf-8",
        )
        env_file = self.home / "from-env.json"
        env_file.write_text(
            json.dumps({"default": {"account_security": ["Example/Synthetic"]}}),
            encoding="utf-8",
        )
        flag_file = self.home / "from-flag.json"
        flag_file.write_text(
            json.dumps({"default": {"job_alert": ["Example/Synthetic"]}}),
            encoding="utf-8",
        )
        config = self.home / "config.toml"
        self._init("personal", "--config", str(config))
        config.write_text(
            f'taxonomy = "{config_file}"\n\n[accounts.personal]\nemail = "ada@example.com"\n',
            encoding="utf-8",
        )
        self._run(
            "--config",
            str(config),
            "import",
            "gog-export",
            "--account",
            "personal",
            "--dir",
            str(export),
        )
        self.assertEqual(self._keys("personal")["L1"], "contract")
        os.environ["MAILCRAWL_TAXONOMY"] = str(env_file)
        self._run(
            "--config",
            str(config),
            "import",
            "gog-export",
            "--account",
            "personal",
            "--dir",
            str(export),
        )
        self.assertEqual(self._keys("personal")["L1"], "account_security")
        self._run(
            "--config",
            str(config),
            "import",
            "gog-export",
            "--account",
            "personal",
            "--dir",
            str(export),
            "--taxonomy",
            str(flag_file),
        )
        self.assertEqual(self._keys("personal")["L1"], "job_alert")
        os.environ.pop("MAILCRAWL_TAXONOMY", None)
        config.write_text('[accounts.personal]\nemail = "ada@example.com"\n', encoding="utf-8")
        self._run(
            "--config",
            str(config),
            "import",
            "gog-export",
            "--account",
            "personal",
            "--dir",
            str(export),
        )
        self.assertEqual(self._keys("personal")["L1"], "correspondence")

    def _init(self, alias: str, *extra: str) -> None:
        code = main(["--json", "--home", str(self.home), *extra, "init", "--account", alias, "--email", "ada@example.com"])
        self.assertEqual(code, 0)

    def _run(self, *args: str) -> dict[str, object]:
        code, doc = self._code(*args)
        self.assertEqual(code, 0, doc)
        return doc

    def _code(self, *args: str) -> tuple[int, dict[str, object]]:
        from io import StringIO
        from contextlib import redirect_stdout

        hold = StringIO()
        with redirect_stdout(hold):
            code = main(["--json", "--home", str(self.home), *args])
        return code, json.loads(hold.getvalue())

    def _keys(self, alias: str) -> dict[str, str | None]:
        conn = connect(self.home / f"{alias}.db")
        try:
            rows = conn.execute("SELECT gmail_label_id, catalogue_key FROM labels").fetchall()
        finally:
            conn.close()
        return {str(row[0]): row[1] for row in rows}


if __name__ == "__main__":
    unittest.main()
