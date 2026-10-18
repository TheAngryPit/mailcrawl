"""Deterministic rules 0–8. No model. First match wins."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.paths import UsageError
from mailcrawl.taxonomy import CONTENT, ORIGIN, effective_keys
from mailcrawl.textnorm import parse_address

RULES_VERSION = "rules-v4"
_FILE_COUNT = re.compile(
    r"(?<!\w)files \(\d+\) (?:from|to)\b|(?<!\w)arquivos \(\d+\) (?:para|de)\b",
    re.IGNORECASE,
)
_SPECIFIC_CONTENT = (
    "newsletter",
    "job_alert",
    "notification",
    "invoice_receipt",
    "purchase_delivery",
    "account_security",
    "contract",
)
_DAY = 86400
_KEY_ROLES = CONTENT + ORIGIN


@dataclass(frozen=True)
class RulesConfig:
    rules_version: str
    read_later_expiry_days: int
    review_days: int
    unrecognized_days: int
    keys: dict[str, str]
    question: tuple[str, ...]
    task: tuple[str, ...]
    closing: tuple[str, ...]
    machine_local: tuple[str, ...]
    transfer_notice: tuple[str, ...]
    receipt_words: tuple[str, ...]
    calendar_response: tuple[str, ...]
    automated_reference: tuple[str, ...]
    billing_action: tuple[str, ...]
    sales: tuple[str, ...]
    security_wording: tuple[str, ...]
    auto_reply_subject: tuple[str, ...]
    read_tracking: tuple[str, ...]
    transfer_local: tuple[str, ...]
    stale_meeting: tuple[str, ...]
    security_notice: tuple[str, ...]
    file_handoff: tuple[str, ...]
    access_request: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    threads: int
    covered: int
    by_state: dict[str, int]
    by_rule: dict[str, int]


@dataclass(frozen=True)
class _Outcome:
    next_state: str
    rule_id: str
    lane: str
    priority: int | None
    review_date: int | None
    muted: int
    evidence: tuple[dict[str, object], ...]


def load_rules(path: Path) -> RulesConfig:
    document = json.loads(path.read_text(encoding="utf-8"))
    version = document.get("rules_version")
    if version != RULES_VERSION:
        raise ValueError(f"rules_version {version}")
    keys = document.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("rules file needs keys")
    mapped = {role: str(keys[role]) for role in _KEY_ROLES}
    return RulesConfig(
        rules_version=version,
        read_later_expiry_days=int(document["read_later_expiry_days"]),
        review_days=int(document["review_days"]),
        unrecognized_days=int(document["unrecognized_days"]),
        keys=mapped,
        question=_merged_phrases(document, "question"),
        task=_merged_phrases(document, "task"),
        closing=_merged_phrases(document, "closing"),
        machine_local=_merged_phrases(document, "machine_local"),
        transfer_notice=_merged_phrases(document, "transfer_notice"),
        receipt_words=_merged_phrases(document, "receipt_words"),
        calendar_response=_merged_phrases(document, "calendar_response"),
        automated_reference=_merged_phrases(document, "automated_reference", also=("out_of_office",)),
        billing_action=_merged_phrases(document, "billing_action"),
        sales=_merged_phrases(document, "sales"),
        security_wording=_merged_phrases(document, "security_wording"),
        auto_reply_subject=_merged_phrases(document, "auto_reply_subject"),
        read_tracking=_merged_phrases(document, "read_tracking"),
        transfer_local=_merged_phrases(document, "transfer_local"),
        stale_meeting=_merged_phrases(document, "stale_meeting"),
        security_notice=_merged_phrases(document, "security_notice"),
        file_handoff=_merged_phrases(document, "file_handoff"),
        access_request=_merged_phrases(document, "access_request"),
    )


def bundled_rules_path() -> Path:
    resource = files("mailcrawl").joinpath("rules-v0.json")
    path = Path(str(resource))
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


@dataclass(frozen=True)
class Preview:
    thread_pk: int
    next_state: str
    rule_id: str
    lane: str
    time_reason: str = ""


def _time_reason(evidence: tuple[dict[str, object], ...]) -> str:
    parts: list[str] = []
    for item in evidence:
        reasons = item.get("time_reason")
        if isinstance(reasons, list):
            parts.extend(str(part) for part in reasons)
    return "|".join(parts)


def preview_rules(
    conn: sqlite3.Connection,
    *,
    now: int,
    config: RulesConfig,
    label_keys: dict[str, tuple[str, ...]] | None = None,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
    test_recipients: frozenset[str] = frozenset(),
    policy=None,
    review: bool = False,
) -> list[Preview]:
    found: list[Preview] = []
    threads = conn.execute("SELECT thread_pk FROM threads ORDER BY thread_pk").fetchall()
    for thread in threads:
        thread_pk = int(thread[0])
        facts = _load(
            conn,
            thread_pk,
            now,
            config,
            label_keys,
            owners,
            domains,
            security_senders,
        )
        outcome = _with_time(
            conn,
            thread_pk,
            _classify(facts, now, config, test_recipients),
            now,
            config.rules_version,
            policy,
            review,
        )
        found.append(
            Preview(thread_pk, outcome.next_state, outcome.rule_id, outcome.lane, _time_reason(outcome.evidence))
        )
    return found


def apply_rules(
    conn: sqlite3.Connection,
    *,
    now: int,
    config: RulesConfig,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
    test_recipients: frozenset[str] = frozenset(),
    policy=None,
    review: bool = False,
) -> Coverage:
    threads = conn.execute("SELECT thread_pk FROM threads ORDER BY thread_pk").fetchall()
    for thread in threads:
        thread_pk = int(thread[0])
        _apply_thread(
            conn,
            thread_pk,
            now,
            config,
            owners,
            domains,
            security_senders,
            test_recipients,
            policy,
            review,
        )
    conn.commit()
    return coverage_counts(conn)


def coverage_counts(conn: sqlite3.Connection) -> Coverage:
    threads = int(conn.execute("SELECT count(*) FROM threads").fetchone()[0])
    rows = conn.execute(
        """
        SELECT next_state, rule_id FROM decisions
        WHERE superseded_by IS NULL
        """
    ).fetchall()
    by_state: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for state, rule_id in rows:
        by_state[str(state)] = by_state.get(str(state), 0) + 1
        by_rule[str(rule_id)] = by_rule.get(str(rule_id), 0) + 1
    return Coverage(threads, len(rows), by_state, by_rule)


def _phrases(value: object) -> tuple[str, ...]:
    return tuple(_phrase_items(value))


def _phrase_items(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("rules file needs a phrase list")
    phrases: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("rules phrase must be text")
        phrases.append(item.strip().lower())
    return phrases


def _merged_phrases(document: dict[str, object], name: str, *, also: tuple[str, ...] = ()) -> tuple[str, ...]:
    locales = document.get("phrases")
    if not isinstance(locales, dict) or "en" not in locales or "pt" not in locales:
        raise ValueError("rules file needs en and pt phrase lists")
    found: list[str] = []
    for phrase_name in (name, *also):
        top = document.get(phrase_name)
        if isinstance(top, list):
            found.extend(_phrase_items(top))
        for locale in sorted(locales):
            block = locales[locale]
            if not isinstance(block, dict):
                raise ValueError("phrase locale must be an object")
            extra = block.get(phrase_name)
            if extra is None:
                continue
            found.extend(_phrase_items(extra))
    if not found:
        raise ValueError(f"rules file needs {name}")
    unique: list[str] = []
    for phrase in found:
        if phrase not in unique:
            unique.append(phrase)
    return tuple(unique)


def _apply_thread(
    conn: sqlite3.Connection,
    thread_pk: int,
    now: int,
    config: RulesConfig,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
    test_recipients: frozenset[str] = frozenset(),
    policy=None,
    review: bool = False,
) -> None:
    facts = _load(
        conn,
        thread_pk,
        now,
        config,
        None,
        owners,
        domains,
        security_senders,
    )
    current = conn.execute(
        """
        SELECT decision_id, thread_pk, next_state, rule_id, lane, priority,
               review_date, muted, created_at, origin
        FROM decisions
        WHERE thread_pk = ? AND superseded_by IS NULL
        """,
        (thread_pk,),
    ).fetchone()
    if current is not None and str(current["origin"]) == "human":
        return
    if current is not None:
        outcome = _transition(current, facts, now, config)
        if outcome is None:
            return
        _supersede(
            conn,
            current,
            _with_time(conn, thread_pk, outcome, now, config.rules_version, policy, review),
            now,
            config.rules_version,
        )
        return
    outcome = _with_time(
        conn,
        thread_pk,
        _classify(facts, now, config, test_recipients),
        now,
        config.rules_version,
        policy,
        review,
    )
    _insert_decision(conn, thread_pk, outcome, now, config.rules_version, None)


def _transition(current: sqlite3.Row, facts: "_Facts", now: int, config: RulesConfig) -> _Outcome | None:
    state = str(current["next_state"])
    created = int(current["created_at"])
    if state == "waiting" and _reply_after(facts, created):
        return _outcome(
            "reply",
            "t-reply",
            "imbox",
            facts.priority if _priority_ok(facts.priority) else None,
            None,
            0,
            ({"type": "rule", "name": "t-reply"},),
        )
    review = current["review_date"]
    if state == "waiting" and review is not None and now >= int(review):
        return _outcome(
            "needs_review",
            "t-overdue",
            "imbox",
            None,
            None,
            0,
            ({"type": "rule", "name": "t-overdue"},),
        )
    expiry = created + config.read_later_expiry_days * _DAY
    if state == "read_later" and now >= expiry:
        if facts.protected:
            return _outcome(
                "needs_review",
                "t-expiry",
                "imbox",
                None,
                None,
                0,
                ({"type": "rule", "name": "t-expiry"}, {"type": "guard", "name": "protected"}),
            )
        return _outcome(
            "noise",
            "t-expiry",
            "feed",
            None,
            None,
            0,
            ({"type": "rule", "name": "t-expiry"},),
        )
    return None


def _classify(
    facts: "_Facts",
    now: int,
    config: RulesConfig,
    test_recipients: frozenset[str] = frozenset(),
) -> _Outcome:
    if facts.starred:
        return _outcome(
            "needs_review",
            "0",
            "imbox",
            None,
            None,
            0,
            _labels(facts.star_labels) + ({"type": "rule", "name": "0"},),
        )
    protected = facts.protected
    if facts.muted or facts.denied:
        if not protected:
            return _outcome(
                "noise",
                "1",
                "screened_out",
                None,
                None,
                1 if facts.muted else 0,
                ({"type": "rule", "name": "1"},),
            )
    if facts.on_read_list and facts.has_key("newsletter"):
        return _outcome(
            "read_later",
            "2-read",
            "feed",
            None,
            now + config.read_later_expiry_days * _DAY,
            0,
            _labels(facts.keys.get("newsletter", ())) + ({"type": "rule", "name": "2-read"},),
        )
    if _structural_noise(facts) and not protected:
        return _outcome(
            "noise",
            "2",
            "feed",
            None,
            None,
            0,
            ({"type": "rule", "name": "2"},),
        )
    if facts.newsletter_sender and not protected:
        return _outcome("noise", "2", "feed", None, None, 0, ({"type": "rule", "name": "2"},))
    if facts.promotions and not facts.specific_content() and not protected:
        return _outcome("noise", "2", "feed", None, None, 0, ({"type": "rule", "name": "2"},))
    if _any_phrase(_text(facts), config.billing_action):
        return _outcome("action", "7", "imbox", None, None, 0, ({"type": "rule", "name": "7"},))
    if _test_send(facts, test_recipients) and not protected:
        return _outcome(
            "resolved",
            "test-send",
            "screened_out",
            None,
            None,
            0,
            ({"type": "rule", "name": "test-send"},),
        )
    if (
        not facts.draft_only
        and facts.last_from_me
        and _question(facts.body, config)
        and not _housekeeping(facts, config)
    ):
        return _outcome(
            "waiting",
            "6",
            "imbox",
            None,
            now + config.review_days * _DAY,
            0,
            ({"type": "rule", "name": "6"},),
        )
    if _receipt(facts, config):
        role = "invoice_receipt" if facts.has_key("invoice_receipt") else "purchase_delivery"
        evidence = _labels(facts.keys.get(role, ())) + ({"type": "rule", "name": "3"},)
        return _outcome("reference", "3", "paper_trail", None, None, 0, evidence)
    if (
        facts.real_security
        or facts.security_sender
        or _tab_security(facts, config)
        or _security_notice(facts, config)
    ):
        fresh = facts.last_at is not None and (now - facts.last_at) < config.unrecognized_days * _DAY
        evidence = _labels(facts.keys.get("account_security", ())) + ({"type": "rule", "name": "4"},)
        if facts.sender_known is False and fresh:
            return _outcome("needs_review", "4", "imbox", None, None, 0, evidence)
        if facts.sender_known is False:
            evidence = evidence + ({"time_reason": ["unrecognized sender is not recent"]},)
        return _outcome("reference", "4", "paper_trail", None, None, 0, evidence)
    if facts.bin_labels:
        return _outcome(
            "resolved",
            "spam-trash",
            "screened_out",
            None,
            None,
            0,
            _labels(facts.bin_labels) + ({"type": "rule", "name": "spam-trash"},),
        )
    if facts.draft_only:
        return _outcome("resolved", "draft", "screened_out", None, None, 0, ({"type": "rule", "name": "draft"},))
    if _housekeeping(facts, config):
        return _outcome(
            "resolved",
            "housekeeping",
            "screened_out",
            None,
            None,
            0,
            ({"type": "rule", "name": "housekeeping"},),
        )
    if _auto_reply(facts, config) and not protected:
        return _outcome(
            "noise",
            "auto-reply",
            "feed",
            None,
            None,
            0,
            ({"type": "rule", "name": "auto-reply"},),
        )
    if _access_request(facts, config):
        return _outcome("action", "7", "imbox", None, None, 0, ({"type": "rule", "name": "7"},))
    if _automated_reference(facts, config):
        return _outcome(
            "reference",
            "auto-ref",
            "paper_trail",
            None,
            None,
            0,
            ({"type": "rule", "name": "auto-ref"},),
        )
    if facts.updates and not facts.specific_content():
        return _outcome(
            "reference",
            "auto-ref",
            "paper_trail",
            None,
            None,
            0,
            ({"type": "rule", "name": "auto-ref"},),
        )
    if _handoff(facts, config) and not protected:
        return _outcome(
            "resolved",
            "handoff",
            "screened_out",
            None,
            None,
            0,
            ({"type": "rule", "name": "handoff"},),
        )
    if (
        facts.last_from_me
        and _short_closing(facts.body, config)
        and not protected
        and not _inbox_noise(facts)
    ):
        return _outcome("resolved", "8", "imbox", None, None, 0, ({"type": "rule", "name": "8"},))
    if (
        facts.human
        and not facts.last_from_me
        and not facts.replied
        and _any_phrase(facts.body, config.sales)
    ):
        return _outcome("screener", "screener", "imbox", None, None, 0, ({"type": "rule", "name": "screener"},))
    if facts.human and not facts.last_from_me:
        priority = facts.priority if _priority_ok(facts.priority) else None
        evidence: tuple[dict[str, object], ...] = ({"type": "rule", "name": "5"},)
        if facts.sender_id is not None:
            evidence = ({"type": "sender", "id": facts.sender_id},) + evidence
        return _outcome("reply", "5", "imbox", priority, None, 0, evidence)
    if facts.last_from_me and (_question(facts.body, config) or _task(facts.body, config)):
        return _outcome(
            "waiting",
            "6",
            "imbox",
            None,
            now + config.review_days * _DAY,
            0,
            ({"type": "rule", "name": "6"},),
        )
    if not facts.last_from_me and not facts.listish and not facts.promotions and _task(facts.body, config):
        return _outcome("action", "7", "imbox", None, None, 0, ({"type": "rule", "name": "7"},))
    if (
        facts.last_from_me
        and _closing(facts.body, config)
        and not _question(facts.body, config)
        and not _task(facts.body, config)
        and not _inbox_noise(facts)
    ):
        if not protected:
            return _outcome("resolved", "8", "imbox", None, None, 0, ({"type": "rule", "name": "8"},))
    if facts.has_key("account_security"):
        return _outcome(
            "reference",
            "4",
            "paper_trail",
            None,
            None,
            0,
            _labels(facts.keys.get("account_security", ())) + ({"type": "rule", "name": "4"},),
        )
    if facts.automated and facts.updates:
        return _outcome(
            "reference",
            "auto-ref",
            "paper_trail",
            None,
            None,
            0,
            ({"type": "rule", "name": "auto-ref"},),
        )
    return _outcome("needs_review", "else", "imbox", None, None, 0, ({"type": "rule", "name": "else"},))


def _structural_noise(facts: "_Facts") -> bool:
    if facts.human or not facts.has_key("automated"):
        return False
    noisy = facts.has_key("newsletter") or facts.has_key("job_alert") or facts.has_key("notification")
    return noisy and facts.listish


@dataclass
class _Facts:
    starred: bool
    star_labels: tuple[dict[str, object], ...]
    bin_labels: tuple[dict[str, object], ...]
    protected: bool
    muted: bool
    denied: bool
    on_read_list: bool
    human: bool
    last_from_me: bool
    last_at: int | None
    body: str
    sender_id: int | None
    sender_known: bool
    priority: int | None
    keys: dict[str, tuple[dict[str, object], ...]]
    listish: bool
    promotions: bool
    updates: bool
    draft_only: bool
    real_security: bool
    to_self: bool
    automated: bool
    newsletter_sender: bool
    security_sender: bool
    replied: bool
    machine: bool
    transfer_sender: bool
    to_hdr: str
    message_count: int
    subject: str
    auto_submitted: bool

    def has_key(self, role: str) -> bool:
        return bool(self.keys.get(role))

    def specific_content(self) -> bool:
        return any(self.keys.get(role) for role in _SPECIFIC_CONTENT)


def _load(
    conn: sqlite3.Connection,
    thread_pk: int,
    now: int,
    config: RulesConfig,
    label_keys: dict[str, tuple[str, ...]] | None = None,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
) -> _Facts:
    messages = conn.execute(
        """
        SELECT messages.msg_pk, messages.sender_id, messages.is_from_me,
               messages.internal_date, messages.body_text, messages.to_hdr,
               messages.subject, senders.address_norm
        FROM messages
        LEFT JOIN senders ON senders.sender_id = messages.sender_id
        WHERE messages.thread_pk = ?
        ORDER BY messages.internal_date, messages.msg_pk
        """,
        (thread_pk,),
    ).fetchall()
    outbound = any(bool(message["is_from_me"]) for message in messages)
    if outbound:
        kept = [message for message in messages if not _tracking_message(message, config)]
        if kept:
            messages = kept
    mute = conn.execute(
        "SELECT until FROM thread_mutes WHERE thread_pk = ?",
        (thread_pk,),
    ).fetchone()
    key_hits: dict[str, list[dict[str, object]]] = {role: [] for role in _KEY_ROLES}
    star_labels: list[dict[str, object]] = []
    bin_labels: list[dict[str, object]] = []
    listish = False
    promotions = False
    updates = False
    blocks_human = False
    auto_submitted = False
    human_label = False
    automated_label = False
    header_human = False
    draft_flags: list[bool] = []
    external_senders: list[int] = []
    for message in messages:
        labels = _message_labels(conn, int(message["msg_pk"]), label_keys)
        has_draft = False
        for label in labels:
            if label["id"] == "STARRED":
                star_labels.append(label)
            if label["id"] in ("SPAM", "TRASH"):
                bin_labels.append(label)
            if label["id"] == "DRAFT":
                has_draft = True
            if label["id"] == "CATEGORY_PROMOTIONS" or label["key"] == "CATEGORY_PROMOTIONS":
                promotions = True
            if label["id"] == "CATEGORY_UPDATES" or label["key"] == "CATEGORY_UPDATES":
                updates = True
            for role, key in config.keys.items():
                if label["key"] == key:
                    key_hits[role].append(label)
        draft_flags.append(has_draft)
        if not message["is_from_me"] and message["sender_id"] is not None:
            sender_id = int(message["sender_id"])
            external_senders.append(sender_id)
            sender_keys = {label["key"] for label in labels}
            if config.keys["human"] in sender_keys:
                human_label = True
            if config.keys["automated"] in sender_keys:
                automated_label = True
            if (
                config.keys["human"] not in sender_keys
                and config.keys["automated"] not in sender_keys
                and _header_human(conn, int(message["msg_pk"]))
            ):
                header_human = True
        if _listish(conn, int(message["msg_pk"])):
            listish = True
        if _blocks_human(conn, int(message["msg_pk"])):
            blocks_human = True
        if _auto_submitted(conn, int(message["msg_pk"])):
            auto_submitted = True
    last = messages[-1] if messages else None
    sender_id = external_senders[-1] if external_senders else None
    decision = _sender_decision(conn, sender_id)
    known = decision is not None
    denied = bool(decision and decision["status"] == "denied")
    on_read_list = bool(decision and decision["route"] == "read")
    approved_imbox = bool(
        decision and decision["status"] == "approved" and decision["route"] == "imbox"
    )
    priority = None
    if decision is not None and decision["priority"] is not None:
        priority = int(decision["priority"])
    body = "" if last is None or last["body_text"] is None else str(last["body_text"])
    last_address = None if last is None else last["address_norm"]
    subject = "" if last is None or last["subject"] is None else str(last["subject"])
    last_from_me = bool(last and last["is_from_me"])
    machine_sender = bool(
        last is not None
        and not last_from_me
        and _machine_address(last_address, config.machine_local)
    )
    transfer_sender = bool(
        last is not None
        and not last_from_me
        and _machine_address(last_address, config.transfer_local)
    )
    text = f"{subject}\n{body}"
    transfer = (
        transfer_sender
        or _any_phrase(text, config.transfer_notice)
        or _FILE_COUNT.search(text) is not None
    )
    stale = _any_phrase(text, config.stale_meeting)
    both_origins = human_label and automated_label
    human = (
        not machine_sender
        and not _mail_domain(last_address)
        and not transfer
        and not stale
        and not both_origins
        and not promotions
        and not blocks_human
        and (human_label or (header_human and not automated_label))
    )
    real_security = bool(key_hits["account_security"]) and not listish and not promotions
    protected = approved_imbox or bool(key_hits["contract"] or key_hits["invoice_receipt"] or real_security)
    if key_hits["correspondence"] and human:
        protected = True
    muted = False
    if mute is not None and (mute["until"] is None or int(mute["until"]) >= now):
        muted = True
    to_hdr = "" if last is None or last["to_hdr"] is None else str(last["to_hdr"])
    replied = any(bool(message["is_from_me"]) for message in messages)
    address = "" if not isinstance(last_address, str) else last_address.strip().lower()
    return _Facts(
        starred=bool(star_labels),
        star_labels=tuple(star_labels),
        bin_labels=tuple(bin_labels),
        protected=protected,
        muted=muted,
        denied=denied,
        on_read_list=on_read_list,
        human=human,
        last_from_me=last_from_me,
        last_at=None if last is None or last["internal_date"] is None else int(last["internal_date"]),
        body=body,
        sender_id=sender_id,
        sender_known=known,
        priority=priority,
        keys={role: tuple(items) for role, items in key_hits.items()},
        listish=listish,
        promotions=promotions,
        updates=updates,
        draft_only=bool(draft_flags) and all(draft_flags),
        real_security=real_security,
        to_self=last_from_me and _only_owners(to_hdr, owners, domains),
        automated=automated_label or transfer_sender,
        newsletter_sender=_machine_address(last_address, ("newsletter",)),
        security_sender=address in security_senders,
        replied=replied,
        machine=machine_sender or transfer_sender,
        transfer_sender=transfer_sender,
        to_hdr=to_hdr,
        message_count=len(messages),
        subject=subject,
        auto_submitted=auto_submitted,
    )


def _message_labels(
    conn: sqlite3.Connection,
    msg_pk: int,
    label_keys: dict[str, tuple[str, ...]] | None = None,
) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT labels.gmail_label_id, labels.name, labels.catalogue_key
        FROM message_labels
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
        WHERE message_labels.msg_pk = ? AND {GMAIL_LABEL_SQL}
        """,
        (msg_pk,),
    ).fetchall()
    labels: list[dict[str, object]] = []
    for row in rows:
        stored = row["catalogue_key"]
        keys = effective_keys(
            str(row["name"]),
            None if stored is None else str(stored),
            label_keys,
        )
        if not keys:
            keys = (None,)
        for key in keys:
            labels.append(
                {
                    "type": "label",
                    "id": row["gmail_label_id"],
                    "name": row["name"],
                    "key": key,
                }
            )
    return labels


