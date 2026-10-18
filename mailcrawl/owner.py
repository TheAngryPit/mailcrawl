"""Decide whether a message is the owner's, and recompute that flag."""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Literal

from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.textnorm import parse_address

Origin = Literal["sent", "alias"]
_SENT = "SENT"
_AUTHSERV = "mx.google.com"
_HAS_SENT = f"""
EXISTS (
  SELECT 1 FROM message_labels
  LEFT JOIN labels
    ON labels.account_id = message_labels.account_id
   AND labels.gmail_label_id = message_labels.gmail_label_id
  WHERE message_labels.msg_pk = messages.msg_pk
    AND message_labels.gmail_label_id = ?
    AND (labels.gmail_label_id IS NULL OR {GMAIL_LABEL_SQL})
)
"""


@dataclass(frozen=True)
class FromMe:
    origin: Origin | None
    untrusted: bool

    @property
    def mine(self) -> bool:
        return self.origin is not None


@dataclass(frozen=True)
class FromMeReport:
    sent: int
    alias: int
    untrusted: int
    changed: int
    unchanged: int
    applied: bool

    def public(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "applied": self.applied,
            "changed": self.changed,
            "ok": True,
            "sent": self.sent,
            "unchanged": self.unchanged,
            "untrusted": self.untrusted,
        }


def classify_from_me(
    *,
    has_sent: bool,
    from_header: str | None,
    auth_results: str | None,
    aliases: frozenset[str],
) -> FromMe:
    if has_sent:
        return FromMe("sent", False)
    _name, address, domain = parse_address(from_header)
    if address is None or domain is None or _fold(address) not in aliases:
        return FromMe(None, False)
    if _gmail_aligned(auth_results, domain):
        return FromMe("alias", False)
    return FromMe(None, True)


def recompute_from_me(
    conn: sqlite3.Connection,
    aliases: frozenset[str],
    *,
    apply: bool,
) -> FromMeReport:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
    if "from_me_origin" not in columns:
        return _sent_only(conn, apply)
    rows = conn.execute(
        f"""
        SELECT messages.msg_pk, messages.from_hdr, messages.auth_results,
               messages.is_from_me, messages.from_me_origin,
               {_HAS_SENT} AS has_sent
        FROM messages
        """,
        (_SENT,),
    ).fetchall()
    sent = alias = untrusted = changed = unchanged = 0
    updates: list[tuple[int, str | None, int]] = []
    for row in rows:
        verdict = classify_from_me(
            has_sent=bool(row[5]),
            from_header=None if row[1] is None else str(row[1]),
            auth_results=None if row[2] is None else str(row[2]),
            aliases=aliases,
        )
        if verdict.origin == "sent":
            sent += 1
        elif verdict.origin == "alias":
            alias += 1
        if verdict.untrusted:
            untrusted += 1
        stored_origin = None if row[4] is None else str(row[4])
        same = int(row[3] or 0) == int(verdict.mine) and stored_origin == verdict.origin
        if same:
            unchanged += 1
            continue
        changed += 1
        updates.append((int(verdict.mine), verdict.origin, int(row[0])))
    if apply and updates:
        own = not conn.in_transaction
        if own:
            conn.execute("BEGIN")
        try:
            conn.executemany(
                "UPDATE messages SET is_from_me = ?, from_me_origin = ? WHERE msg_pk = ?",
                updates,
            )
            if own:
                conn.commit()
        except Exception:
            if own:
                conn.rollback()
            raise
    return FromMeReport(sent, alias, untrusted, changed, unchanged, apply)


def _sent_only(conn: sqlite3.Connection, apply: bool) -> FromMeReport:
    if not apply:
        return FromMeReport(0, 0, 0, 0, 0, False)
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "is_from_me" not in {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}:
        return FromMeReport(0, 0, 0, 0, 0, True)
    if "message_labels" not in tables or "labels" not in tables:
        return FromMeReport(0, 0, 0, 0, 0, True)
    conn.execute(
        f"""
        UPDATE messages
        SET is_from_me = CASE WHEN {_HAS_SENT} THEN 1 ELSE 0 END
        """,
        (_SENT,),
    )
    return FromMeReport(0, 0, 0, 0, 0, True)


def _gmail_aligned(auth_results: str | None, domain: str) -> bool:
    """Trust the first Authentication-Results block only.

    Gmail prepends its own result. A later block can repeat ``mx.google.com``
    and still be the sender's header. DMARC and DKIM prove that domain, not
    one mailbox on it.
    """
    if not auth_results:
        return False
    wanted = _strip_one_dot(_fold(domain))
    if not wanted:
        return False
    blocks = _blocks(auth_results)
    if not blocks:
        return False
    # An encoded word is not a Gmail result. Decoding it can forge a clause.
    if "=?" in blocks[0]:
        return False
    clauses = _scan(blocks[0])
    if clauses is None or not clauses or clauses[0] != [(_AUTHSERV, "")]:
        return False
    dmarc = [
        value
        for clause in clauses[1:]
        for key, value in clause
        if key == "dmarc"
    ]
    if len(dmarc) != 1 or dmarc[0] == "fail":
        return False
    for clause in clauses[1:]:
        if not clause or clause[0] != ("dkim", "pass"):
            continue
        for key, value in clause[1:]:
            if key not in ("header.d", "header.i"):
                continue
            if _domain_of(value) == wanted:
                return True
            break
    return False


def _blocks(text: str) -> list[str]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines() or [text]:
        if current and line[:1] not in ("", " ", "\t"):
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return ["\n".join(part) for part in blocks if "".join(part).strip()]


def _scan(text: str) -> list[list[tuple[str, str]]] | None:
    """Split one block into key=value pairs.

    ``None`` means an unbalanced comment, quote, or trailing escape.
    A quoted string hides parentheses and semicolons, including inside a
    comment. Every method result is kept. The caller uses the first
    property value for a key.
    """
    clauses: list[list[str]] = [[]]
    token: list[str] = []
    depth = 0
    quote = False
    escape = False

    def finish_token() -> None:
        nonlocal token
        if token:
            clauses[-1].append("".join(token))
            token = []

    for char in text:
        if escape:
            escape = False
            if depth == 0:
                token.append(char)
            continue
        if char == "\\":
            escape = True
            continue
        if quote:
            if char == '"':
                quote = False
            elif depth == 0:
                token.append(char)
            continue
        if depth:
            if char == '"':
                quote = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            continue
        if char == '"':
            quote = True
            continue
        if char == "(":
            depth = 1
            continue
        if char == ")":
            return None
        if char == ";":
            finish_token()
            if clauses[-1]:
                clauses.append([])
            continue
        if char.isspace():
            finish_token()
            continue
        token.append(char)
    if escape or quote or depth:
        return None
    finish_token()
    if clauses and not clauses[-1]:
        clauses.pop()
    parsed: list[list[tuple[str, str]]] = []
    for clause in clauses:
        pairs: list[tuple[str, str]] = []
        for item in clause:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            pairs.append((_fold(key), _fold(value)))
        if clause and "=" not in clause[0]:
            if len(clause) != 1:
                return None
            parsed.append([(_fold(clause[0]), "")])
            continue
        if pairs:
            parsed.append(pairs)
    return parsed


def _domain_of(value: str) -> str:
    token = _strip_one_dot(_fold(value.strip())).lstrip("@")
    if "@" in token:
        token = _strip_one_dot(token.rsplit("@", 1)[1])
    return token


def _strip_one_dot(value: str) -> str:
    if value.endswith("."):
        return value[:-1]
    return value


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()
