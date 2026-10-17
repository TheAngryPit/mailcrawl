"""Stream a Google Takeout mbox into label and message records.

SPEC section 4.1 lists which Takeout facts were observed on real mail and
which are still assumptions. Every value that does not parse falls back.
Nothing in a message raises.
"""

from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
import email
from email import policy
from email.feedparser import BytesFeedParser
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from typing import BinaryIO

from mailcrawl.contract import PSEUDO_LABEL, LabelRec, MessageRec
from mailcrawl.eml import allow_headers, parsed_record
from mailcrawl.gognative import ExportInvalid
from mailcrawl.clock import as_of
from mailcrawl.textnorm import chosen_stamp, parse_date

PROVISIONAL = "mbox:"
RFC822 = "rfc822:"
RAW_PATH = "mbox:"

_GMAIL_ID_MAX = 1 << 64
_SEPARATOR_ID = re.compile(rb"^From (\d+)@\S*")
_SEPARATOR_REST = re.compile(rb"^From \S+\s+(.*?)\s*$")
_TAKEOUT_FROM = re.compile(rb"^From \d+@(?:xxx|example\.com)(?:\s|$)")
_ESCAPED_FROM = re.compile(rb"^>+From ")
_TAKEOUT_HEADERS = ("x-gm-", "x-gmail-", "x-google-")
_HEX = re.compile(r"[0-9a-f]+")
_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")
_BLANK = b"\r\n"
_FEED_CHUNK = 1 << 16
_READ = ("x-gm-msgid", "x-gm-thrid", "x-gmail-message-id", "x-gmail-labels")

SYSTEM_LABELS: dict[str, str] = {
    "inbox": "INBOX",
    "caixa de entrada": "INBOX",
    "sent": "SENT",
    "sent mail": "SENT",
    "enviados": "SENT",
    "enviadas": "SENT",
    "e-mails enviados": "SENT",
    "starred": "STARRED",
    "com estrela": "STARRED",
    "important": "IMPORTANT",
    "importante": "IMPORTANT",
    "unread": "UNREAD",
    "não lido": "UNREAD",
    "não lidos": "UNREAD",
    "não lida": "UNREAD",
    "não lidas": "UNREAD",
    "draft": "DRAFT",
    "drafts": "DRAFT",
    "rascunho": "DRAFT",
    "rascunhos": "DRAFT",
    "spam": "SPAM",
    "trash": "TRASH",
    "lixo": "TRASH",
    "lixeira": "TRASH",
    "reciclagem": "TRASH",
    "chat": "CHAT",
    "chats": "CHAT",
    "category personal": "CATEGORY_PERSONAL",
    "categoria pessoal": "CATEGORY_PERSONAL",
    "category social": "CATEGORY_SOCIAL",
    "categoria social": "CATEGORY_SOCIAL",
    "categoria redes sociais": "CATEGORY_SOCIAL",
    "category promotions": "CATEGORY_PROMOTIONS",
    "categoria promoções": "CATEGORY_PROMOTIONS",
    "category updates": "CATEGORY_UPDATES",
    "categoria atualizações": "CATEGORY_UPDATES",
    "category forums": "CATEGORY_FORUMS",
    "categoria fóruns": "CATEGORY_FORUMS",
}
SYSTEM_LABELS.update({label_id.lower(): label_id for label_id in set(SYSTEM_LABELS.values())})

PSEUDO_LABELS: dict[str, str] = {
    "opened": PSEUDO_LABEL + "opened",
    "aberto": PSEUDO_LABEL + "opened",
    "aberta": PSEUDO_LABEL + "opened",
    "archived": PSEUDO_LABEL + "archived",
    "arquivado": PSEUDO_LABEL + "archived",
    "arquivada": PSEUDO_LABEL + "archived",
}


@dataclass(frozen=True)
class Refused:
    offset: int
    reason: str


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
    return SYSTEM_LABELS.get(unicodedata.normalize("NFC", name).casefold())


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
    ) -> None:
        if not path.is_file():
            raise ExportInvalid("missing_file", "mbox file is missing", 1)
        self.path = path
        self._stored = stored
        self._known = dict(known)
        self._seed = seed
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
        with self.path.open("rb") as handle:
            for entry in _entries(handle, self._digest.update, self._stored):
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
            label_id = system_label(name)
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


