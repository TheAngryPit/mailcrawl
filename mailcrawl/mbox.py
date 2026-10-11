"""Stream a Google Takeout mbox into label and message records.

The Takeout shape is not verified on real mail: the ``From <number>@...``
separator, the ``X-GM-*`` and ``X-Gmail-*`` header names, and their decimal
encoding all come from the approved plan, not from a sanitized sample. Every
value that does not parse falls back. Nothing in a message raises.
"""

from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from email.header import decode_header, make_header
from pathlib import Path
from typing import BinaryIO

from mailcrawl.contract import PSEUDO_LABEL, LabelRec, MessageRec
from mailcrawl.eml import eml_record
from mailcrawl.gognative import ExportInvalid
from mailcrawl.textnorm import parse_date

PROVISIONAL = "mbox:"
RFC822 = "rfc822:"
RAW_PATH = "mbox:"

_GMAIL_ID_MAX = 1 << 64
_SEPARATOR_ID = re.compile(rb"^From (\d+)@\S*")
_SEPARATOR_REST = re.compile(rb"^From \S+\s+(.*?)\s*$")
_ESCAPED_FROM = re.compile(rb"^>+From ")
_TAKEOUT_HEADERS = ("x-gm-", "x-gmail-", "x-google-")
_HEX = re.compile(r"[0-9a-f]+")
_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")
_HEADER_END = re.compile(rb"\r?\n\r?\n")
_READ = ("x-gm-msgid", "x-gm-thrid", "x-gmail-message-id", "x-gmail-labels", "date")

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
    date: str | None = None


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

    def __init__(self, path: Path, known: dict[str, LabelRec], seed: tuple[LabelRec, ...] = ()) -> None:
        if not path.is_file():
            raise ExportInvalid("missing_file", "mbox file is missing", 1)
        self.path = path
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
            for offset, separator, raw in _entries(handle, self._digest.update):
                parsed = self._parse(offset, separator, raw, f"{raw_path}#{offset}")
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

    def _parse(
        self, offset: int, separator: bytes, raw: bytes, raw_path: str
    ) -> tuple[list[LabelRec], MessageRec] | Refused:
        try:
            stripped, takeout = _strip_takeout(raw)
            draft = eml_record(
                stripped,
                gmail_message_id="",
                gmail_thread_id="",
                label_ids=(),
                internal_date=0,
                raw_path=raw_path,
            )
        except Exception:
            return Refused(offset, "unparseable")
        message_id = (
            gmail_hex(takeout.gm_msgid, decimal_only=True)
            or gmail_hex(takeout.gmail_message_id, decimal_only=False)
            or _separator_id(separator)
        )
        if message_id is None:
            if not draft.rfc822_message_id or not _bare_message_id(draft.rfc822_message_id):
                return Refused(offset, "no_message_id")
            message_id = rfc822_key(draft.rfc822_message_id)
        own = _bare_message_id(draft.rfc822_message_id or "") or message_id
        thread_id = gmail_hex(takeout.gm_thrid, decimal_only=True) or provisional_thread(
            thread_root(draft.references_hdr, draft.in_reply_to, own)
        )
        labels = self._labels(takeout.labels)
        return labels, replace(
            draft,
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            label_ids=tuple(dict.fromkeys(label.gmail_label_id for label in labels)),
            internal_date=_internal_date(takeout.date, separator),
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


def _entries(handle: BinaryIO, digest: Callable[[bytes], None]) -> Iterator[tuple[int, bytes, bytes]]:
    offset = 0
    start = 0
    separator: bytes | None = None
    lines: list[bytes] = []
    previous_blank = True
    for line in handle:
        digest(line)
        blank = line.strip(b"\r\n") == b""
        if line.startswith(b"From ") and previous_blank:
            if separator is not None:
                yield start, separator, _join(lines)
            separator, start, lines = line, offset, []
        elif separator is None:
            if not blank:
                raise ExportInvalid("unsupported_schema", "file does not start with an mbox From line", 3)
        else:
            lines.append(_canonical(line[1:] if _ESCAPED_FROM.match(line) else line))
        previous_blank = blank
        offset += len(line)
    if separator is None:
        raise ExportInvalid("unsupported_schema", "file has no mbox From line", 3)
    yield start, separator, _join(lines)


def _canonical(line: bytes) -> bytes:
    """End every line with CRLF so the hash does not depend on the file's mixed endings."""
    if line.endswith(b"\n"):
        return line.rstrip(b"\r\n") + b"\r\n"
    return line


def _join(lines: list[bytes]) -> bytes:
    if lines and lines[-1].strip(b"\r\n") == b"":
        lines.pop()
    return b"".join(lines)


def _strip_takeout(raw: bytes) -> tuple[bytes, _Takeout]:
    """Drop ``X-Gm-*``, ``X-Gmail-*``, and ``X-Google-*`` from the header block and read Takeout values."""
    found = _HEADER_END.search(raw)
    head, tail = (raw[: found.end()], raw[found.end() :]) if found else (raw, b"")
    kept: list[bytes] = []
    values: dict[str, str] = {}
    capturing: str | None = None
    dropping = False
    for line in head.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t"):
            if capturing is not None:
                values[capturing] += " " + _header_text(line)
            if not dropping:
                kept.append(line)
            continue
        name, colon, rest = line.partition(b":")
        current = name.strip().decode("ascii", "replace").lower() if colon else ""
        dropping = current.startswith(_TAKEOUT_HEADERS)
        capturing = current if current in _READ and current not in values else None
        if capturing is not None:
            values[capturing] = _header_text(rest)
        if not dropping:
            kept.append(line)
    takeout = _Takeout(
        gm_msgid=values.get("x-gm-msgid"),
        gm_thrid=values.get("x-gm-thrid"),
        gmail_message_id=values.get("x-gmail-message-id"),
        labels=_decoded(values.get("x-gmail-labels")),
        date=values.get("date"),
    )
    return b"".join(kept) + tail, takeout


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


def _internal_date(header: str | None, separator: bytes) -> int:
    rest = _SEPARATOR_REST.match(separator)
    fallback = rest.group(1).decode("utf-8", "replace") if rest else None
    for candidate in (header, fallback):
        parsed = parse_date(candidate) if candidate else None
        if parsed is not None and parsed >= 0:
            return parsed
    return 0


def _bare_message_id(value: str) -> str:
    return value.strip().strip("<>").strip()
