"""Read one RFC822 message with the standard library email parser."""

from __future__ import annotations

import email
import hashlib
from email import policy
from email.message import EmailMessage, Message

from mailcrawl.contract import AttachmentRec, MessageRec
from mailcrawl.extract import parse_attachment
from mailcrawl.schema import HEADER_ALLOW
from mailcrawl.textnorm import html_to_text

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
    fallback: dict[str, str | None] | None = None,
) -> MessageRec:
    """Build a message record from RFC822 bytes. Header values beat ``fallback``."""
    parsed = email.message_from_bytes(raw, policy=policy.default)
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
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_path=raw_path,
        size_bytes=len(raw) if size_bytes is None else size_bytes,
        in_reply_to=headers.get("in-reply-to"),
        references_hdr=headers.get("references"),
        auth_results=headers.get("authentication-results"),
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
        return plain
    if html is not None:
        return html_to_text(html)
    return None


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


def header_text(message: EmailMessage, name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _attachment(part: Message) -> AttachmentRec:
    filename = part.get_filename()
    mime_type = part.get_content_type()
    payload = part.get_payload(decode=True)
    payload_bytes = payload if isinstance(payload, bytes) else None
    size = len(payload_bytes) if payload_bytes is not None else None
    content_id = _content_id(part.get("content-id"))
    inline = 1 if part.get_content_disposition() == "inline" else 0
    parsed = parse_attachment(
        mime_type,
        payload_bytes,
        decoded_text=_text_content(part),
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


def _content_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().strip("<>").strip()
    return text or None