def _header_human(conn: sqlite3.Connection, msg_pk: int) -> bool:
    rows = conn.execute(
        "SELECT name, value FROM headers WHERE msg_pk = ?",
        (msg_pk,),
    ).fetchall()
    found = {str(row["name"]).lower(): str(row["value"]) for row in rows}
    if "list-unsubscribe" in found:
        return False
    precedence = found.get("precedence", "").strip().lower()
    if re.search(r"(?<!\w)(bulk|list)(?!\w)", precedence):
        return False
    auto = found.get("auto-submitted")
    if auto is not None and auto.strip().lower() != "no":
        return False
    return True


def _blocks_human(conn: sqlite3.Connection, msg_pk: int) -> bool:
    rows = conn.execute(
        "SELECT name, value FROM headers WHERE msg_pk = ?",
        (msg_pk,),
    ).fetchall()
    for row in rows:
        name = str(row["name"]).lower()
        value = str(row["value"]).strip().lower()
        if name == "list-unsubscribe":
            return True
        if name == "precedence" and re.search(r"(?<!\w)bulk(?!\w)", value):
            return True
    return False


def _auto_submitted(conn: sqlite3.Connection, msg_pk: int) -> bool:
    rows = conn.execute(
        "SELECT value FROM headers WHERE msg_pk = ? AND lower(name) = 'auto-submitted'",
        (msg_pk,),
    ).fetchall()
    for row in rows:
        value = str(row["value"]).strip().lower()
        if value and value != "no":
            return True
    return False


