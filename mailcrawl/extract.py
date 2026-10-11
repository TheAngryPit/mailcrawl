"""Attachment text extraction. External tools stay optional."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from mailcrawl.textnorm import html_to_text

OCR_MIN_CHARS_PER_PAGE_V0 = 25
OCR_LOW_CONFIDENCE_V0 = 60
PARSER_VERSION = "parser-v0"

_OFFICE_TYPES = frozenset(
    {
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)
_OCR_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/tiff",
        "image/webp",
    }
)
_ICS_TYPES = frozenset({"text/calendar", "application/ics"})


@dataclass(frozen=True)
class BackendRead:
    text: str
    page_count: int
    ocr_conf: float | None


class AttachmentBackend(Protocol):
    def available(self, kind: str) -> bool:
        """True when this process can run the named slot."""

    def read(self, kind: str, payload: bytes) -> BackendRead:
        """Read one payload. Called only when available(kind) is true."""


@dataclass(frozen=True)
class ExtractResult:
    extract_status: str
    extract_method: str | None
    extracted_text: str | None
    file_sha256: str | None
    ocr_status: str
    ocr_conf: float | None
    page_count: int | None
    parser_version: str


class DefaultBackend:
    """No external binary is required, and none is invoked."""

    def available(self, kind: str) -> bool:
        return False

    def read(self, kind: str, payload: bytes) -> BackendRead:
        raise RuntimeError(f"no backend for {kind}")


def decide_ocr(
    text: str,
    page_count: int,
    ocr_conf: float | None,
    *,
    backend_present: bool,
) -> str:
    if not backend_present:
        return "not_needed"
    pages = page_count if page_count > 0 else 1
    if (len(text) / pages) >= OCR_MIN_CHARS_PER_PAGE_V0:
        return "not_needed"
    if ocr_conf is None:
        return "pending"
    if ocr_conf < OCR_LOW_CONFIDENCE_V0:
        return "low_confidence"
    return "ok"


def parse_attachment(
    mime_type: str | None,
    payload: bytes | None,
    *,
    decoded_text: str | None = None,
    backend: AttachmentBackend | None = None,
) -> ExtractResult:
    if payload is None:
        return _result("skipped", "no_bytes", None, None, "not_needed", None, None)
    digest = hashlib.sha256(payload).hexdigest()
    mime = _mime(mime_type)
    if mime in ("text/plain", "text/html"):
        return _text_result(mime, payload, decoded_text, digest)
    kind = _slot(mime)
    if kind is None:
        return _result("unsupported", None, None, digest, "not_needed", None, None)
    tool = backend if backend is not None else DefaultBackend()
    if not tool.available(kind):
        return _result("skipped", kind, None, digest, "not_needed", None, None)
    read = tool.read(kind, payload)
    ocr_status = "not_needed"
    ocr_conf = None
    if kind in ("pdf", "ocr"):
        ocr_status = decide_ocr(
            read.text,
            read.page_count,
            read.ocr_conf,
            backend_present=True,
        )
        ocr_conf = read.ocr_conf
    text = read.text.strip()
    status = "empty" if not text else "ok"
    return _result(
        status,
        kind,
        text or None,
        digest,
        ocr_status,
        ocr_conf,
        read.page_count,
    )


def _text_result(
    mime: str,
    payload: bytes,
    decoded_text: str | None,
    digest: str,
) -> ExtractResult:
    if decoded_text is None:
        decoded_text = payload.decode("utf-8", errors="replace")
    if mime == "text/html":
        text = html_to_text(decoded_text)
        method = "html2text"
    else:
        text = decoded_text.strip()
        method = "text/plain"
    status = "empty" if not text else "ok"
    return _result(status, method, text or None, digest, "not_needed", None, None)


def _mime(mime_type: str | None) -> str:
    if not mime_type:
        return ""
    return mime_type.split(";", 1)[0].strip().lower()


def _slot(mime: str) -> str | None:
    if mime == "application/pdf":
        return "pdf"
    if mime in _OCR_TYPES:
        return "ocr"
    if mime in _OFFICE_TYPES:
        return "office"
    if mime in _ICS_TYPES:
        return "ics"
    return None


def _result(
    status: str,
    method: str | None,
    text: str | None,
    digest: str | None,
    ocr_status: str,
    ocr_conf: float | None,
    page_count: int | None,
) -> ExtractResult:
    return ExtractResult(
        status,
        method,
        text,
        digest,
        ocr_status,
        ocr_conf,
        page_count,
        PARSER_VERSION,
    )
