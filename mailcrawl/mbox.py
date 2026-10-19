# SPDX-License-Identifier: MIT
"""Stream a Google Takeout mbox into label and message records.

SPEC section 4.1 lists which Takeout facts were observed on real mail and
which are still assumptions. Every value that does not parse falls back.
Nothing in a message raises.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
import email
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from typing import BinaryIO

from mailcrawl.contract import PSEUDO_LABEL, LabelRec, MessageRec
from mailcrawl.eml import LimitedFeedParser, allow_headers, parsed_record
from mailcrawl import inputio
from mailcrawl.inputio import Refused
from mailcrawl.gognative import ExportInvalid
from mailcrawl.clock import as_of
from mailcrawl.takeout_labels import resolved_label, system_id
from mailcrawl.textnorm import chosen_stamp, parse_date

PROVISIONAL = "mbox:"
RFC822 = "rfc822:"
RAW_PATH = "mbox:"

_GMAIL_ID_MAX = 1 << 64
_SEPARATOR_ID = re.compile(rb"^From (\d+)@\S*")
_SEPARATOR_REST = re.compile(rb"^From \S+\s+(.*?)\s*$")
_TAKEOUT_FROM = re.compile(rb"^From \d+@xxx(?:\s|$)")
_ESCAPED_FROM = re.compile(rb"^>+From ")
_TAKEOUT_HEADERS = ("x-gm-", "x-gmail-", "x-google-")
_HEX = re.compile(r"[0-9a-f]+")
_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")
_BLANK = b"\r\n"
_FEED_CHUNK = 1 << 16
_READ = ("x-gm-msgid", "x-gm-thrid", "x-gmail-message-id", "x-gmail-labels")

PSEUDO_LABELS: dict[str, str] = {
    "opened": PSEUDO_LABEL + "opened",
    "aberto": PSEUDO_LABEL + "opened",
    "aberta": PSEUDO_LABEL + "opened",
    "archived": PSEUDO_LABEL + "archived",
    "arquivado": PSEUDO_LABEL + "archived",
    "arquivada": PSEUDO_LABEL + "archived",
}


@dataclass(frozen=True)
class _Takeout:
    gm_msgid: str | None = None
    gm_thrid: str | None = None
    gmail_message_id: str | None = None
    labels: str | None = None


def gmail_hex(value: str | bytes | None, *, decimal_only: bool) -> str | None:
    """Lowercase hex without padding, or None when the value is not an id."""
    if value is None:
        return None
    text = (value.decode("ascii", "replace") if isinstance(value, bytes) else value).strip().lower()
    if len(text) > 24:
        return None
    if text.isascii() and text.isdigit():
        number = int(text)
    elif decimal_only:
        return None
    elif text.startswith("0x") and _HEX.fullmatch(text[2:]):
        number = int(text[2:], 16)
    elif _HEX.fullmatch(text):
        number = int(text, 16)
    else:
        return None
    if number <= 0 or number >= _GMAIL_ID_MAX:
        return None
    return format(number, "x")


def label_names(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        row = next(csv.reader([value], skipinitialspace=True), [])
    except csv.Error:
        row = value.split(",")
    return [name.strip() for name in row if name.strip()]


def system_label(name: str) -> str | None:
    return system_id(name)


def thread_root(references: str | None, in_reply_to: str | None, own: str) -> str:
    for header in (references, in_reply_to):
        if header:
            found = _MESSAGE_ID.search(header)
            if found:
                return found.group(1)
            if header.strip():
                return header.split()[0]
    return own


def rfc822_key(message_id: str) -> str:
    return RFC822 + hashlib.sha256(_bare_message_id(message_id).encode()).hexdigest()


def provisional_thread(root: str) -> str:
    return PROVISIONAL + hashlib.sha256(root.encode()).hexdigest()


class MboxSource:
    """Yield labels and messages from one mbox file, one message in memory at a time."""

    def __init__(
        self,
        path: Path,
        known: dict[str, LabelRec],
        seed: tuple[LabelRec, ...] = (),
        stored: Callable[[str], bool] = lambda key: False,
        label_map: dict[str, str] | None = None,
        limits: inputio.ImportLimits | None = None,
    ) -> None:
        if not path.is_file():
            raise ExportInvalid("missing_file", "mbox file is missing", 1)
        self.path = path
        self._stored = stored
        self._known = dict(known)
        self._seed = seed
        self._label_map = {} if label_map is None else label_map
        self._limits = inputio.DEFAULT_MBOX_IMPORT_LIMITS if limits is None else limits
        self._digest = hashlib.sha256()
        self._done = False

    def sha256(self) -> str:
        if not self._done:
            raise RuntimeError("mbox source was not read to the end")
        return self._digest.hexdigest()

    def items(self) -> Iterator[LabelRec | MessageRec | Refused]:
        emitted = {label.gmail_label_id for label in self._known.values()}
        for label in self._seed:
            emitted.add(label.gmail_label_id)
            yield label
        raw_path = RAW_PATH + str(self.path.resolve())
        with inputio.SafeRoot(self.path.parent) as reader, reader.open(self.path.name) as handle:
            initial_stat = os.fstat(handle.fileno())
            initial_size = initial_stat.st_size
            if (
                self._limits.max_total_bytes is not None
                and initial_size > self._limits.max_total_bytes
            ):
                raise inputio.ImportExhausted("mbox import byte limit exceeded")
            for entry in _entries(
                handle,
                self._digest.update,
                self._stored,
                initial_size=initial_size,
                initial_signature=_file_signature(initial_stat),
                limits=self._limits,
            ):
                parsed = self._parse(entry, f"{raw_path}#{entry.offset}")
                if isinstance(parsed, Refused):
                    yield parsed
                    continue
                labels, message = parsed
                for label in labels:
                    if label.gmail_label_id not in emitted:
                        emitted.add(label.gmail_label_id)
                        yield label
                yield message
        self._done = True

    def _parse(self, entry: _Entry, raw_path: str) -> tuple[list[LabelRec], MessageRec] | Refused:
        offset, separator = entry.offset, entry.separator
        try:
            parsed, raw_sha256, size_bytes, takeout, message_id = entry.finish()
            draft = parsed_record(
                parsed,
                raw_sha256=raw_sha256,
                size_bytes=size_bytes,
                gmail_message_id="",
                gmail_thread_id="",
                label_ids=(),
                internal_date=0,
                raw_path=raw_path,
            )
        except inputio.ImportExhausted:
            raise
        except inputio.InputLimit:
            return Refused(offset, "input_limit")
        except Exception:
            return Refused(offset, "unparseable")
        if message_id is None:
            return Refused(offset, "no_message_id")
        own = _bare_message_id(draft.rfc822_message_id or "") or message_id
        thread_id = gmail_hex(takeout.gm_thrid, decimal_only=True) or provisional_thread(
            thread_root(draft.references_hdr, draft.in_reply_to, own)
        )
        labels = self._labels(takeout.labels)
        raw_date = parsed.get("date")
        stamp, trusted = _internal_date(str(raw_date) if raw_date else None, separator)
        return labels, replace(
            draft,
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            label_ids=tuple(dict.fromkeys(label.gmail_label_id for label in labels)),
            internal_date=stamp,
            date_trusted=trusted,
        )

    def _labels(self, header: str | None) -> list[LabelRec]:
        found: list[LabelRec] = []
        for name in label_names(header):
            label_id = resolved_label(name, self._label_map)
            if label_id is not None:
                found.append(LabelRec(label_id, label_id, "system"))
            elif name.casefold() in PSEUDO_LABELS:
                pseudo = PSEUDO_LABELS[name.casefold()]
                found.append(LabelRec(pseudo, pseudo, "system"))
            elif name in self._known:
                found.append(self._known[name])
            else:
                found.append(LabelRec(PROVISIONAL + name, name, "user"))
        return found


def from_line_is_separator(preceding: bytes | None) -> bool:
    if preceding is None:
        return True
    return preceding.endswith(b"\n") and preceding.strip(b"\r\n") == b""


def _entries(
    handle: BinaryIO,
    digest: Callable[[bytes], None],
    stored: Callable[[str], bool],
    *,
    initial_size: int | None = None,
    initial_signature: tuple[int, int, int, int, int] | None = None,
    limits: inputio.ImportLimits | None = None,
) -> Iterator[_Entry]:
    initial_stat = os.fstat(handle.fileno())
    if initial_size is None:
        initial_size = initial_stat.st_size
    if initial_signature is None:
        initial_signature = _file_signature(initial_stat)
    if initial_stat.st_size != initial_size or _file_signature(initial_stat) != initial_signature:
        raise inputio.ImportExhausted("mbox changed during read")
    policy = inputio.DEFAULT_MBOX_IMPORT_LIMITS if limits is None else limits
    offset = 0
    entry: _Entry | None = None
    preceding: bytes | None = None
    complete = True
    total = 0
    count = 0
    while total < initial_size:
        remaining = initial_size - total
        line = handle.readline(min(inputio.MAX_LINE_BYTES + 1, remaining))
        if not line:
            raise inputio.ImportExhausted("mbox changed during read")
        digest(line)
        total += len(line)
        if policy.max_total_bytes is not None and total > policy.max_total_bytes:
            raise inputio.ImportExhausted("mbox import byte limit exceeded")
        if not complete or len(line) > inputio.MAX_LINE_BYTES:
            if entry is not None:
                entry.reject()
            complete = line.endswith(b"\n")
            offset += len(line)
            preceding = line
            continue
        complete = line.endswith(b"\n")
        if line.startswith(b"From ") and from_line_is_separator(preceding):
            if entry is not None:
                yield entry
            count += 1
            if policy.max_records is not None and count > policy.max_records:
                raise inputio.ImportExhausted("mbox record limit exceeded")
            entry = _Entry(offset, line, stored)
        elif entry is None:
            if line.strip(b"\r\n") != b"":
                raise ExportInvalid("unsupported_schema", "file does not start with an mbox From line", 3)
        else:
            entry.add(_canonical(line[1:] if _ESCAPED_FROM.match(line) else line))
        preceding = line
        offset += len(line)
    final_stat = os.fstat(handle.fileno())
    if total != initial_size or _file_signature(final_stat) != initial_signature:
        raise inputio.ImportExhausted("mbox changed during read")
    if entry is None:
        raise ExportInvalid("unsupported_schema", "file has no mbox From line", 3)
    yield entry


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _canonical(line: bytes) -> bytes:
    """End every line with CRLF so the hash does not depend on the file's mixed endings."""
    if line.endswith(b"\n"):
        return line.rstrip(b"\r\n") + b"\r\n"
    return line