def _listish(conn: sqlite3.Connection, msg_pk: int) -> bool:
    rows = conn.execute(
        "SELECT name, value FROM headers WHERE msg_pk = ?",
        (msg_pk,),
    ).fetchall()
    for row in rows:
        name = str(row["name"]).lower()
        value = str(row["value"]).strip().lower()
        if name in ("list-unsubscribe", "list-id"):
            return True
        if name == "precedence" and re.search(r"(?<!\w)(bulk|list)(?!\w)", value):
            return True
    return False


def _sender_decision(conn: sqlite3.Connection, sender_id: int | None) -> sqlite3.Row | None:
    if sender_id is None:
        return None
    return conn.execute(
        """
        SELECT status, route, priority FROM sender_decisions
        WHERE sender_id = ?
        """,
        (sender_id,),
    ).fetchone()


def _reply_after(facts: _Facts, created_at: int) -> bool:
    return False if facts.last_at is None else (not facts.last_from_me and facts.last_at > created_at)


def _is_owner(address: object, owners: frozenset[str], domains: frozenset[str]) -> bool:
    if not isinstance(address, str) or "@" not in address:
        return False
    addr = address.strip().lower()
    if addr in owners:
        return True
    return addr.rsplit("@", 1)[1] in domains


def _only_owners(to_hdr: object, owners: frozenset[str], domains: frozenset[str]) -> bool:
    if not isinstance(to_hdr, str) or not to_hdr.strip():
        return False
    found = False
    for part in to_hdr.split(","):
        _name, addr, _domain = parse_address(part)
        if addr is None:
            continue
        found = True
        if not _is_owner(addr, owners, domains):
            return False
    return found