def _entries(
    handle: BinaryIO, digest: Callable[[bytes], None], stored: Callable[[str], bool]
) -> Iterator[_Entry]:
    offset = 0
    entry: _Entry | None = None
    previous_blank = True
    for line in handle:
        digest(line)
        blank = line.strip(b"\r\n") == b""
        if line.startswith(b"From ") and previous_blank:
            if entry is not None:
                yield entry
            entry = _Entry(offset, line, stored)
        elif entry is None:
            if not blank:
                raise ExportInvalid("unsupported_schema", "file does not start with an mbox From line", 3)
        else:
            entry.add(_canonical(line[1:] if _ESCAPED_FROM.match(line) else line))
        previous_blank = blank
        offset += len(line)
    if entry is None:
        raise ExportInvalid("unsupported_schema", "file has no mbox From line", 3)
    yield entry


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
        self._parser = BytesFeedParser(policy=policy.default)
        self._size = 0
        self._takeout = _Takeout()

    def add(self, line: bytes) -> None:
        # A blank line is held back one step: the one before the next separator is not part of the message.
        if self._held:
            self._take(self._held)
            self._held = b""
        if line == _BLANK:
            self._held = line
        else:
            self._take(line)

    def finish(self) -> tuple[EmailMessage, str, int, _Takeout, str | None]:
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
                self._parser.feed(data)
            self._size += len(data)


def _message_key(takeout: _Takeout, separator: bytes, rfc822_id: str | None) -> str | None:
    found = (
        gmail_hex(takeout.gm_msgid, decimal_only=True)
        or gmail_hex(takeout.gmail_message_id, decimal_only=False)
        or _separator_id(separator)
    )
    if found is None and rfc822_id and _bare_message_id(rfc822_id):
        found = rfc822_key(rfc822_id)
    return found


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
    found = _SEPARATOR_ID.match(separator)
    if found is None:
        return None
    return gmail_hex(found.group(1), decimal_only=True)


def stored_date_trusted(raw_path: object, stored: int) -> int | None:
    """Trust a stored stamp only when the raw source shows it is the receipt clock."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    if raw_path.startswith(RAW_PATH):
        return _mbox_row_trust(raw_path, stored)
    return _header_only_trust(raw_path, stored)


def _mbox_ref(raw_path: str) -> tuple[Path, int] | None:
    body = raw_path[len(RAW_PATH) :]
    path_text, sep, offset_text = body.rpartition("#")
    if not sep or not offset_text.isdigit() or len(offset_text) > 12:
        return None
    return Path(path_text), int(offset_text)


def _mbox_row_trust(raw_path: str, stored: int) -> int:
    ref = _mbox_ref(raw_path)
    if ref is None:
        return 0
    path, offset = ref
    try:
        if not path.is_file():
            return 0
        separator = _read_mbox_separator(path, offset)
    except OSError:
        return 0
    rest = _SEPARATOR_REST.match(separator)
    fallback = rest.group(1).decode("utf-8", "replace") if rest else None
    receipt = parse_date(fallback) if fallback else None
    if receipt is not None and receipt > 0 and stored == receipt:
        return 1
    return 0


def _header_only_trust(raw_path: str, stored: int) -> int | None:
    path = Path(raw_path)
    try:
        if not path.is_file():
            return None
        header = _date_header(_read_head(path, 0))
    except OSError:
        return None
    stamp = parse_date(header) if header else None
    if stamp is not None and stamp > 0 and stored == stamp:
        return 0
    return None


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


def _date_header(head: bytes) -> str | None:
    capturing = False
    value = ""
    for line in head.splitlines():
        if line[:1] in (b" ", b"\t"):
            if capturing:
                value += " " + line.decode("utf-8", "replace").strip()
            continue
        capturing = False
        name, colon, rest = line.partition(b":")
        if colon and name.strip().lower() == b"date":
            capturing = True
            value = rest.decode("utf-8", "replace").strip()
    return value or None


def _internal_date(header: str | None, separator: bytes) -> tuple[int, int]:
    rest = _SEPARATOR_REST.match(separator)
    fallback = rest.group(1).decode("utf-8", "replace") if rest else None
    receipt = parse_date(fallback) if fallback else None
    header_stamp = parse_date(header) if header else None
    return chosen_stamp(receipt, header_stamp, as_of(None))


def _bare_message_id(value: str) -> str:
    return value.strip().strip("<>").strip()
