# SPDX-License-Identifier: MIT
"""Read a gog backup plaintext export.

`gog backup export` writes `gmail/<account>/labels.json` and
`gmail/<account>/messages/index.jsonl`. The `eml` field is a slash-separated
path relative to the export `--out` directory, from `backupExportMessageEMLPath`.
`internalDate` is Gmail's unix millisecond clock.
"""

from __future__ import annotations

import hashlib
import json
import io
from dataclasses import replace
from collections.abc import Iterator
from mailcrawl import inputio
from pathlib import Path, PurePosixPath

from mailcrawl.contract import ExportDocument, LabelRec, MessageRec, infer_kind
from mailcrawl.eml import eml_record
from mailcrawl.clock import as_of
from mailcrawl.textnorm import chosen_stamp, parse_date

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
        super().__init__("".join(char if char.isprintable() else "?" for char in detail[:400]))


def load_native(
    export_dir: Path,
    *,
    limits: inputio.ImportLimits | None = None,
) -> tuple[ExportDocument, str]:
    policy = inputio.DEFAULT_GOG_IMPORT_LIMITS if limits is None else limits
    root = export_dir.parent.parent if export_dir.parent.name == "gmail" else export_dir
    try:
        reader = inputio.SafeRoot(root)
    except OSError as exc:
        raise _unsupported("export directory is missing") from exc
    try:
        root, labels_path, index_path = _locate(export_dir, reader)
        labels = _parse_labels(reader.read(str(labels_path.relative_to(root)), inputio.MAX_LABEL_BYTES))
        raw_index = reader.read(str(index_path.relative_to(root)), inputio.MAX_INDEX_BYTES)
    except inputio.UnsafeInput as exc:
        reader.close()
        raise ExportInvalid("unsafe_path", "unsafe export metadata path", 1) from exc
    except BaseException:
        reader.close()
        raise
    messages = _parse_index(raw_index, root, reader, limits=policy)
    return (
        ExportDocument(
            manifest_messages=sum(bool(line.strip()) for line in raw_index.splitlines()),
            labels=labels,
            messages=messages,
            raw_json=None,
        ),
        hashlib.sha256(raw_index).hexdigest(),
    )


def _unsupported(detail: str) -> ExportInvalid:
    return ExportInvalid("unsupported_schema", detail, 3)


def _bad(detail: str) -> ExportInvalid:
    return ExportInvalid("malformed_record", detail, 3)


def _locate(export_dir: Path, discovery: inputio.SafeRoot) -> tuple[Path, Path, Path]:
    if not export_dir.is_dir():
        raise _unsupported("export directory is missing")
    if export_dir.parent.name == "gmail":
        return export_dir.parent.parent, export_dir / "labels.json", export_dir / "messages/index.jsonl"
    gmail = export_dir / "gmail"
    found: list[Path] = []
    try:
        for name in discovery.directories("gmail"):
            child = gmail / name
            complete = True
            for metadata in (f"gmail/{name}/labels.json", f"gmail/{name}/messages/index.jsonl"):
                try:
                    with discovery.open(metadata):
                        pass
                except FileNotFoundError:
                    complete = False
            if complete:
                found.append(child)
                if len(found) > 1:
                    raise _unsupported("export has more than one gmail account directory")
    except inputio.UnsafeInput as exc:
        raise ExportInvalid("unsafe_path", "unsafe export directory path", 1) from exc
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


def load_labels(path: Path) -> tuple[LabelRec, ...]:
    try:
        return _parse_labels(inputio.read_file(path, inputio.MAX_LABEL_BYTES))
    except inputio.UnsafeInput as exc:
        raise ExportInvalid("unsafe_path", "unsafe labels path", 1) from exc
    except OSError as exc:
        raise ExportInvalid("missing_file", "labels.json is missing", 1) from exc


def _parse_labels(raw: bytes) -> tuple[LabelRec, ...]:
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
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


