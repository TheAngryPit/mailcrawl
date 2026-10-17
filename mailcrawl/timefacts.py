"""Per-thread dates from stored mail. The text scan is bounded and has no eval."""

from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass

from mailcrawl.clock import AS_OF_MAX, AS_OF_MIN, utc_parts
from mailcrawl.decisions import DecisionDraft
from mailcrawl.textnorm import parse_address
from mailcrawl.time_cues import CUES, MONTHS

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
_FLAGS = re.ASCII | re.IGNORECASE
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b", _FLAGS)
_ATE = re.compile(
    r"\bat[eé] dia (\d{1,2})(?: de ([a-zç]{3,12}))?(?: de (\d{4}))?\b",
    _FLAGS,
)
if "marco" not in MONTHS:
    MONTHS["marco"] = MONTHS.get("março", 3)
_MONTH_ALT = "|".join(re.escape(name) for name in sorted(MONTHS, key=len, reverse=True))
_LATIN_FLAGS = re.IGNORECASE | re.ASCII
_DMY = re.compile(
    rf"(?<![0-9])([0-9]{{1,2}})\s+({_MONTH_ALT})\s+([0-9]{{4}})(?![0-9])",
    _LATIN_FLAGS,
)
_MDY = re.compile(
    rf"(?<![0-9])({_MONTH_ALT})\s+([0-9]{{1,2}}),?\s+([0-9]{{4}})(?![0-9])",
    _LATIN_FLAGS,
)
_DE_MONTH = re.compile(
    rf"(?<![0-9])([0-9]{{1,2}})\s+de\s+({_MONTH_ALT})\s+de\s+([0-9]{{4}})(?![0-9])",
    _LATIN_FLAGS,
)
_DOTTED = re.compile(
    rf"(?<![0-9])([0-9]{{1,2}})\.\s*({_MONTH_ALT})\s+([0-9]{{4}})(?![0-9])",
    _LATIN_FLAGS,
)
_YMD_HAN = re.compile(r"(?<![0-9])([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日", re.ASCII)
_YMD_KO = re.compile(r"(?<![0-9])([0-9]{4})년\s*([0-9]{1,2})월\s*([0-9]{1,2})일", re.ASCII)


def _cue_pattern(kind: str, *, cjk: bool, latin: bool) -> re.Pattern[str]:
    words = [word for word, cue in CUES.items() if cue == kind and _is_cjk(word) == cjk and word.isascii() == latin]
    if not words:
        return re.compile(r"(?!)")
    alt = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    if cjk:
        return re.compile(alt)
    if latin:
        return re.compile(rf"(?<![A-Za-z0-9_])(?:{alt})(?![A-Za-z0-9_])", re.IGNORECASE | re.ASCII)
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE)


def _is_cjk(text: str) -> bool:
    return any("\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff" or "\uac00" <= char <= "\ud7af" for char in text)


_DEADLINE_LATIN = _cue_pattern("deadline", cjk=False, latin=True)
_DEADLINE_WORD = _cue_pattern("deadline", cjk=False, latin=False)
_DEADLINE_CJK = _cue_pattern("deadline", cjk=True, latin=False)
_MEETING_LATIN = _cue_pattern("meeting", cjk=False, latin=True)
_MEETING_WORD = _cue_pattern("meeting", cjk=False, latin=False)
_MEETING_CJK = _cue_pattern("meeting", cjk=True, latin=False)


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
    date_trusted: bool

    def public(self) -> dict[str, object]:
        return {
            "age_days": self.age_days,
            "date_trusted": self.date_trusted,
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
) -> TimeFacts:
    rows = conn.execute(
        """
        SELECT internal_date, from_hdr, is_from_me, date_trusted, subject, body_text
        FROM messages
        WHERE thread_pk = ?
        ORDER BY internal_date, msg_pk
        """,
        (thread_pk,),
    ).fetchall()
    stamps = [int(row["internal_date"]) for row in rows if row["internal_date"] is not None]
    first_at = min(stamps) if stamps else None
    last_row = None
    for row in rows:
        if row["internal_date"] is None:
            continue
        if last_row is None or int(row["internal_date"]) >= int(last_row["internal_date"]):
            last_row = row
    last_at = None if last_row is None else int(last_row["internal_date"])
    date_trusted = bool(
        last_row is not None and int(last_row["date_trusted"] or 0) == 1 and last_at not in (None, 0)
    )
    if last_at is None:
        age = None
    elif last_at > moment:
        age = 0
    else:
        age = (moment - last_at) // 86400
    last_human = _last_human(rows)
    found: list[FoundDate] = []
    used = 0
    for row in rows:
        if used >= TEXT_CAP or len(found) >= _MAX_DATES:
            break
        piece = _piece(row)
        room = TEXT_CAP - used
        sample = piece[:room]
        used += len(sample)
        year_hint, month_hint, day_hint = _hints(row["internal_date"], moment)
        found.extend(_dates(sample, year_hint, month_hint, day_hint))
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
        date_trusted=date_trusted,
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
        if _untrusted_age(facts):
            state = "needs_review"
            reasons.append("untrusted date")
        else:
            state = "resolved"
            lane = "paper_trail"
            reasons.append("waiting is stale")
    if (facts.past_deadline or facts.past_meeting) and state in ("reply", "action"):
        state = "needs_review"
        reasons.append("past date")
    if (
        facts.age_days is not None
        and facts.age_days > chosen.old_days
        and lane == "imbox"
        and state in ("reply", "action")
    ):
        if _untrusted_age(facts):
            state = "needs_review"
            reasons.append("untrusted date")
        else:
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


