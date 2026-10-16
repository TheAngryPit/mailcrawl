"""Keyword search, image_only, and pdftotext stay off the model path."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mailcrawl.extract import PdftotextBackend, parse_attachment
from mailcrawl.ingest import import_mbox
from mailcrawl.search import search_messages
from mailcrawl.store import connect, init_account

ROOT = Path(__file__).resolve().parents[1]
MBOX = ROOT / "testdata" / "fixtures" / "phase2" / "keyword.mbox"
PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Count 1 /Kids [3 0 R] >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 68 >> stream
BT /F1 12 Tf 36 100 Td (fatura FT99881) Tj ET
endstream
endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000244 00000 n 
0000000362 00000 n 
trailer << /Size 6 /Root 1 0 R >>
startxref
441
%%EOF
"""


class KeywordSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.conn = connect(init_account(home, "vcl", "ada@example.com"))
        result = import_mbox(
            self.conn,
            alias="vcl",
            mbox_path=MBOX,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(result.n_new, 3)
        self.assertEqual(result.n_error, 0)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_image_only_on_short_threads_with_a_pdf_or_a_large_image(self) -> None:
        rows = {
            row["gmail_message_id"]: row
            for row in self.conn.execute(
                """
                SELECT messages.gmail_message_id, threads.image_only, messages.subject
                FROM messages JOIN threads ON threads.thread_pk = messages.thread_pk
                """
            )
        }
        invoice = _one(rows, "Reunião da fatura")
        prose = _one(rows, "Texto longo")
        picture = _one(rows, "Só imagem")
        self.assertEqual(invoice["image_only"], 1)
        self.assertEqual(prose["image_only"], 0)
        self.assertEqual(picture["image_only"], 1)

    def test_pdftotext_extracts_the_invoice_token(self) -> None:
        self.assertIsNotNone(shutil.which("pdftotext"))
        parsed = parse_attachment("application/pdf", PDF, backend=PdftotextBackend())
        self.assertEqual(parsed.extract_status, "ok")
        self.assertIn("fatura FT99881", parsed.extracted_text or "")
        self.assertEqual(parsed.ocr_status, "pending")
        row = self.conn.execute(
            """
            SELECT extracted_text, extract_status, extract_method
            FROM attachments
            WHERE mime_type = 'application/pdf'
            """
        ).fetchone()
        self.assertEqual(row["extract_status"], "ok")
        self.assertEqual(row["extract_method"], "pdf")
        self.assertIn("FT99881", row["extracted_text"])

    def test_fts_folds_diacritics_and_trigram_finds_the_substring(self) -> None:
        folded = search_messages(self.conn, "reuniao")
        accented = search_messages(self.conn, "reunião")
        self.assertEqual(_ids(folded), _ids(accented))
        self.assertTrue(_ids(folded))
        self.assertTrue(all(hit["source"] == "fts" for hit in folded))
        fragment = search_messages(self.conn, "99881")
        self.assertEqual(len(fragment), 1)
        self.assertEqual(fragment[0]["source"], "trigram")
        self.assertEqual(fragment[0]["gmail_message_id"], _ids(folded)[0])

    def test_sql_filters_run_before_fts(self) -> None:
        invoice = search_messages(self.conn, "reuniao", image_only=1, mime="application/pdf")
        self.assertEqual(len(invoice), 1)
        hidden = search_messages(self.conn, "linha", image_only=1)
        self.assertEqual(hidden, [])
        picture = search_messages(self.conn, "imagem", sender="cam@example.com", image_only=1)
        self.assertEqual(len(picture), 1)
        self.assertEqual(picture[0]["source"], "fts")


def _ids(hits: list[dict[str, str | None]]) -> list[str | None]:
    return [hit["gmail_message_id"] for hit in hits]


def _one(rows: dict[str, sqlite3.Row], subject: str) -> sqlite3.Row:
    found = [row for row in rows.values() if row["subject"] == subject]
    if len(found) != 1:
        raise AssertionError(subject)
    return found[0]


if __name__ == "__main__":
    unittest.main()
