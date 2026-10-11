"""Read a gog backup plaintext export.

`gog backup export` writes `gmail/<account>/labels.json` and
`gmail/<account>/messages/index.jsonl`. The `eml` field is a slash-separated
path relative to the export `--out` directory, from `backupExportMessageEMLPath`.
`internalDate` is Gmail's unix millisecond clock.
"""

from __future__ import annotations

import email
import hashlib
import json
from email import policy
from pathlib import Path, PurePosixPath

from mailcrawl.contract import ExportDocument, LabelRec, MessageRec, infer_kind
from mailcrawl.eml import header_text, view_message
from mailcrawl.textnorm import parse_date

_INDEX_KNOWN = frozenset(
    {
        "id",
        "threadId",
        "historyId",
        "internalDate",
        "labelIds",
        "sizeEstimate",
        "subject",
        "from",
        "to",
        "cc",
        "date",
        "eml",
        "markdown",
        "attachments",
    }
)


class ExportInvalid(Exception):
    """The directory is not a usable gog export."""

    def __init__(self, error: str, detail: str, exit_code: int) -> None:
        self.error = error
        self.exit_code = exit_code
        super().__init__(detail)


def load_native(export_dir: Path) -> tuple[ExportDocument, str]:
    root, labels_path, index_path = _locate(export_dir)
    labels = _parse_labels(labels_path)
    raw_index = _read_bytes(index_path, "messages/index.jsonl")
    messages = _parse_index(raw_index, root)
    return (
        ExportDocument(
            manifest_messages=len(messages),
            labels=labels,
            messages=tuple(messages),
            raw_json=None,
        ),
        hashlib.sha256(raw_index).hexdigest(),
    )


def _unsupported(detail: str) -> ExportInvalid:
    return ExportInvalid("unsupported_schema", detail, 3)


def _bad(detail: str) -> ExportInvalid:
    return ExportInvalid("malformed_record", detail, 3)


def _locate(export_dir: Path) -> tuple[Path, Path, Path]:
    if not export_dir.is_dir():
        raise _unsupported("export directory is missing")
    gmail = export_dir / "gmail"
    found: list[Path] = []
    if gmail.is_dir():
        for child in sorted(path for path in gmail.iterdir() if path.is_dir()):
            labels = child / "labels.json"
            index = child / "messages" / "index.jsonl"
            if labels.is_file() and index.is_file():
                found.append(child)
    if len(found) > 1:
        names = ", ".join(path.name for path in found)
        raise _unsupported(f"export has more than one gmail account directory: {names}")
    if len(found) == 1:
        account = found[0]
        return export_dir, account / "labels.json", account / "messages" / "index.jsonl"
    labels = export_dir / "labels.json"
    index = export_dir / "messages" / "index.jsonl"
    if labels.is_file() and index.is_file():
        # The stored eml path still starts at gmail/<account>/, so the root is --out.
        if export_dir.parent.name == "gmail":
            return export_dir.parent.parent, labels, index
        return export_dir, labels, index
    raise _unsupported(
        "export directory has no export.json and no gmail account with labels.json and messages/index.jsonl"
    )


def _parse_labels(path: Path) -> tuple[LabelRec, ...]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _bad("labels.json is not valid JSON") from exc
    if not isinstance(document, list):
        raise _bad("labels.json is not an array")
    labels: list[LabelRec] = []
    seen: set[str] = set()
    for index, item in enumerate(document, start=1):
        if not isinstance(item, dict):
            raise _bad(f"labels.json entry {index} is not an object")
        label_id = item.get("id")
        name = item.get("name")
        if not isinstance(label_id, str) or not label_id:
            raise _bad(f"labels.json entry {index} is missing id")
        if not isinstance(name, str) or not name:
            raise _bad(f"labels.json entry {index} id {label_id} is missing name")
        if label_id in seen:
            raise _bad(f"labels.json repeats label id {label_id}")
        seen.add(label_id)
        kind = item.get("type")
        if kind is None or kind == "":
            kind = infer_kind(label_id)
        elif not isinstance(kind, str) or kind not in ("system", "user"):
            raise _bad(f"labels.json entry {index} id {label_id} has an unsupported type")
        labels.append(LabelRec(label_id, name, kind))
    return tuple(labels)


def _parse_index(raw: bytes, root: Path) -> list[MessageRec]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise _bad("messages/index.jsonl is not valid UTF-8") from exc
    messages: list[MessageRec] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        messages.append(_parse_record(line, line_no, root))
    return messages