def _last_human(rows) -> str:
    for row in reversed(rows):
        header = "" if row["from_hdr"] is None else str(row["from_hdr"])
        _name, address, domain = parse_address(header)
        if _automated(address, domain):
            continue
        if int(row["is_from_me"] or 0) == 1:
            return "owner"
        return "other"
    return "none"


def _untrusted_age(facts: TimeFacts) -> bool:
    return (not facts.date_trusted) or facts.last_at == 0


def _automated(address: str | None, domain: str | None) -> bool:
    if not address:
        return False
    local = address.split("@", 1)[0].casefold()
    host = (domain or "").casefold()
    labels = host.split(".") if host else []
    for token in _AUTOMATED:
        needle = token[:-1] if token.endswith("@") else token
        needle = needle.casefold()
        if local == needle or host == needle or needle in labels:
            return True
    return False


def _piece(row) -> str:
    subject = "" if row["subject"] is None else str(row["subject"])
    body = "" if row["body_text"] is None else str(row["body_text"])
    return f"{subject}\n{body}".lower()


def _hints(stamp, moment: int) -> tuple[int, int, int]:
    parts = None if stamp is None else _parts(stamp)
    if parts is None:
        parts = _parts(moment)
    if parts is None:
        return (2000, 1, 1)
    return parts


def _parts(stamp) -> tuple[int, int, int] | None:
    try:
        number = int(stamp)
    except (TypeError, ValueError):
        return None
    if number < AS_OF_MIN or number > AS_OF_MAX:
        return None
    year, month, day = utc_parts(number)
    return int(year), int(month), int(day)


def _before(found: int, moment: int) -> bool:
    return found // 86400 < moment // 86400


def _dates(sample: str, year_hint: int, month_hint: int, day_hint: int) -> list[FoundDate]:
    found: list[FoundDate] = []

    def add(kind: str, year: int, month: int, day: int) -> None:
        if len(found) >= _MAX_DATES:
            return
        stamp = _stamp(year, month, day)
        if stamp is None:
            return
        found.append(FoundDate(kind, stamp))

    spans: list[tuple[int, int]] = []

    def take(kind: str, year: int, month: int, day: int, start: int, end: int) -> None:
        if any(start < right and end > left for left, right in spans):
            return
        before = len(found)
        add(kind, year, month, day)
        if len(found) != before:
            spans.append((start, end))

    for match in _ISO.finditer(sample):
        take(_kind(sample, match.start()), int(match.group(1)), int(match.group(2)), int(match.group(3)), match.start(), match.end())
    for pattern, day_group, month_group, year_group in (
        (_DE_MONTH, 1, 2, 3),
        (_DOTTED, 1, 2, 3),
        (_DMY, 1, 2, 3),
        (_MDY, 2, 1, 3),
    ):
        for match in pattern.finditer(sample):
            month = _month_number(match.group(month_group))
            if month is None:
                continue
            take(
                _kind(sample, match.start()),
                int(match.group(year_group)),
                month,
                int(match.group(day_group)),
                match.start(),
                match.end(),
            )
    for match in _YMD_HAN.finditer(sample):
        take(_kind(sample, match.start()), int(match.group(1)), int(match.group(2)), int(match.group(3)), match.start(), match.end())
    for match in _YMD_KO.finditer(sample):
        take(_kind(sample, match.start()), int(match.group(1)), int(match.group(2)), int(match.group(3)), match.start(), match.end())
    for match in _ATE.finditer(sample):
        month_name = match.group(2)
        year_text = match.group(3)
        year, month, day = _ate_day(
            int(match.group(1)),
            year_hint,
            month_hint,
            day_hint,
            month_name,
            year_text,
        )
        take("deadline", year, month, day, match.start(), match.end())
    return found


def _ate_day(
    day: int,
    year_hint: int,
    month_hint: int,
    day_hint: int,
    month_name: str | None,
    year_text: str | None,
) -> tuple[int, int, int]:
    if month_name is None and year_text is None and day < day_hint:
        month = month_hint + 1
        year = year_hint
        if month > 12:
            return year + 1, 1, day
        return year, month, day
    month = month_hint if month_name is None else (_month_number(month_name) or 0)
    year = year_hint if year_text is None else int(year_text)
    return year, month, day


def _month_number(name: str) -> int | None:
    return MONTHS.get(" ".join(name.casefold().split()))


def _kind(sample: str, start: int) -> str:
    window = sample[max(0, start - _CUE) : start]
    if _DEADLINE_LATIN.search(window) or _DEADLINE_WORD.search(window) or _DEADLINE_CJK.search(window):
        return "deadline"
    if _MEETING_LATIN.search(window) or _MEETING_WORD.search(window) or _MEETING_CJK.search(window):
        return "meeting"
    return "date"


def _stamp(year: int, month: int, day: int) -> int | None:
    if year < _YEAR_MIN or year > _YEAR_MAX or month < 1 or month > 12:
        return None
    if day < 1 or day > calendar.monthrange(year, month)[1]:
        return None
    return calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0))