def _machine_address(address: object, tokens: tuple[str, ...]) -> bool:
    if not isinstance(address, str) or "@" not in address:
        return False
    local = address.split("@", 1)[0].lower()
    parts = set(re.split(r"[._+\-]", local))
    for token in tokens:
        if token == local or token in parts:
            return True
        if "-" in token and local.startswith(token + "-"):
            return True
    return False


def _mail_domain(address: object) -> bool:
    if not isinstance(address, str) or "@" not in address:
        return False
    return address.rsplit("@", 1)[1].strip().lower().startswith("mail.")


def _receipt_words_apply(facts: _Facts) -> bool:
    return not (facts.human and facts.message_count > 1 and not facts.automated)


def _text(facts: _Facts) -> str:
    return f"{facts.subject}\n{facts.body}"


def _inbox_noise(facts: _Facts) -> bool:
    return facts.listish or facts.promotions


def _receipt(facts: _Facts, config: RulesConfig) -> bool:
    return bool(
        facts.has_key("invoice_receipt")
        or facts.has_key("purchase_delivery")
        or (_receipt_words_apply(facts) and _any_phrase(facts.body, config.receipt_words))
    )


def _tracking_message(message: sqlite3.Row, config: RulesConfig) -> bool:
    if message["is_from_me"]:
        return False
    subject = "" if message["subject"] is None else str(message["subject"])
    body = "" if message["body_text"] is None else str(message["body_text"])
    return _any_phrase(f"{subject}\n{body}", config.read_tracking)


