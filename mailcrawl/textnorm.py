# SPDX-License-Identifier: MIT
"""Pure text helpers for subject lines, HTML bodies, and addresses."""

from __future__ import annotations

import re
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from mailcrawl.paths import UsageError

_PREFIX = re.compile(r"(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_BREAK_TAGS = {"p", "br", "div", "tr", "blockquote", "li", "h1", "h2", "h3"}
_ON_WROTE = re.compile(r"^On .+ wrote:\s*$", re.IGNORECASE)
_EM_ESCREVEU = re.compile(r"^Em .+ escreveu:\s*$", re.IGNORECASE)
_HEADER_FROM = re.compile(r"^(?:From|De):", re.IGNORECASE)
_HEADER_SENT = re.compile(r"^(?:Sent|Enviada):", re.IGNORECASE)
_ORIGINAL_MESSAGE = "-----Original Message-----"
_SIGNATURE = "-- "
_OUTLOOK_WINDOW = 8


def subject_norm(subject: str | None) -> str | None:
    if subject is None:
        return None
    if len(subject) > 65536:
        raise UsageError("subject exceeds text budget")
    text = subject.strip()
    index = 0
    while match := _PREFIX.match(text, index):
        index = match.end()
    return _SPACE.sub(" ", text[index:]).strip()


def parse_address(value: str | None) -> tuple[str | None, str | None, str | None]:
    if not value:
        return None, None, None
    name, addr = parseaddr(value)
    addr = addr.strip().lower()
    if "@" not in addr:
        return (name or None), None, None
    return (name or None), addr, addr.rsplit("@", 1)[1]


def chosen_stamp(receipt: int | None, header: int | None, moment: int) -> tuple[int, int]:
    """Prefer the receipt clock. A header date is untrusted. A future stamp clamps to ``moment``."""
    if receipt is not None and receipt > 0:
        stamp, trusted = receipt, 1
    elif header is not None and header > 0:
        stamp, trusted = header, 0
    else:
        return 0, 0
    if stamp > moment:
        stamp = moment
    return stamp, trusted


def parse_date(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        try:
            return int(parsedate_to_datetime(stripped).timestamp())
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_body(text: str | None) -> str | None:
    if text is None:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    cut = _suffix_cut(lines)
    if cut is not None:
        lines = lines[:cut]
    kept = [line for line in lines if not line.lstrip().startswith(">")]
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _suffix_cut(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line == _SIGNATURE:
            return index
        stripped = line.strip()
        if stripped == _ORIGINAL_MESSAGE:
            return index
        if _ON_WROTE.match(stripped) or _EM_ESCREVEU.match(stripped):
            return index
        if _HEADER_FROM.match(stripped) and _sent_within(lines, index):
            return index
    return None


def _sent_within(lines: list[str], index: int) -> bool:
    end = min(len(lines), index + 1 + _OUTLOOK_WINDOW)
    for follower in lines[index + 1 : end]:
        if _HEADER_SENT.match(follower.strip()):
            return True
    return False


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
