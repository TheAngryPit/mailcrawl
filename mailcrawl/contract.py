"""Fixture contract mailcrawl.gog-export.v0.

Key names belong to this contract. They are not verified gog field names.
A document fails closed when the schema label differs or any of the four
required message keys is missing.
"""

from __future__ import annotations

import email
import hashlib
import json
from dataclasses import dataclass
from email import policy
from pathlib import Path

from mailcrawl.schema import HEADER_ALLOW
from mailcrawl.textnorm import html_to_text, parse_date

UNSUPPORTED_SCHEMA = "unsupported_schema"
CONTRACT_V0 = "mailcrawl.gog-export.v0"

MESSAGE_REQUIRED = (
    "example_message_id",
    "example_thread_id",
    "example_label_ids",
    "example_date",
)

MESSAGE_KNOWN = frozenset(
    {
        *MESSAGE_REQUIRED,
        "example_internal_date",
        "example_rfc822_message_id",
        "example_from",
        "example_to",
        "example_cc",
        "example_bcc",
        "example_reply_to",
        "example_subject",
        "example_snippet",
        "example_history_id",
        "example_body",
        "example_html",
        "example_attachments",
        "example_headers",
        "example_raw_file",
        "example_size",
    }
)

ENVELOPE_KNOWN = frozenset(
    {
        "schema",
        "synthetic",
        "note",
        "example_manifest",
        "example_labels",
        "example_messages",
    }
)

LABEL_KNOWN = frozenset({"example_label_id", "example_name", "example_kind"})
ATTACHMENT_KNOWN = frozenset(
    {"example_name", "example_mime", "example_size", "example_attachment_id"}
)

_HEADER_ALLOW = frozenset(HEADER_ALLOW)
_SYSTEM_LABELS = frozenset(
    {
        "INBOX",
        "SENT",
        "DRAFT",
        "DRAFTS",
        "SPAM",
        "TRASH",
        "UNREAD",
        "STARRED",
        "IMPORTANT",
        "CHAT",
    }
)


class ExportRejected(Exception):
    """The document is not the v0 fixture contract."""


class ManifestMismatch(Exception):
    """example_manifest.messages does not match the message list."""


@dataclass(frozen=True)
class AttachmentRec:
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    attachment_id: str | None
    extracted_text: str | None = None
    extract_status: str = "pending"
    content_id: str | None = None
    is_inline: int = 0


@dataclass(frozen=True)
class LabelRec:
    gmail_label_id: str
    name: str
    kind: str


@dataclass(frozen=True)
class MessageRec:
    gmail_message_id: str
    gmail_thread_id: str
    label_ids: tuple[str, ...]
    internal_date: int
    rfc822_message_id: str | None
    from_hdr: str | None
    to_hdr: str | None
    cc_hdr: str | None
    bcc_hdr: str | None
    reply_to_hdr: str | None
    subject: str | None
    snippet: str | None
    gmail_history_id: str | None
    body_text: str | None
    headers: tuple[tuple[str, str], ...]
    attachments: tuple[AttachmentRec, ...]
    raw_json: str | None
    raw_sha256: str
    raw_path: str | None
    size_bytes: int | None
    in_reply_to: str | None
    references_hdr: str | None
    auth_results: str | None


@dataclass(frozen=True)
class ExportDocument:
    manifest_messages: int
    labels: tuple[LabelRec, ...]
    messages: tuple[MessageRec, ...]
    raw_json: str | None


def classify_export(document: object) -> str:
    """Return ``ok`` or ``unsupported_schema``."""
    try:
        parse_export(document)
    except ExportRejected:
        return UNSUPPORTED_SCHEMA
    return "ok"