def _tab_security(facts: _Facts, config: RulesConfig) -> bool:
    if not facts.updates or facts.specific_content():
        return False
    return _any_phrase(_text(facts), config.security_wording)


def _security_notice(facts: _Facts, config: RulesConfig) -> bool:
    if facts.listish or facts.promotions:
        return False
    return _any_phrase(_text(facts), config.security_notice)


def _access_request(facts: _Facts, config: RulesConfig) -> bool:
    if facts.human or facts.listish or facts.promotions:
        return False
    if not (facts.automated or facts.machine):
        return False
    return _any_phrase(_text(facts), config.access_request)


def _test_send(facts: _Facts, recipients: frozenset[str]) -> bool:
    if not facts.last_from_me or not _template_body(facts.body):
        return False
    if _known_tester(facts.to_hdr, recipients):
        return True
    return _opens_with_html(facts.body)


def _known_tester(to_hdr: str, recipients: frozenset[str]) -> bool:
    if not recipients or not to_hdr.strip():
        return False
    for part in to_hdr.split(","):
        _name, addr, _domain = parse_address(part)
        if isinstance(addr, str) and addr.strip().lower() in recipients:
            return True
    return False


def _opens_with_html(body: str) -> bool:
    match = re.search(r"</?[a-zA-Z][^>]*>", body)
    if match is None:
        return False
    return not body[: match.start()].strip()


