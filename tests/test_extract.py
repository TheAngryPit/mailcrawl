"""Attachment parsing stays behind an interface and does not require binaries."""

import hashlib
import os
import unittest

from mailcrawl.extract import (
    OCR_LOW_CONFIDENCE_V0,
    OCR_MIN_CHARS_PER_PAGE_V0,
    BackendRead,
    decide_ocr,
    parse_attachment,
)


class _PresentBackend:
    def __init__(self, text: str, page_count: int, ocr_conf: float | None) -> None:
        self._text = text
        self._page_count = page_count
        self._ocr_conf = ocr_conf

    def available(self, kind: str) -> bool:
        return True

    def read(self, kind: str, payload: bytes) -> BackendRead:
        return BackendRead(self._text, self._page_count, self._ocr_conf)


class ParseAttachmentTest(unittest.TestCase):
    def test_text_plain_is_extracted_and_hashed(self) -> None:
        payload = b"hello synthetic"
        parsed = parse_attachment("text/plain", payload)
        self.assertEqual(parsed.extract_status, "ok")
        self.assertEqual(parsed.extract_method, "text/plain")
        self.assertEqual(parsed.extracted_text, "hello synthetic")
        self.assertEqual(parsed.file_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(parsed.ocr_status, "not_needed")

    def test_text_html_uses_html2text(self) -> None:
        parsed = parse_attachment("text/html", b"<p>Linha html</p>")
        self.assertEqual(parsed.extract_status, "ok")
        self.assertEqual(parsed.extract_method, "html2text")
        self.assertEqual(parsed.extracted_text, "Linha html")

    def test_missing_bytes_are_skipped(self) -> None:
        parsed = parse_attachment("application/pdf", None)
        self.assertEqual(parsed.extract_status, "skipped")
        self.assertEqual(parsed.extract_method, "no_bytes")
        self.assertIsNone(parsed.file_sha256)
        self.assertIsNone(parsed.extracted_text)
        self.assertEqual(parsed.ocr_status, "not_needed")

    def test_unknown_type_is_unsupported(self) -> None:
        parsed = parse_attachment("application/octet-stream", b"abc")
        self.assertEqual(parsed.extract_status, "unsupported")
        self.assertIsNone(parsed.extracted_text)
        self.assertEqual(parsed.file_sha256, hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(parsed.ocr_status, "not_needed")

    def test_known_slots_without_a_backend_are_skipped(self) -> None:
        for mime, method in (
            ("application/pdf", "pdf"),
            ("image/png", "ocr"),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "office",
            ),
            ("text/calendar", "ics"),
        ):
            parsed = parse_attachment(mime, b"synthetic-bytes")
            self.assertEqual(parsed.extract_status, "skipped", mime)
            self.assertEqual(parsed.extract_method, method, mime)
            self.assertEqual(parsed.ocr_status, "not_needed", mime)
            self.assertIsNotNone(parsed.file_sha256, mime)

    def test_ocr_thresholds_run_only_when_a_backend_is_present(self) -> None:
        self.assertEqual(OCR_MIN_CHARS_PER_PAGE_V0, 25)
        self.assertEqual(OCR_LOW_CONFIDENCE_V0, 60)
        self.assertEqual(
            decide_ocr("x" * 10, 1, 10.0, backend_present=False),
            "not_needed",
        )
        low = parse_attachment(
            "application/pdf",
            b"%PDF",
            backend=_PresentBackend("x" * 24, 1, 59.0),
        )
        self.assertEqual(low.ocr_status, "low_confidence")
        self.assertEqual(low.extract_status, "ok")
        at_chars = parse_attachment(
            "application/pdf",
            b"%PDF",
            backend=_PresentBackend("x" * 25, 1, 59.0),
        )
        self.assertEqual(at_chars.ocr_status, "not_needed")
        at_conf = parse_attachment(
            "application/pdf",
            b"%PDF",
            backend=_PresentBackend("x" * 24, 1, 60.0),
        )
        self.assertEqual(at_conf.ocr_status, "ok")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