def parse_export(document: object, root: Path | None = None) -> ExportDocument:
    if not isinstance(document, dict) or document.get("schema") != CONTRACT_V0:
        raise ExportRejected()
    raw_messages = document.get("example_messages")
    if not isinstance(raw_messages, list):
        raise ExportRejected()
    labels = _parse_labels(document.get("example_labels"))
    messages = tuple(_parse_message(item, root) for item in raw_messages)
    manifest_messages = _manifest_count(document.get("example_manifest"), len(messages))
    unknown = {key: value for key, value in document.items() if key not in ENVELOPE_KNOWN}
    return ExportDocument(
        manifest_messages=manifest_messages,
        labels=labels,
        messages=messages,
        raw_json=_raw_json(unknown),
    )


def infer_kind(label_id: str) -> str:
    if label_id in _SYSTEM_LABELS or label_id.startswith("CATEGORY_"):
        return "system"
    return "user"


def _manifest_count(manifest: object, message_count: int) -> int:
    if manifest is None:
        return message_count
    if not isinstance(manifest, dict) or "messages" not in manifest:
        raise ExportRejected()
    count = manifest["messages"]
    if isinstance(count, bool) or not isinstance(count, int):
        raise ExportRejected()
    if count != message_count:
        raise ManifestMismatch(
            f"manifest messages {count} does not match list length {message_count}"
        )
    return count