def _handoff(facts: _Facts, config: RulesConfig) -> bool:
    if not facts.last_from_me:
        return False
    if _question(facts.body, config) or _task(facts.body, config):
        return False
    return _any_phrase(facts.body, config.file_handoff)


def _auto_reply(facts: _Facts, config: RulesConfig) -> bool:
    if _receipt(facts, config):
        return False
    if facts.auto_submitted:
        return True
    return _any_phrase(facts.subject, config.auto_reply_subject)


def _template_body(body: str) -> bool:
    text = body.strip()
    if len(text) < 40:
        return False
    return len(re.findall(r"</?[a-zA-Z][^>]*>", text)) >= 3


def _any_phrase(body: str, phrases: tuple[str, ...]) -> bool:
    return any(_phrase_in(body, phrase) for phrase in phrases)


def _housekeeping(facts: _Facts, config: RulesConfig) -> bool:
    return facts.to_self or _any_phrase(facts.body, config.calendar_response)


def _automated_reference(facts: _Facts, config: RulesConfig) -> bool:
    if facts.human:
        return False
    text = _text(facts)
    return (
        facts.transfer_sender
        or _FILE_COUNT.search(text) is not None
        or _any_phrase(text, config.automated_reference)
        or _any_phrase(text, config.transfer_notice)
        or _any_phrase(text, config.stale_meeting)
    )