class _Entry:
    """One message in flight. Only its header block is buffered.

    Body lines go to SHA-256 and to a feed parser in fixed-size chunks, so the
    raw message is never held whole. ``email.message_from_bytes`` on a whole
    message splits it into a line list first and peaks near ten times its size.

    When the key is already stored, the body is hashed but not parsed. Every
    outcome for an existing row (duplicate, conflict, or a row from another
    source) needs only the header block and the hash.
    """

    def __init__(self, offset: int, separator: bytes, stored: Callable[[str], bool]) -> None:
        self.offset = offset
        self.separator = separator
        self._stored = stored
        self._key: str | None = None
        self._header: EmailMessage | None = None
        self._head: bytearray | None = bytearray()
        self._held = b""
        self._chunk = bytearray()
        self._hash = hashlib.sha256()
        self._parser = LimitedFeedParser(policy=policy.default)
        self._rejected = False
        self._raw_size = 0
        self._size = 0
        self._takeout = _Takeout()

    def reject(self) -> None:
        self._rejected = True
        self._head = None
        self._chunk.clear()
        self._held = b""
        self._parser = None

    def add(self, line: bytes) -> None:
        if self._rejected:
            return
        self._raw_size += len(line)
        if self._raw_size > inputio.MAX_MESSAGE_BYTES:
            self.reject()
            return
        # A blank line is held back one step: the one before the next separator is not part of the message.
        if self._held:
            self._take(self._held)
            self._held = b""
            if self._rejected:
                return
        if line == _BLANK:
            self._held = line
        else:
            self._take(line)

    def finish(self) -> tuple[EmailMessage, str, int, _Takeout, str | None]:
        if self._rejected:
            raise inputio.InputLimit("message byte limit exceeded")
        if self._head is not None:
            self._end_head()
        self._flush()
        parsed = self._parser.close() if self._header is None else self._header
        assert isinstance(parsed, EmailMessage)
        return parsed, self._hash.hexdigest(), self._size, self._takeout, self._key

    def _take(self, line: bytes) -> None:
        if self._head is None:
            self._chunk += line
            if len(self._chunk) >= _FEED_CHUNK:
                self._flush()
            return
        if len(self._head) + len(line) > inputio.MAX_HEADER_BYTES:
            self.reject()
            return
        self._head += line
        if line == _BLANK:
            self._end_head()

    def _end_head(self) -> None:
        assert self._head is not None
        kept, self._takeout = _strip_takeout(bytes(self._head), self.separator)
        self._head = None
        header: EmailMessage | None = None
        try:
            header = email.message_from_bytes(kept, policy=policy.default)
            rfc822_id = allow_headers(header).get("message-id")
        except Exception:
            rfc822_id = None
        self._key = _message_key(self._takeout, self.separator, rfc822_id)
        if header is not None and self._key is not None and self._stored(self._key):
            self._header = header
        self._chunk += kept
        self._flush()

    def _flush(self) -> None:
        if self._chunk:
            data = bytes(self._chunk)
            self._chunk.clear()
            self._hash.update(data)
            if self._header is None:
                try:
                    self._parser.feed(data)
                except inputio.InputLimit:
                    self.reject()
            self._size += len(data)