def _parse_record(line: str, line_no: int, root: Path) -> MessageRec:
    try:
        item = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _bad(f"messages/index.jsonl line {line_no} is not valid JSON") from exc
    if not isinstance(item, dict):
        raise _bad(f"messages/index.jsonl line {line_no} is not an object")
    message_id = _required_str(item, "id", line_no)
    thread_value = item.get("threadId")
    if not isinstance(thread_value, str) or not thread_value:
        raise _bad(f"record {message_id} line {line_no} is missing threadId")
    thread_id = thread_value
    where = f"record {message_id} line {line_no}"
    label_ids = _label_ids(item.get("labelIds"), where)
    internal_date = _internal_date(item, where)
    index_from = _optional_str(item.get("from"), where, "from")
    index_to = _address_list(item.get("to"), where, "to")
    index_cc = _address_list(item.get("cc"), where, "cc")
    index_subject = _optional_str(item.get("subject"), where, "subject")
    eml_rel = item.get("eml")
    if not isinstance(eml_rel, str) or not eml_rel:
        raise _bad(f"{where} has no eml path")
    eml_path = _resolve_eml(root, eml_rel, where)
    raw_bytes = _read_bytes(eml_path, eml_rel, where)
    parsed = email.message_from_bytes(raw_bytes, policy=policy.default)
    body, headers, parts = view_message(parsed)
    unknown = {key: value for key, value in item.items() if key not in _INDEX_KNOWN}
    history = item.get("historyId")
    if history is not None and not isinstance(history, str):
        raise _bad(f"{where} field historyId is not a string")
    size = _size(item.get("sizeEstimate"), where)
    if size is None:
        size = len(raw_bytes)
    return MessageRec(
        gmail_message_id=message_id,
        gmail_thread_id=thread_id,
        label_ids=tuple(label_ids),
        internal_date=internal_date,
        rfc822_message_id=headers.get("message-id"),
        from_hdr=header_text(parsed, "from") or index_from,
        to_hdr=header_text(parsed, "to") or index_to,
        cc_hdr=header_text(parsed, "cc") or index_cc,
        bcc_hdr=header_text(parsed, "bcc"),
        reply_to_hdr=headers.get("reply-to"),
        subject=header_text(parsed, "subject") or index_subject,
        snippet=None,
        gmail_history_id=history if isinstance(history, str) else None,
        body_text=body,
        headers=tuple(sorted(headers.items())),
        attachments=parts,
        raw_json=_raw_json(unknown),
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw_path=str(eml_path),
        size_bytes=size,
        in_reply_to=headers.get("in-reply-to"),
        references_hdr=headers.get("references"),
        auth_results=headers.get("authentication-results"),
    )


def _required_str(item: dict[str, object], key: str, line_no: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise _bad(f"messages/index.jsonl line {line_no} is missing {key}")
    return value


def _optional_str(value: object, where: str, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad(f"{where} field {field} is not a string")
    return value or None


def _label_ids(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _bad(f"{where} field labelIds is not an array of strings")
    return value


def _address_list(value: object, where: str, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _bad(f"{where} field {field} is not an array of strings")
    if not value:
        return None
    return ", ".join(value)


def _internal_date(item: dict[str, object], where: str) -> int:
    if "internalDate" in item and item["internalDate"] is not None:
        value = item["internalDate"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _bad(f"{where} field internalDate is not an integer")
        # Gmail and the gog index store unix milliseconds.
        return value // 1000
    parsed = parse_date(item.get("date"))
    if parsed is None:
        raise _bad(f"{where} is missing internalDate and date")
    return parsed


def _size(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _bad(f"{where} field sizeEstimate is not an integer")
    return value


def _resolve_eml(root: Path, rel: str, where: str) -> Path:
    if "\x00" in rel or "\\" in rel or rel.startswith("/") or ":" in rel:
        raise ExportInvalid("unsafe_path", f"{where} has an unsafe eml path", 1)
    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ExportInvalid("unsafe_path", f"{where} has an unsafe eml path", 1)
    base = root.resolve()
    candidate = base.joinpath(*pure.parts).resolve()
    if not candidate.is_relative_to(base):
        raise ExportInvalid("unsafe_path", f"{where} eml path escapes the export directory", 1)
    if not candidate.is_file():
        raise ExportInvalid("missing_file", f"{where} eml file is missing: {rel}", 1)
    return candidate


def _read_bytes(path: Path, label: str, where: str | None = None) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        if where is None:
            raise ExportInvalid("missing_file", f"{label} is missing", 1) from exc
        raise ExportInvalid("missing_file", f"{where} eml file is missing: {label}", 1) from exc


def _raw_json(unknown: dict[str, object]) -> str | None:
    if not unknown:
        return None
    return json.dumps(unknown, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
