"""Scaffold fixtures stay synthetic and are not a gog field guess."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)")
V0_KEYS = {"schema", "synthetic", "note", "example_messages"}
MESSAGE_KEYS = {
    "example_ref",
    "example_from",
    "example_to",
    "example_subject",
    "example_body",
}


class ScaffoldFixtureTest(unittest.TestCase):
    def test_readme_title_line(self) -> None:
        first = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(
            first,
            "# mailcrawl 📬 — Your inboxes, on the record. Locally.",
        )

    def test_addresses_are_example_com(self) -> None:
        offenders: list[str] = []
        for path in (ROOT / "testdata").rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for match in EMAIL.finditer(text):
                if match.group(1).lower() != "example.com":
                    offenders.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(offenders, [])

    def test_v0_fixture_is_a_named_stub(self) -> None:
        document = json.loads(
            (ROOT / "testdata/fixtures/gog-export/v0/export.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(document), V0_KEYS)
        self.assertTrue(document["synthetic"])
        self.assertEqual(document["schema"], "mailcrawl.gog-export.v0")
        messages = document["example_messages"]
        self.assertGreaterEqual(len(messages), 1)
        for message in messages:
            self.assertEqual(set(message), MESSAGE_KEYS)

    def test_taxonomy_fixture_is_synthetic(self) -> None:
        document = json.loads(
            (ROOT / "testdata/catalogue/taxonomy.synthetic.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(document["schema"], "mailcrawl.taxonomy.synthetic")
        self.assertTrue(document["synthetic"])
        blob = json.dumps(document)
        self.assertNotIn("personal", blob)
        self.assertNotIn("pessoal", blob)

    def test_spec_leaves_schema_points_open(self) -> None:
        text = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
        for phrase in (
            "v1 DDL",
            "`personal` or `pessoal`",
            "provenance_hash",
            "mailcrawl.gog-export.v0",
            "quote and signature stripping",
            "from_hdr",
            "index.db",
            "taxonomy catalogue",
            "allow-list",
            "subject_norm",
        ):
            self.assertIn(phrase, text)
        self.assertGreaterEqual(text.count("**OPEN.**"), 10)
