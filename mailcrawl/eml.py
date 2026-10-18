# SPDX-License-Identifier: MIT
"""Read one RFC822 message with the standard library email parser."""

from __future__ import annotations

import binascii
import email
import hashlib
from email import policy
from email.message import EmailMessage, Message

from mailcrawl.contract import AttachmentRec, MessageRec
from mailcrawl.extract import parse_attachment, pdf_backend
from mailcrawl.schema import HEADER_ALLOW
from mailcrawl.textnorm import html_to_text
from mailcrawl.threads import embeddable_image

_HEADER_ALLOW = frozenset(HEADER_ALLOW)


def eml_record(
    raw: bytes,
    *,
    gmail_message_id: str,
    gmail_thread_id: str,
    label_ids: tuple[str, ...],
    internal_date: int,
    raw_path: str,
    size_bytes: int | None = None,
    gmail_history_id: str | None = None,
    raw_json: str | None = None,
    date_trusted: int = 1,
    fallback: dict[str, str | None] | None = None,
) -> MessageRec:
    """Build a message record from RFC822 bytes. Header values beat ``fallback``."""
    return parsed_record(
        email.message_from_bytes(raw, policy=policy.default),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw) if size_bytes is None else size_bytes,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        label_ids=label_ids,
        internal_date=internal_date,
        raw_path=raw_path,
        gmail_history_id=gmail_history_id,
        raw_json=raw_json,
        date_trusted=date_trusted,
        fallback=fallback,
    )


def parsed_record(
    parsed: EmailMessage,
    *,
    raw_sha256: str,
    size_bytes: int,
    gmail_message_id: str,
    gmail_thread_id: str,
    label_ids: tuple[str, ...],
    internal_date: int,
    raw_path: str,
    gmail_history_id: str | None = None,
    raw_json: str | None = None,
    date_trusted: int = 1,
    fallback: dict[str, str | None] | None = None,
) -> MessageRec:
    """Build a message record from a parsed message whose raw bytes were hashed while streaming."""
    body, headers, parts = view_message(parsed)
    spare = fallback or {}
    return MessageRec(
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        label_ids=label_ids,
        internal_date=internal_date,
        rfc822_message_id=headers.get("message-id"),
        from_hdr=header_text(parsed, "from") or spare.get("from"),
        to_hdr=header_text(parsed, "to") or spare.get("to"),
        cc_hdr=header_text(parsed, "cc") or spare.get("cc"),
        bcc_hdr=header_text(parsed, "bcc"),
        reply_to_hdr=headers.get("reply-to"),
        subject=header_text(parsed, "subject") or spare.get("subject"),
        snippet=None,
        gmail_history_id=gmail_history_id,
        body_text=body,
        headers=tuple(sorted(headers.items())),
        attachments=parts,
        raw_json=raw_json,
        raw_sha256=raw_sha256,
        raw_path=raw_path,
        size_bytes=size_bytes,
        in_reply_to=headers.get("in-reply-to"),
        references_hdr=headers.get("references"),
        auth_results=header_block(parsed, "authentication-results"),
        date_trusted=date_trusted,
    )


def view_message(message: EmailMessage) -> tuple[str | None, dict[str, str], tuple[AttachmentRec, ...]]:
    return body_text(message), allow_headers(message), attachments(message)


def body_text(message: EmailMessage) -> str | None:
    plain: str | None = None
    html: str | None = None
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart" or is_attachment(part):
                continue
            kind = part.get_content_type()
            text = _text_content(part)
            if text is None:
                continue
            if kind == "text/plain" and plain is None:
                plain = text
            elif kind == "text/html" and html is None:
                html = text
    elif not is_attachment(message):
        kind = message.get_content_type()
        text = _text_content(message)
        if isinstance(text, str) and kind == "text/plain":
            plain = text
        elif isinstance(text, str) and kind == "text/html":
            html = text
    if plain is not None:
        return _lf(plain)
    if html is not None:
        return _lf(html_to_text(html))
    return None


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def allow_headers(message: EmailMessage) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name in _HEADER_ALLOW:
        text = header_text(message, name)
        if text:
            headers[name] = text
    return headers


def attachments(message: EmailMessage) -> tuple[AttachmentRec, ...]:
    if not message.is_multipart():
        if is_attachment(message):
            return (_attachment(message),)
        return ()
    records: list[AttachmentRec] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or not is_attachment(part):
            continue
        records.append(_attachment(part))
    return tuple(records)


def is_attachment(part: Message) -> bool:
    disposition = part.get_content_disposition()
    filename = part.get_filename()
    if disposition == "attachment":
        return True
    if disposition == "inline" and filename:
        return True
    return disposition is None and bool(filename)


def header_block(message: EmailMessage, name: str) -> str | None:
    """Return the raw header text.

    ``get_all`` decodes RFC 2047 encoded words. Authentication-Results is not
    an unstructured header, and that decoding can close a comment early.
    """
    wanted = name.lower()
    parts: list[str] = []
    for key, value in message.raw_items():
        if key.lower() != wanted:
            continue
        text = _raw_header(value)
        if text:
            parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


def _raw_header(value: str) -> str:
    # raw_items keeps a lone 8-bit byte as a surrogate. SQLite rejects that.
    text = value.encode("ascii", "surrogateescape").decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def header_text(message: EmailMessage, name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _payload(part: Message) -> bytes | None:
    """Decode base64 straight from the stored text.

    ``get_payload(decode=True)`` re-encodes the text, splits it into lines, and
    joins it again before decoding, which peaks near six times the attachment.
    Anything the fast path rejects goes through the standard library unchanged.
    """
    if str(part.get("content-transfer-encoding", "")).lower() == "base64":
        text = part.get_payload()
        if isinstance(text, str):
            try:
                return binascii.a2b_base64(text)
            except (binascii.Error, ValueError):
                pass
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else None


def _attachment(part: Message) -> AttachmentRec:
    filename = part.get_filename()
    mime_type = part.get_content_type()
    payload_bytes = _payload(part)
    size = len(payload_bytes) if payload_bytes is not None else None
    content_id = _content_id(part.get("content-id"))
    inline = 1 if part.get_content_disposition() == "inline" else 0
    parsed = parse_attachment(
        mime_type,
        payload_bytes,
        decoded_text=_decoded_text(part, payload_bytes),
        backend=pdf_backend(),
    )
    return AttachmentRec(
        filename,
        mime_type,
        size,
        None,
        parsed.extracted_text,
        parsed.extract_status,
        content_id,
        inline,
        parsed.extract_method,
        parsed.file_sha256,
        parsed.ocr_status,
        parsed.ocr_conf,
        parsed.page_count,
        parsed.parser_version,
        payload_bytes if embeddable_image(mime_type, inline, size) else None,
    )


def _text_content(part: Message) -> str | None:
    if part.get_content_maintype() != "text":
        return None
    try:
        content = part.get_content()
    except (KeyError, LookupError, ValueError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    return content


def _decoded_text(part: Message, payload: bytes | None) -> str | None:
    """``get_content()`` for a text part, without decoding the transfer encoding a second time."""
    if part.get_content_maintype() != "text" or payload is None:
        return None
    try:
        return payload.decode(str(part.get_param("charset", "ASCII")), errors="replace")
    except (LookupError, TypeError):
        return None


def _content_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().strip("<>").strip()
    return text or None