def _message_key(takeout: _Takeout, separator: bytes, rfc822_id: str | None) -> str | None:
    found = (
        gmail_hex(takeout.gm_msgid, decimal_only=True)
        or gmail_hex(takeout.gmail_message_id, decimal_only=False)
        or _separator_id(separator)
    )
    if found is not None:
        return found
    if _TAKEOUT_FROM.match(separator) is not None:
        return None
    if rfc822_id and _bare_message_id(rfc822_id):
        return rfc822_key(rfc822_id)
    return None


def _strip_takeout(head: bytes, separator: bytes) -> tuple[bytes, _Takeout]:
    """Drop Google headers and read Takeout values from the leading Google block only."""
    kept: list[bytes] = []
    values: dict[str, str] = {}
    capturing: str | None = None
    dropping = False
    in_block = True
    for line in head.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t"):
            if capturing is not None:
                values[capturing] += " " + _header_text(line)
            if not dropping:
                kept.append(line)
            continue
        name, colon, rest = line.partition(b":")
        current = name.strip().decode("ascii", "replace").lower() if colon else ""
        takeout_header = bool(current) and current.startswith(_TAKEOUT_HEADERS)
        if in_block and current and not takeout_header:
            in_block = False
            capturing = None
        dropping = takeout_header
        capturing = current if in_block and current in _READ and current not in values else None
        if capturing is not None:
            values[capturing] = _header_text(rest)
        if not dropping:
            kept.append(line)
    labels = values.get("x-gmail-labels")
    accept = labels is not None and _TAKEOUT_FROM.match(separator) is not None
    takeout = _Takeout(
        gm_msgid=values.get("x-gm-msgid") if accept else None,
        gm_thrid=values.get("x-gm-thrid") if accept else None,
        gmail_message_id=values.get("x-gmail-message-id") if accept else None,
        labels=_decoded(labels) if accept else None,
    )
    return b"".join(kept), takeout


