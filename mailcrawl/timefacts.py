"""Per-thread dates from stored mail. The text scan is bounded and has no eval."""

from __future__ import annotations

import calendar
import re
import sqlite3
import time
from dataclasses import dataclass

from mailcrawl.decisions import DecisionDraft
from mailcrawl.textnorm import parse_address

TEXT_CAP = 8000
_MAX_DATES = 8
_CUE = 40
STALE_DAYS = 90
OLD_DAYS = 90
_YEAR_MIN = 1990
_YEAR_MAX = 2100
_AUTOMATED = (
    "mailsuite",
    "mailtrack",
    "mixmax",
    "noreply",
    "no-reply",
    "mailer-daemon",
    "notifications@",
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "março": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(
    r"\b(\d{1,2}) (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]{0,6} (\d{4})\b",
    re.IGNORECASE,
)
_MDY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]{0,6} (\d{1,2}),? (\d{4})\b",
    re.IGNORECASE,
)
_PT = re.compile(
    r"\b(\d{1,2}) de (janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro) de (\d{4})\b",
    re.IGNORECASE,
)
_ATE = re.compile(
    r"\bat[eé] dia (\d{1,2})(?: de ([a-zç]{3,12}))?(?: de (\d{4}))?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FoundDate:
    kind: str
    at: int


@dataclass(frozen=True)
class TimeFacts:
    as_of: int
    first_at: int | None
    last_at: int | None
    age_days: int | None
    last_human: str
    found: tuple[FoundDate, ...]
    past_deadline: bool
    past_meeting: bool

    def public(self) -> dict[str, object]:
        return {
            "age_days": self.age_days,
            "dates": [{"at": item.at, "kind": item.kind} for item in self.found],
            "first_at": self.first_at,
            "last_at": self.last_at,
            "last_human": self.last_human,
            "past_deadline": self.past_deadline,
            "past_meeting": self.past_meeting,
        }


@dataclass(frozen=True)
class TimePolicy:
    stale_days: int = STALE_DAYS
    old_days: int = OLD_DAYS


def thread_time_facts(
    conn: sqlite3.Connection,
    thread_pk: int,
    moment: int,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
) -> TimeFacts:
    rows = conn.execute(
        """
        SELECT internal_date, from_hdr, is_from_me, subject, body_text
        FROM messages
        WHERE thread_pk = ?
        ORDER BY internal_date, msg_pk
        """,
        (thread_pk,),
    ).fetchall()
    stamps = [int(row["internal_date"]) for row in rows if row["internal_date"] is not None]
    first_at = min(stamps) if stamps else None
    last_at = max(stamps) if stamps else None
    age = None if last_at is None else (moment - last_at) // 86400
    last_human = _last_human(rows, owners, domains)
    found: list[FoundDate] = []
    used = 0
    for row in rows:
        if used >= TEXT_CAP or len(found) >= _MAX_DATES:
            break
        piece = _piece(row)
        room = TEXT_CAP - used
        sample = piece[:room]
        used += len(sample)
        year_hint = _year(row["internal_date"], moment)
        month_hint = _month(row["internal_date"], moment)
        found.extend(_dates(sample, year_hint, month_hint))
    kept = tuple(found[:_MAX_DATES])
    return TimeFacts(
        as_of=moment,
        first_at=first_at,
        last_at=last_at,
        age_days=age,
        last_human=last_human,
        found=kept,
        past_deadline=any(item.kind == "deadline" and _before(item.at, moment) for item in kept),
        past_meeting=any(item.kind == "meeting" and _before(item.at, moment) for item in kept),
    )


def apply_time(draft: DecisionDraft, facts: TimeFacts, policy: TimePolicy | None = None) -> DecisionDraft:
    chosen = policy or TimePolicy()
    state = draft.next_state
    lane = draft.lane
    review_date = draft.review_date
    reasons: list[str] = []
    if facts.last_human == "owner" and state in ("reply", "action"):
        state = "waiting"
        if review_date is None:
            review_date = facts.as_of
        reasons.append("owner sent last")
    if state == "waiting" and facts.age_days is not None and facts.age_days > chosen.stale_days:
        state = "resolved"
        lane = "paper_trail"
        reasons.append("waiting is stale")
    if facts.past_deadline or facts.past_meeting:
        if lane == "imbox" or state in ("reply", "action", "needs_review"):
            state = "reference"
            lane = "paper_trail"
            reasons.append("past date")
    if (
        facts.age_days is not None
        and facts.age_days > chosen.old_days
        and lane == "imbox"
        and state in ("reply", "action")
    ):
        state = "reference"
        lane = "paper_trail"
        reasons.append("last activity is old")
    if not reasons:
        return draft
    note = {"time": facts.public(), "time_reason": reasons}
    return DecisionDraft(
        next_state=state,
        lane=lane,
        rule_id=draft.rule_id,
        rules_version=draft.rules_version,
        origin=draft.origin,
        source_detail=draft.source_detail,
        evidence=draft.evidence + (note,),
        priority=draft.priority,
        review_date=review_date,
        muted=draft.muted,
    )


def _last_human(rows, owners: frozenset[str], domains: frozenset[str]) -> str:
    for row in reversed(rows):
        header = "" if row["from_hdr"] is None else str(row["from_hdr"])
        _name, address, domain = parse_address(header)
        if _automated(header, address):
            continue
        if int(row["is_from_me"] or 0) == 1 or _owner(address, domain, owners, domains):
            return "owner"
        return "other"
    return "none"


def _owner(address: str | None, domain: str | None, owners: frozenset[str], domains: frozenset[str]) -> bool:
    if address is not None and address in owners:
        return True
    return domain is not None and domain in domains


def _automated(header: str, address: str | None) -> bool:
    haystack = header.lower()
    if address:
        haystack = f"{haystack} {address}"
    return any(token in haystack for token in _AUTOMATED)


def _piece(row) -> str:
    subject = "" if row["subject"] is None else str(row["subject"])
    body = "" if row["body_text"] is None else str(row["body_text"])
    return f"{subject}\n{body}".lower()


def _year(stamp, moment: int) -> int:
    source = moment if stamp is None else int(stamp)
    return _parts(source)[0]


def _month(stamp, moment: int) -> int:
    source = moment if stamp is None else int(stamp)
    return _parts(source)[1]


def _parts(stamp: int) -> tuple[int, int, int]:
    year, month, day = time.gmtime(int(stamp))[:3]
    return int(year), int(month), int(day)


def _before(found: int, moment: int) -> bool:
    return found // 86400 < moment // 86400


def _dates(sample: str, year_hint: int, month_hint: int) -> list[FoundDate]:
    found: list[FoundDate] = []

    def add(kind: str, year: int, month: int, day: int) -> None:
        if len(found) >= _MAX_DATES:
            return
        stamp = _stamp(year, month, day)
        if stamp is None:
            return
        found.append(FoundDate(kind, stamp))

    for match in _ISO.finditer(sample):
        add(_kind(sample, match.start()), int(match.group(1)), int(match.group(2)), int(match.group(3)))
    for match in _DMY.finditer(sample):
        add(
            _kind(sample, match.start()),
            int(match.group(3)),
            _MONTHS[match.group(2).lower()[:3]],
            int(match.group(1)),
        )
    for match in _MDY.finditer(sample):
        add(
            _kind(sample, match.start()),
            int(match.group(3)),
            _MONTHS[match.group(1).lower()[:3]],
            int(match.group(2)),
        )
    for match in _PT.finditer(sample):
        month_name = match.group(2).lower()
        add(_kind(sample, match.start()), int(match.group(3)), _MONTHS[month_name], int(match.group(1)))
    for match in _ATE.finditer(sample):
        month_name = match.group(2)
        year_text = match.group(3)
        month = month_hint if month_name is None else _MONTHS.get(month_name.lower(), 0)
        year = year_hint if year_text is None else int(year_text)
        add("deadline", year, month, int(match.group(1)))
    return found


def _kind(sample: str, start: int) -> str:
    window = sample[max(0, start - _CUE) : start]
    if "deadline" in window or "prazo" in window or "due " in window:
        return "deadline"
    if "meet" in window or "reuni" in window or "call" in window:
        return "meeting"
    return "date"


def _stamp(year: int, month: int, day: int) -> int | None:
    if year < _YEAR_MIN or year > _YEAR_MAX or month < 1 or month > 12:
        return None
    if day < 1 or day > calendar.monthrange(year, month)[1]:
        return None
    return calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0))
