"""Pure text helpers for subject lines, HTML bodies, and addresses."""

from __future__ import annotations

import re
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

_PREFIX = re.compile(r"^(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_BREAK_TAGS = {"p", "br", "div", "tr", "blockquote", "li", "h1", "h2", "h3"}


def subject_norm(subject: str | None) -> str | None:
    if subject is None:
        return None
    text = subject.strip()
    while True:
        nxt = _PREFIX.sub("", text, count=1)
        if nxt == text:
            break
        text = nxt.strip()
    return _SPACE.sub(" ", text).strip()


def parse_address(value: str | None) -> tuple[str | None, str | None, str | None]:
    if not value:
        return None, None, None
    name, addr = parseaddr(value)
    addr = addr.strip().lower()
    if "@" not in addr:
        return (name or None), None, None
    return (name or None), addr, addr.rsplit("@", 1)[1]


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


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