def _header_text(value: bytes) -> str:
    return value.decode("utf-8", "replace").strip()


def _decoded(value: str | None) -> str | None:
    if value is None or "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, ValueError):
        return value


def _separator_id(separator: bytes) -> str | None:
    if _TAKEOUT_FROM.match(separator) is None:
        return None
    found = _SEPARATOR_ID.match(separator)
    if found is None:
        return None
    return gmail_hex(found.group(1), decimal_only=True)


@dataclass(frozen=True)
class MboxFacts:
    kind: str
    receipt: tuple[int, int] | None = None


def mbox_facts(raw_path: object, gmail_message_id: str) -> MboxFacts:
    """``takeout`` or ``plain`` when this row's separator is at the offset.

    ``unread`` when the file is missing or the separator belongs to another Gmail id.
    An unread row is left unchanged.
    """
    opened = _open_mbox_row(raw_path, gmail_message_id)
    if opened is None:
        return MboxFacts("unread")
    _path, separator, head = opened
    if head.startswith(separator):
        head = head[len(separator) :]
    _kept, takeout = _strip_takeout(head, separator)
    kind = "takeout" if takeout.labels is not None else "plain"
    return MboxFacts(kind, _separator_receipt(separator))


def takeout_block(raw_path: object, gmail_message_id: str) -> bool:
    """True when this row's separator and leading block would be accepted at import."""
    return mbox_facts(raw_path, gmail_message_id).kind == "takeout"


