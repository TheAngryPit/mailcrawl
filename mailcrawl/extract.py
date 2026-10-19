# SPDX-License-Identifier: MIT
"""Attachment text extraction. External tools stay optional."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import selectors
import tempfile
import time
import os
import resource
import sys
from mailcrawl import inputio
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


class PdfExtractError(Exception):
    pass


class PdftotextBackend:
    def available(self, kind: str) -> bool:
        return kind == "pdf" and shutil.which("pdftotext") is not None

    def read(self, kind: str, payload: bytes) -> BackendRead:
        if kind != "pdf":
            raise PdfExtractError(kind)
        if len(payload) > inputio.MAX_DECODED_BYTES:
            raise inputio.InputLimit("PDF input byte limit exceeded")
        inputio.charge_extraction(len(payload))
        output = bytearray()
        stderr_size = 0
        deadline = time.monotonic() + 30
        with tempfile.TemporaryFile() as source:
            source.write(payload)
            source.seek(0)
            with subprocess.Popen(
                ["pdftotext", "-q", "-", "-"], stdin=source,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=_child_limits,
            ) as proc:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
                        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise PdfExtractError("pdftotext timed out")
                            for key, _event in selector.select(min(remaining, 0.25)):
                                chunk = os.read(key.fileobj.fileno(), 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                elif key.data == "stdout":
                                    if len(output) + len(chunk) > inputio.MAX_EXTRACT_BYTES:
                                        raise inputio.InputLimit("PDF output byte limit exceeded")
                                    inputio.charge_extraction(len(chunk))
                                    output.extend(chunk)
                                else:
                                    stderr_size += len(chunk)
                                    if stderr_size > inputio.MAX_EXTRACT_STDERR_BYTES:
                                        raise inputio.InputLimit("PDF stderr byte limit exceeded")
                    if proc.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                        raise PdfExtractError("pdftotext failed")
                except BaseException:
                    proc.kill()
                    proc.wait()
                    raise
        text = output.decode("utf-8", errors="replace").replace("\f", "\n")
        pages = max(output.count(b"\f"), 1 if text.strip() else 0)
        return BackendRead(text, pages, None)


def _child_limits() -> None:
    hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
    cpu = 10 if hard == resource.RLIM_INFINITY else min(10, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    if sys.platform != "darwin" and hasattr(resource, "RLIMIT_AS"):
        hard = resource.getrlimit(resource.RLIMIT_AS)[1]
        memory = 512 << 20 if hard == resource.RLIM_INFINITY else min(512 << 20, hard)
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


def pdf_backend() -> PdftotextBackend:
    return PdftotextBackend()


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
    if len(payload) > inputio.MAX_DECODED_BYTES:
        raise inputio.InputLimit("attachment byte limit exceeded")
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
    try:
        read = tool.read(kind, payload)
    except (PdfExtractError, OSError, subprocess.SubprocessError):
        return _result("error", kind, None, digest, "not_needed", None, None)
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
    if len(read.text) > inputio.MAX_TEXT_CHARS:
        raise inputio.InputLimit("extracted text limit exceeded")
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
    inputio.charge_extraction(len(decoded_text))
    if len(decoded_text) > inputio.MAX_TEXT_CHARS:
        raise inputio.InputLimit("text attachment limit exceeded")
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
