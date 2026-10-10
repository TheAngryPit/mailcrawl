"""Fail-closed behavior of the unverified export contract."""

import json
import unittest
from pathlib import Path

from mailcrawl.contract import CONTRACT_V0, UNSUPPORTED_SCHEMA, classify_export

ROOT = Path(__file__).resolve().parents[1]
V0_PATH = ROOT / "testdata" / "fixtures" / "gog-export" / "v0" / "export.json"
UNKNOWN_PATH = ROOT / "testdata" / "fixtures" / "gog-export" / "unknown" / "export.json"


class ClassifyExportTest(unittest.TestCase):
    def test_v0_fixture_is_accepted(self) -> None:
        document = json.loads(V0_PATH.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], CONTRACT_V0)
        self.assertEqual(classify_export(document), "ok")

    def test_missing_required_key_fails_closed(self) -> None:
        document = json.loads(V0_PATH.read_text(encoding="utf-8"))
        for key in (
            "example_message_id",
            "example_thread_id",
            "example_label_ids",
            "example_date",
        ):
            broken = json.loads(json.dumps(document))
            del broken["example_messages"][0][key]
            self.assertEqual(classify_export(broken), UNSUPPORTED_SCHEMA)

    def test_unknown_fixture_fails_closed(self) -> None:
        document = json.loads(UNKNOWN_PATH.read_text(encoding="utf-8"))
        self.assertNotEqual(document["schema"], CONTRACT_V0)
        self.assertEqual(classify_export(document), UNSUPPORTED_SCHEMA)

    def test_empty_document_fails_closed(self) -> None:
        self.assertEqual(classify_export({}), UNSUPPORTED_SCHEMA)

    def test_non_document_fails_closed(self) -> None:
        self.assertEqual(classify_export(None), UNSUPPORTED_SCHEMA)