def _separator_receipt(separator: bytes) -> tuple[int, int] | None:
    rest = _SEPARATOR_REST.match(separator)
    fallback = rest.group(1).decode("utf-8", "replace") if rest else None
    receipt = parse_date(fallback) if fallback else None
    if receipt is None or receipt <= 0:
        return None
    return chosen_stamp(receipt, None, as_of(None))


def _open_mbox_row(raw_path: object, gmail_message_id: str) -> tuple[Path, bytes, bytes] | None:
    if not isinstance(raw_path, str) or not raw_path.startswith(RAW_PATH):
        return None
    ref = _mbox_ref(raw_path)
    if ref is None:
        return None
    path, offset = ref
    try:
        if not path.is_file():
            return None
        if not _mbox_boundary(path, offset):
            return None
        separator = _read_mbox_separator(path, offset)
        if _separator_id(separator) != gmail_message_id:
            return None
        head = _read_head(path, offset)
    except OSError:
        return None
    return path, separator, head


def _mbox_ref(raw_path: str) -> tuple[Path, int] | None:
    body = raw_path[len(RAW_PATH) :]
    path_text, sep, offset_text = body.rpartition("#")
    if not sep or not offset_text.isdigit() or len(offset_text) > 12:
        return None
    return Path(path_text), int(offset_text)


def _mbox_boundary(path: Path, offset: int) -> bool:
    return from_line_is_separator(_preceding_line(path, offset))


def _preceding_line(path: Path, offset: int) -> bytes | None:
    if offset == 0:
        return None
    with path.open("rb") as handle:
        handle.seek(offset - 1)
        if handle.read(1) != b"\n":
            return b"x"
        start = 0
        cursor = offset - 2
        while cursor >= 0:
            handle.seek(cursor)
            byte = handle.read(1)
            if byte == b"\n":
                start = cursor + 1
                break
            if byte != b"\r":
                return b"x\n"
            cursor -= 1
        handle.seek(start)
        return handle.read(offset - start)


def _read_mbox_separator(path: Path, offset: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.readline(4096)


def _read_head(path: Path, offset: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while size < 65536:
            line = handle.readline(4096)
            if not line or line in (b"\n", b"\r\n"):
                break
            chunks.append(line)
            size += len(line)
    return b"".join(chunks)


def _internal_date(header: str | None, separator: bytes) -> tuple[int, int]:
    rest = _SEPARATOR_REST.match(separator)
    fallback = rest.group(1).decode("utf-8", "replace") if rest else None
    receipt = parse_date(fallback) if fallback else None
    header_stamp = parse_date(header) if header else None
    return chosen_stamp(receipt, header_stamp, as_of(None))


def _bare_message_id(value: str) -> str:
    return value.strip().strip("<>").strip()
