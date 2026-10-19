# SPDX-License-Identifier: MIT
"""Fixture contract mailcrawl.gog-export.v0.

Key names belong to this contract. They are not verified gog field names.
A document fails closed when the schema label differs or any of the four
required message keys is missing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import Iterable
from mailcrawl import inputio
from pathlib import Path

from mailcrawl.extract import PARSER_VERSION
from mailcrawl.schema import HEADER_ALLOW
from mailcrawl.clock import as_of
from mailcrawl.textnorm import chosen_stamp, html_to_text, parse_date

UNSUPPORTED_SCHEMA = "unsupported_schema"
CONTRACT_V0 = "mailcrawl.gog-export.v0"

PSEUDO_LABEL = "pseudo:"
# Every read of labels for rules, plans, or a Gmail write adds this predicate.
GMAIL_LABEL_SQL = "labels.gmail_label_id NOT LIKE 'pseudo:%'"

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
    extract_method: str | None = None
    file_sha256: str | None = None
    ocr_status: str = "not_needed"
    ocr_conf: float | None = None
    page_count: int | None = None
    parser_version: str | None = None
    image_bytes: bytes | None = None


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
    date_trusted: int = 1


@dataclass(frozen=True)
class ExportDocument:
    manifest_messages: int
    labels: tuple[LabelRec, ...]
    messages: Iterable[MessageRec | inputio.Refused]
    raw_json: str | None


def classify_export(document: object) -> str:
    """Return ``ok`` or ``unsupported_schema``."""
    try:
        parsed = parse_export(document)
        tuple(parsed.messages)
    except ExportRejected:
        return UNSUPPORTED_SCHEMA
    return "ok"


def parse_export(
    document: object,
    root: Path | inputio.SafeRoot | None = None,
    *,
    limits: inputio.ImportLimits | None = None,
) -> ExportDocument:
    if not isinstance(document, dict) or document.get("schema") != CONTRACT_V0:
        raise ExportRejected()
    raw_messages = document.get("example_messages")
    if not isinstance(raw_messages, list):
        raise ExportRejected()
    policy = inputio.DEFAULT_GOG_IMPORT_LIMITS if limits is None else limits
    labels = _parse_labels(document.get("example_labels"))
    if policy.max_records is not None and len(raw_messages) > policy.max_records:
        raise inputio.InputLimit("manifest record limit exceeded")
    manifest_messages = _manifest_count(document.get("example_manifest"), len(raw_messages))
    messages = _fixture_messages(raw_messages, root, limits=policy)
    unknown = {key: value for key, value in document.items() if key not in ENVELOPE_KNOWN}
    return ExportDocument(
        manifest_messages=manifest_messages,
        labels=labels,
        messages=messages,
        raw_json=_raw_json(unknown),
    )


def _fixture_messages(
    items: list[object],
    root: Path | inputio.SafeRoot | None,
    *,
    limits: inputio.ImportLimits | None = None,
) -> Iterable[MessageRec | inputio.Refused]:
    reader = root if isinstance(root, inputio.SafeRoot) else (inputio.SafeRoot(root) if root is not None else None)
    policy = inputio.DEFAULT_GOG_IMPORT_LIMITS if limits is None else limits
    account = inputio.import_budget.get()
    token = None
    if account is None:
        account = inputio.ImportBudget(limits=policy)
        token = inputio.import_budget.set(account)
    try:
        for offset, item in enumerate(items):
            size = len(json.dumps(item, ensure_ascii=False).encode())
            inputio.charge_input(size, account)
            try:
                if size > inputio.MAX_RECORD_BYTES:
                    raise inputio.InputLimit("fixture record byte limit exceeded")
                yield _parse_message(item, reader)
            except inputio.ImportExhausted:
                raise
            except inputio.InputLimit:
                yield inputio.Refused(offset, "input_limit")
    finally:
        if reader is not None:
            reader.close()
        if token is not None:
            inputio.import_budget.reset(token)
            account.close()


def is_pseudo_label(label_id: str) -> bool:
    """A Takeout-only label id. It is never a Gmail label and is never sent to Gmail."""
    return label_id.startswith(PSEUDO_LABEL)


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


def _parse_message(item: object, root: Path | inputio.SafeRoot | None) -> MessageRec:
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
    if len(set(label_ids)) != len(label_ids):
        raise ExportRejected()
    header_date = parse_date(item["example_date"])
    receipt = None
    if "example_internal_date" in item and item["example_internal_date"] is not None:
        receipt = parse_date(item["example_internal_date"])
    if header_date is None and receipt is None:
        raise ExportRejected()
    internal_date, date_trusted = chosen_stamp(receipt, header_date, as_of(None))
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
        date_trusted=date_trusted,
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
    from mailcrawl.eml import body_text, parse_bytes, validate_mime
    message = parse_bytes(raw_bytes)
    validate_mime(message)
    return body_text(message)


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
                extract_status="skipped",
                extract_method="no_bytes",
                ocr_status="not_needed",
                parser_version=PARSER_VERSION,
            )
        )
        extra = {key: val for key, val in item.items() if key not in ATTACHMENT_KNOWN}
        if extra:
            unknown.append(extra)
    return tuple(records), unknown


def _read_raw(value: object, root: Path | inputio.SafeRoot | None) -> tuple[bytes | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value:
        raise ExportRejected()
    if root is None:
        return None, value
    try:
        if isinstance(root, inputio.SafeRoot):
            raw = inputio.read_import(root, value, inputio.MAX_MESSAGE_BYTES)
            base = root.path
        else:
            with inputio.SafeRoot(root) as reader:
                raw = inputio.read_import(reader, value, inputio.MAX_MESSAGE_BYTES)
            base = root.absolute()
        return raw, str(base / value)
    except (OSError, inputio.UnsafeInput) as exc:
        raise ExportRejected() from exc


def _raw_sha256(item: dict[str, object], raw_bytes: bytes | None) -> str:
    if raw_bytes is not None:
        return hashlib.sha256(raw_bytes).hexdigest()
    payload = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _raw_json(unknown: dict[str, object]) -> str | None:
    if not unknown:
        return None
    return json.dumps(unknown, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