def _parse_index(
    raw: bytes,
    root: Path,
    pinned: inputio.SafeRoot | None = None,
    *,
    limits: inputio.ImportLimits | None = None,
) -> Iterator[MessageRec | inputio.Refused]:
    policy = inputio.DEFAULT_GOG_IMPORT_LIMITS if limits is None else limits
    budget = inputio.import_budget.get() or inputio.ImportBudget(limits=policy)
    count = 0
    raw_cache: list[tuple[str, MessageRec]] = []
    with pinned if pinned is not None else inputio.SafeRoot(root) as reader:
        handle = io.BytesIO(raw)
        while True:
            line = handle.readline(inputio.MAX_RECORD_BYTES + 1)
            if not line:
                return
            if len(line) > inputio.MAX_RECORD_BYTES:
                raise inputio.InputLimit("native index record limit exceeded")
            if not line.strip():
                continue
            count += 1
            if policy.max_records is not None and count > policy.max_records:
                raise inputio.ImportExhausted("native index record limit exceeded")
            try:
                text = line.decode("utf-8")
                record = _parse_record(text, count, root, reader, budget, raw_cache)
                yield record
            except UnicodeError as exc:
                raise _bad("messages/index.jsonl is not valid UTF-8") from exc
            except inputio.ImportExhausted:
                raise
            except inputio.InputLimit:
                yield inputio.Refused(count, "input_limit")


def _parse_record(line: str, line_no: int, root: Path, reader: inputio.SafeRoot | None = None, budget: inputio.ImportBudget | None = None, cache: list[tuple[str, MessageRec]] | None = None) -> MessageRec:
    try:
        item = json.loads(line)
    except (json.JSONDecodeError, RecursionError) as exc:
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
    internal_date, date_trusted = _internal_date(item, where)
    index_from = _optional_str(item.get("from"), where, "from")
    index_to = _address_list(item.get("to"), where, "to")
    index_cc = _address_list(item.get("cc"), where, "cc")
    index_subject = _optional_str(item.get("subject"), where, "subject")
    eml_rel = item.get("eml")
    if not isinstance(eml_rel, str) or not eml_rel:
        raise _bad(f"{where} has no eml path")
    eml_path = _resolve_eml(root, eml_rel, where)
    base = cache[0][1] if cache and cache[0][0] == eml_rel else None
    if base is None:
        try:
            if reader is not None:
                raw_bytes = inputio.read_import(reader, eml_rel, inputio.MAX_MESSAGE_BYTES, budget)
            else:
                with inputio.SafeRoot(root) as opened:
                    raw_bytes = inputio.read_import(opened, eml_rel, inputio.MAX_MESSAGE_BYTES, budget)
        except inputio.UnsafeInput as exc:
            raise ExportInvalid("unsafe_path", f"{where} has unsafe message path", 1) from exc
        except OSError as exc:
            raise ExportInvalid("missing_file", f"{where} eml file is missing: {eml_rel}", 1) from exc
        base = eml_record(
            raw_bytes, gmail_message_id="", gmail_thread_id="", label_ids=(),
            internal_date=0, raw_path=str(eml_path),
        )
        if cache is not None:
            cache[:] = [(eml_rel, base)]
    unknown = {key: value for key, value in item.items() if key not in _INDEX_KNOWN}
    history = item.get("historyId")
    if history is not None and not isinstance(history, str):
        raise _bad(f"{where} field historyId is not a string")
    estimated_size = _size(item.get("sizeEstimate"), where)
    return replace(
        base,
        gmail_message_id=message_id,
        gmail_thread_id=thread_id,
        label_ids=tuple(label_ids),
        internal_date=internal_date,
        size_bytes=base.size_bytes if estimated_size is None else estimated_size,
        gmail_history_id=history,
        raw_json=_raw_json(unknown),
        date_trusted=date_trusted,
        from_hdr=base.from_hdr or index_from,
        to_hdr=base.to_hdr or index_to,
        cc_hdr=base.cc_hdr or index_cc,
        subject=base.subject or index_subject,
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
    if len(set(value)) != len(value):
        raise _bad(f"{where} repeats labelIds")
    return value


def _address_list(value: object, where: str, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _bad(f"{where} field {field} is not an array of strings")
    if not value:
        return None
    return ", ".join(value)


def _internal_date(item: dict[str, object], where: str) -> tuple[int, int]:
    receipt = None
    if "internalDate" in item and item["internalDate"] is not None:
        value = item["internalDate"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _bad(f"{where} field internalDate is not an integer")
        # Gmail and the gog index store unix milliseconds.
        receipt = value // 1000
    header = parse_date(item.get("date"))
    if receipt is None and header is None:
        raise _bad(f"{where} is missing internalDate and date")
    return chosen_stamp(receipt, header, as_of(None))


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
    return root.absolute().joinpath(*pure.parts)


def _raw_json(unknown: dict[str, object]) -> str | None:
    if not unknown:
        return None
    return json.dumps(unknown, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
