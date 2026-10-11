"""Read one RFC822 message with the standard library email parser."""

from __future__ import annotations

from email.message import EmailMessage, Message

from mailcrawl.contract import AttachmentRec
from mailcrawl.schema import HEADER_ALLOW
from mailcrawl.textnorm import html_to_text

_HEADER_ALLOW = frozenset(HEADER_ALLOW)


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
    size = len(payload) if isinstance(payload, bytes) else None
    content_id = _content_id(part.get("content-id"))
    inline = 1 if part.get_content_disposition() == "inline" else 0
    text = _attachment_text(part)
    if text:
        return AttachmentRec(
            filename,
            mime_type,
            size,
            None,
            text,
            "ok",
            content_id,
            inline,
        )
    return AttachmentRec(
        filename,
        mime_type,
        size,
        None,
        None,
        "pending",
        content_id,
        inline,
    )


def _attachment_text(part: Message) -> str | None:
    if part.get_content_maintype() != "text":
        return None
    text = _text_content(part)
    if text is None or not text.strip():
        return None
    if part.get_content_type() == "text/html":
        converted = html_to_text(text)
        return converted or None
    return text


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