def _short_closing(body: str, config: RulesConfig) -> bool:
    text = body.strip()
    if not text or len(text) > 80:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 2:
        return False
    return _closing(body, config) and not _question(body, config) and not _task(body, config)


def _question(body: str, config: RulesConfig) -> bool:
    for line in body.splitlines():
        if line.strip().endswith("?"):
            return True
    return _any_phrase(body, config.question)


def _task(body: str, config: RulesConfig) -> bool:
    return any(_phrase_in(body, phrase) for phrase in config.task)


def _closing(body: str, config: RulesConfig) -> bool:
    return any(_phrase_in(body, phrase) for phrase in config.closing)


def _phrase_in(body: str, phrase: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
    return re.search(pattern, body.lower()) is not None


def _priority_ok(priority: int | None) -> bool:
    return priority is not None and 0 <= priority <= 3


def _labels(items: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    cleaned: list[dict[str, object]] = []
    for item in items:
        cleaned.append({"type": "label", "id": item.get("id"), "name": item.get("name")})
    return tuple(cleaned)


def _with_time(
    conn,
    thread_pk: int,
    outcome: _Outcome,
    now: int,
    rules_version: str,
    policy,
    review: bool = False,
) -> _Outcome:
    from mailcrawl.decisions import DecisionDraft
    from mailcrawl.timefacts import apply_time, present_time_closure, thread_time_facts

    draft = DecisionDraft(
        next_state=outcome.next_state,
        lane=outcome.lane,
        rule_id=outcome.rule_id,
        rules_version=rules_version,
        origin="rule",
        source_detail=outcome.rule_id,
        evidence=outcome.evidence,
        priority=outcome.priority,
        review_date=outcome.review_date,
        muted=outcome.muted,
    )
    adjusted = apply_time(draft, thread_time_facts(conn, thread_pk, now), policy)
    presented = present_time_closure(adjusted, outcome.lane, review)
    if presented is adjusted and adjusted is draft:
        return outcome
    return _outcome(
        presented.next_state,
        outcome.rule_id,
        presented.lane,
        outcome.priority,
        presented.review_date,
        outcome.muted,
        presented.evidence,
    )


def _outcome(
    state: str,
    rule_id: str,
    lane: str,
    priority: int | None,
    review_date: int | None,
    muted: int,
    evidence: tuple[dict[str, object], ...],
) -> _Outcome:
    return _Outcome(state, rule_id, lane, priority, review_date, muted, evidence)


def supersede_current(
    conn: sqlite3.Connection,
    old_id: int,
    insert_new: Callable[[], int],
) -> int:
    row = conn.execute(
        "SELECT origin FROM decisions WHERE decision_id = ?",
        (old_id,),
    ).fetchone()
    if row is not None and str(row["origin"]) == "human":
        raise UsageError("a human decision stays")
    conn.execute(
        "UPDATE decisions SET superseded_by = ? WHERE decision_id = ?",
        (old_id, old_id),
    )
    new_id = insert_new()
    conn.execute(
        "UPDATE decisions SET superseded_by = ? WHERE decision_id = ?",
        (new_id, old_id),
    )
    return new_id


def _supersede(
    conn: sqlite3.Connection,
    current: sqlite3.Row,
    outcome: _Outcome,
    now: int,
    rules_version: str,
) -> None:
    if _same(current, outcome):
        return
    supersede_current(
        conn,
        int(current["decision_id"]),
        lambda: _insert_decision(
            conn,
            int(current["thread_pk"]),
            outcome,
            now,
            rules_version,
            None,
        ),
    )


def _same(current: sqlite3.Row, outcome: _Outcome) -> bool:
    priority = current["priority"]
    review = current["review_date"]
    return (
        str(current["next_state"]) == outcome.next_state
        and str(current["rule_id"]) == outcome.rule_id
        and str(current["lane"]) == outcome.lane
        and priority == outcome.priority
        and review == outcome.review_date
        and int(current["muted"]) == outcome.muted
    )


def _insert_decision(
    conn: sqlite3.Connection,
    thread_pk: int,
    outcome: _Outcome,
    now: int,
    rules_version: str,
    superseded_by: int | None,
) -> int:
    evidence = json.dumps(list(outcome.evidence), sort_keys=True, separators=(",", ":"))
    row = conn.execute(
        """
        INSERT INTO decisions(
          thread_pk, next_state, priority, review_date, lane, muted,
          source, origin, source_detail, rule_id, rules_version, evidence,
          created_at, superseded_by
        ) VALUES (?, ?, ?, ?, ?, ?, 'rule', 'rule', ?, ?, ?, ?, ?, ?)
        RETURNING decision_id
        """,
        (
            thread_pk,
            outcome.next_state,
            outcome.priority,
            outcome.review_date,
            outcome.lane,
            outcome.muted,
            outcome.rule_id,
            outcome.rule_id,
            rules_version,
            evidence,
            now,
            superseded_by,
        ),
    ).fetchone()
    return int(row["decision_id"])