def _parse_labels(value: object) -> tuple[LabelRec, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ExportRejected()
    labels: list[LabelRec] = []
    for item in value:
        if not isinstance(item, dict):
            raise ExportRejected()
        label_id = item.get("example_label_id")
        if not isinstance(label_id, str) or not label_id:
            raise ExportRejected()
        name = item.get("example_name")
        if name is None:
            name = label_id
        if not isinstance(name, str) or not name:
            raise ExportRejected()
        kind = item.get("example_kind")
        if kind is None:
            kind = infer_kind(label_id)
        if kind not in ("system", "user"):
            raise ExportRejected()
        labels.append(LabelRec(label_id, name, kind))
    return tuple(labels)


def _parse_message(item: object, root: Path | None) -> MessageRec:
    if not isinstance(item, dict):
        raise ExportRejected()
    for key in MESSAGE_REQUIRED:
        if key not in item or item[key] is None:
            raise ExportRejected()
    message_id = item["example_message_id"]
    thread_id = item["example_thread_id"]
    label_ids = item["example_label_ids"]
    if not isinstance(message_id, str) or not message_id:
        raise ExportRejected()
    if not isinstance(thread_id, str) or not thread_id:
        raise ExportRejected()
    if not isinstance(label_ids, list) or not all(
        isinstance(label_id, str) and label_id for label_id in label_ids
    ):
        raise ExportRejected()
    internal_date = parse_date(item["example_date"])
    if "example_internal_date" in item and item["example_internal_date"] is not None:
        internal_date = parse_date(item["example_internal_date"])
    if internal_date is None:
        raise ExportRejected()
    raw_bytes, raw_path = _read_raw(item.get("example_raw_file"), root)
    headers = _headers(item)
    body = _body(item, raw_bytes)
    attachments, attachment_unknown = _attachments(item.get("example_attachments"))
    unknown = {key: value for key, value in item.items() if key not in MESSAGE_KNOWN}
    if attachment_unknown:
        unknown["example_attachments_unknown"] = attachment_unknown
    size = item.get("example_size")
    if isinstance(size, bool) or not isinstance(size, int):
        size = len(raw_bytes) if raw_bytes is not None else (len(body.encode()) if body else None)
    history = item.get("example_history_id")
    snippet = item.get("example_snippet")
    return MessageRec(
        gmail_message_id=message_id,
        gmail_thread_id=thread_id,
        label_ids=tuple(label_ids),
        internal_date=internal_date,
        rfc822_message_id=_optional_str(headers.get("message-id")),
        from_hdr=_optional_str(item.get("example_from")),
        to_hdr=_optional_str(item.get("example_to")),
        cc_hdr=_optional_str(item.get("example_cc")),
        bcc_hdr=_optional_str(item.get("example_bcc")),
        reply_to_hdr=_optional_str(headers.get("reply-to") or item.get("example_reply_to")),
        subject=_optional_str(item.get("example_subject")),
        snippet=_optional_str(snippet) if "example_snippet" in item else None,
        gmail_history_id=_optional_str(history) if "example_history_id" in item else None,
        body_text=body,
        headers=tuple(sorted(headers.items())),
        attachments=attachments,
        raw_json=_raw_json(unknown),
        raw_sha256=_raw_sha256(item, raw_bytes),
        raw_path=raw_path,
        size_bytes=size,
        in_reply_to=_optional_str(headers.get("in-reply-to")),
        references_hdr=_optional_str(headers.get("references")),
        auth_results=_optional_str(headers.get("authentication-results")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    return value


def _headers(item: dict[str, object]) -> dict[str, str]:
    raw = item.get("example_headers") or {}
    if not isinstance(raw, dict):
        raise ExportRejected()
    headers: dict[str, str] = {}
    for name, value in raw.items():
        key = str(name).strip().lower()
        if key in _HEADER_ALLOW and value is not None:
            headers[key] = str(value)
    rfc822 = item.get("example_rfc822_message_id")
    if isinstance(rfc822, str) and rfc822 and "message-id" not in headers:
        headers["message-id"] = rfc822
    reply_to = item.get("example_reply_to")
    if isinstance(reply_to, str) and reply_to and "reply-to" not in headers:
        headers["reply-to"] = reply_to
    return headers


def _body(item: dict[str, object], raw_bytes: bytes | None) -> str | None:
    body = item.get("example_body")
    if isinstance(body, str):
        return body
    html = item.get("example_html")
    if isinstance(html, str):
        return html_to_text(html)
    if raw_bytes is None:
        return None
    return _body_from_eml(raw_bytes)


def _body_from_eml(raw_bytes: bytes) -> str | None:
    message = email.message_from_bytes(raw_bytes, policy=policy.default)
    plain: str | None = None
    html: str | None = None
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            kind = part.get_content_type()
            try:
                text = part.get_content()
            except (KeyError, LookupError, ValueError):
                continue
            if not isinstance(text, str):
                continue
            if kind == "text/plain" and plain is None:
                plain = text
            elif kind == "text/html" and html is None:
                html = text
    else:
        kind = message.get_content_type()
        try:
            text = message.get_content()
        except (KeyError, LookupError, ValueError):
            text = None
        if isinstance(text, str) and kind == "text/plain":
            plain = text
        elif isinstance(text, str) and kind == "text/html":
            html = text
    if plain is not None:
        return plain
    if html is not None:
        return html_to_text(html)
    return None


def _attachments(
    value: object,
) -> tuple[tuple[AttachmentRec, ...], list[dict[str, object]]]:
    if value is None:
        return (), []
    if not isinstance(value, list):
        raise ExportRejected()
    records: list[AttachmentRec] = []
    unknown: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ExportRejected()
        size = item.get("example_size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
            raise ExportRejected()
        records.append(
            AttachmentRec(
                filename=_optional_str(item.get("example_name")),
                mime_type=_optional_str(item.get("example_mime")),
                size_bytes=size if isinstance(size, int) else None,
                attachment_id=_optional_str(item.get("example_attachment_id")),
            )
        )
        extra = {key: val for key, val in item.items() if key not in ATTACHMENT_KNOWN}
        if extra:
            unknown.append(extra)
    return tuple(records), unknown


def _read_raw(value: object, root: Path | None) -> tuple[bytes | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value:
        raise ExportRejected()
    if root is None:
        return None, value
    base = root.resolve()
    candidate = (base / value).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        raise ExportRejected()
    return candidate.read_bytes(), str(candidate)


def _raw_sha256(item: dict[str, object], raw_bytes: bytes | None) -> str:
    if raw_bytes is not None:
        return hashlib.sha256(raw_bytes).hexdigest()
    payload = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _raw_json(unknown: dict[str, object]) -> str | None:
    if not unknown:
        return None
    return json.dumps(unknown, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
