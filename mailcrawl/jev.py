"""Typed Jev battery. Answers are stored once. Code composes the lane."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from mailcrawl.decisions import DecisionDraft, decide_open, eligible_thread_pks
from mailcrawl.inference import enabled_points, enter_model, invoke
from mailcrawl.paths import UsageError
from mailcrawl.timefacts import TimePolicy, apply_time, present_time_closure, thread_time_facts

MODEL_ID = "jev-1.13.0"
BATTERY_VERSION = "battery-v1"
SEARCH_VERSION = "search-noul-v1"
NOUL_NO = 0.40
NOUL_YES = 0.60
CONFIDENCE_FLOOR = 0.60
RERANK_LIMIT = 20
_RETRY_CAP = 60
LOCAL_VERIFY = (
    "python3 -m mailcrawl --db ~/MailVault/db-v3-sandbox "
    "jev --account thehive --client http"
)
_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_CATEGORY_CRITERIA = {
    "CATEGORY_PERSONAL": "Mail from a person to the recipient.",
    "CATEGORY_SOCIAL": "A social network notification.",
    "CATEGORY_PROMOTIONS": "Marketing or a promotion.",
    "CATEGORY_UPDATES": "A receipt, a bill, or an automated update.",
    "CATEGORY_FORUMS": "A list or a forum.",
    "other": "None of the category keys fit.",
}
_COMPOSE = (
    ("is-bulk", "noise", "feed"),
    ("asks-deadline", "action", "imbox"),
    ("asks-reply", "reply", "imbox"),
    ("is-receipt", "reference", "paper_trail"),
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UsageError("jev refuses a redirect")


class TokenLimit(Exception):
    pass


class RateLimited(Exception):
    pass


@dataclass(frozen=True)
class Question:
    question_id: str
    kind: str
    instructions: str
    criteria: dict[str, str] | tuple[str, ...] | None = None


@dataclass(frozen=True)
class Answer:
    kind: str
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None


BATTERY = (
    Question(
        "asks-reply",
        "noul",
        "Does this message ask the recipient to reply?",
        {
            "true": "The recipient is asked to answer.",
            "false": "No reply is requested.",
        },
    ),
    Question(
        "asks-deadline",
        "noul",
        "Does this message ask for an action with a stated deadline?",
        {
            "true": "An action and a deadline are both stated.",
            "false": "No deadline is stated.",
        },
    ),
    Question(
        "is-receipt",
        "noul",
        "Is this message a receipt or an invoice?",
        {
            "true": "It is a receipt or an invoice.",
            "false": "It is not a receipt or an invoice.",
        },
    ),
    Question(
        "is-bulk",
        "noul",
        "Is this bulk or marketing mail?",
        {
            "true": "It is bulk or marketing mail.",
            "false": "It is not bulk or marketing mail.",
        },
    ),
    Question(
        "category",
        "choice",
        "Which category key fits this message?",
        _CATEGORY_CRITERIA,
    ),
    Question(
        "urgency",
        "score",
        "How urgent is this message for the recipient?",
        ("none", "low", "high"),
    ),
)


class JevClient(Protocol):
    model_id: str
    left_machine: bool

    def ask(
        self,
        state: str,
        questions: tuple[Question, ...],
    ) -> tuple[dict[str, Answer], str]: ...


def open_client(name: str) -> JevClient:
    if name == "fake":
        return FakeJev()
    if name == "http":
        return HttpJev()
    raise UsageError(f"unknown jev client {name}")


def request_body(state: str, questions: tuple[Question, ...]) -> dict[str, object]:
    if MODEL_ID == "jev-latest":
        raise UsageError("jev-latest is not a pin")
    return {
        "model": MODEL_ID,
        "state": state,
        "questions": {item.question_id: _payload(item) for item in questions},
    }


def jev_decide(
    conn: sqlite3.Connection,
    thread_pk: int,
    client: JevClient,
    config: dict[str, bool] | None,
    now: int,
    *,
    policy: TimePolicy | None = None,
    review: bool = False,
) -> sqlite3.Row | None:
    if "jev-battery" not in enabled_points(config):
        return None
    _refuse_alias(client.model_id)
    if _image_only(conn, thread_pk):
        return None
    return decide_open(
        conn,
        thread_pk,
        now,
        lambda: _timed(conn, thread_pk, client, config, now, policy, review),
    )


@dataclass(frozen=True)
class GreyPass:
    eligible: int
    decided: int
    review: int


def score_grey_band(
    conn: sqlite3.Connection,
    client: JevClient,
    config: dict[str, bool] | None,
    now: int,
    *,
    policy: TimePolicy | None = None,
    review: bool = False,
) -> GreyPass:
    rows = eligible_thread_pks(conn, include_image_only=False)
    decided = 0
    pending = 0
    for thread_pk in rows:
        decision = jev_decide(
            conn,
            thread_pk,
            client,
            config,
            now,
            policy=policy,
            review=review,
        )
        if decision is None or decision["next_state"] == "needs_review":
            pending += 1
        else:
            decided += 1
    return GreyPass(len(rows), decided, pending)


def _api_key() -> str:
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise UsageError(
        "TYPESAFE_API_KEY or JEV_API_KEY is not set. "
        f"local-verify: {LOCAL_VERIFY}"
    )


def _retry_after(header: str | None) -> float:
    if header is None:
        return 0
    try:
        seconds = int(header.strip())
    except ValueError:
        return 0
    if seconds < 0:
        return 0
    return float(min(seconds, _RETRY_CAP))


def rerank(
    conn: sqlite3.Connection,
    ranked: list[tuple[int, str]],
    client: JevClient | None,
    config: dict[str, bool] | None,
    query: str,
) -> list[tuple[int, str]]:
    if client is None or "search-reranker" not in enabled_points(config):
        return ranked
    _refuse_alias(client.model_id)
    head = ranked[:RERANK_LIMIT]
    tail = ranked[RERANK_LIMIT:]
    if not head:
        return ranked
    texts = _thread_text(conn, [msg_pk for msg_pk, _source in head])
    scores = _cached_scores(conn, query, [thread_pk for _msg, thread_pk, _text in texts])
    if scores is None:
        state = _search_state(query, texts)
        questions = _search_questions(texts)
        answered = invoke(
            "search-reranker",
            lambda: client.ask(state, questions),
            config,
        )
        if answered is None:
            return ranked
        answers, model = answered
        _refuse_alias(model)
        scores = {}
        for _msg, thread_pk, _text in texts:
            question_id = f"pair-{thread_pk}"
            value = _noul_value(answers.get(question_id))
            scores[thread_pk] = value
            _save_rerank(conn, query, thread_pk, question_id, model, value)
        any_answer = answers.get("any-answers")
        _save_rerank(conn, query, 0, "any-answers", model, _noul_value(any_answer))
        conn.commit()
    ordered = sorted(
        range(len(head)),
        key=lambda index: (-scores[texts[index][1]], index),
    )
    return [(head[index][0], "jev") for index in ordered] + list(tail)


class FakeJev:
    model_id = MODEL_ID
    left_machine = False

    def ask(
        self,
        state: str,
        questions: tuple[Question, ...],
    ) -> tuple[dict[str, Answer], str]:
        enter_model()
        document = {"model": MODEL_ID, "answers": _scripted(state, questions)}
        return parse_document(document, questions)


class HttpJev:
    model_id = MODEL_ID
    left_machine = True

    def ask(
        self,
        state: str,
        questions: tuple[Question, ...],
    ) -> tuple[dict[str, Answer], str]:
        enter_model()
        key = _api_key()
        payload = json.dumps(request_body(state, questions)).encode()
        req = urllib.request.Request(
            _ENDPOINT,
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        raw = ""
        for attempt in (1, 2):
            try:
                with opener.open(req, timeout=30) as resp:
                    raw = resp.read().decode()
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                if "max_tokens_exceeded" in body:
                    raise TokenLimit() from exc
                if exc.code == 429 and attempt == 1:
                    time.sleep(_retry_after(exc.headers.get("Retry-After")))
                    continue
                if exc.code == 429:
                    raise RateLimited() from exc
                raise UsageError("jev request failed") from exc
            except urllib.error.URLError as exc:
                raise UsageError("jev request failed") from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UsageError("jev request failed") from exc
        if not isinstance(document, dict):
            raise UsageError("jev request failed")
        if "max_tokens_exceeded" in json.dumps(document):
            raise TokenLimit()
        return parse_document(document, questions)


def parse_document(
    document: dict[str, object],
    questions: tuple[Question, ...],
) -> tuple[dict[str, Answer], str]:
    model = document.get("model")
    if not isinstance(model, str):
        raise UsageError("jev response has no model")
    _refuse_alias(model)
    raw = document.get("answers")
    if not isinstance(raw, dict):
        raise UsageError("jev response has no answers")
    found: dict[str, Answer] = {}
    for item in questions:
        payload = raw.get(item.question_id)
        if not isinstance(payload, dict):
            raise UsageError(f"jev response missed {item.question_id}")
        found[item.question_id] = _parse_answer(item, payload)
    return found, model


def _timed(
    conn: sqlite3.Connection,
    thread_pk: int,
    client: JevClient,
    config: dict[str, bool] | None,
    now: int,
    policy: TimePolicy | None,
    review: bool,
) -> DecisionDraft:
    draft = _produce(conn, thread_pk, client, config)
    facts = thread_time_facts(conn, thread_pk, now)
    adjusted = apply_time(draft, facts, policy)
    return present_time_closure(adjusted, draft.lane, review)


def _produce(
    conn: sqlite3.Connection,
    thread_pk: int,
    client: JevClient,
    config: dict[str, bool] | None,
) -> DecisionDraft:
    stored = _load_answers(conn, thread_pk)
    model = client.model_id
    if stored == "partial":
        return _review(model, client.left_machine)
    if stored is None:
        try:
            answered = invoke(
                "jev-battery",
                lambda: client.ask(_thread_state(conn, thread_pk), BATTERY),
                config,
            )
        except (TokenLimit, RateLimited):
            return _review(client.model_id, client.left_machine)
        if answered is None:
            return _review(client.model_id, False)
        stored, model = answered
        _refuse_alias(model)
        _save_answers(conn, thread_pk, model, stored)
    return _draft(stored, model, client.left_machine)


def _draft(answers: dict[str, Answer], model: str, left_machine: bool) -> DecisionDraft:
    grey = _in_band(answers)
    if grey:
        state, lane = "needs_review", "imbox"
    else:
        state, lane = _lane(answers)
    return DecisionDraft(
        next_state=state,
        lane=lane,
        rule_id="jev",
        rules_version=BATTERY_VERSION,
        origin="model",
        source_detail=model,
        evidence=(
            {
                "battery": BATTERY_VERSION,
                "grey": grey,
                "left_machine": left_machine,
                "model": model,
            },
        ),
    )


def _review(model: str, left_machine: bool) -> DecisionDraft:
    return DecisionDraft(
        next_state="needs_review",
        lane="imbox",
        rule_id="jev",
        rules_version=BATTERY_VERSION,
        origin="model",
        source_detail=model,
        evidence=(
            {
                "battery": BATTERY_VERSION,
                "grey": True,
                "left_machine": left_machine,
                "model": model,
            },
        ),
    )


def _in_band(answers: dict[str, Answer]) -> bool:
    for item in BATTERY:
        answer = answers.get(item.question_id)
        if answer is None:
            return True
        if item.kind == "noul":
            if answer.noul is None or NOUL_NO < answer.noul < NOUL_YES:
                return True
            continue
        if answer.confidence is None or answer.confidence < CONFIDENCE_FLOOR:
            return True
    return False


def _lane(answers: dict[str, Answer]) -> tuple[str, str]:
    for question_id, state, lane in _COMPOSE:
        answer = answers[question_id]
        if answer.noul is not None and answer.noul >= NOUL_YES:
            return state, lane
    return "reference", "paper_trail"


def _payload(item: Question) -> dict[str, object]:
    body: dict[str, object] = {"type": item.kind, "instructions": item.instructions}
    if item.criteria is not None:
        body["criteria"] = list(item.criteria) if item.kind == "score" else item.criteria
    return body


def _parse_answer(item: Question, payload: dict[str, object]) -> Answer:
    if item.kind == "noul":
        value = payload.get("noul")
        if not isinstance(value, (int, float)):
            raise UsageError(f"jev noul {item.question_id}")
        return Answer(kind="noul", noul=float(value))
    probabilities = _probabilities(payload.get("probabilities"))
    confidence = payload.get("confidence")
    if item.kind == "choice":
        choice = payload.get("choice")
        if not isinstance(choice, str):
            raise UsageError(f"jev choice {item.question_id}")
        return Answer(
            kind="choice",
            choice=choice,
            confidence=None if not isinstance(confidence, (int, float)) else float(confidence),
            probabilities=probabilities,
        )
    score = payload.get("score")
    return Answer(
        kind="score",
        score=None if not isinstance(score, (int, float)) else float(score),
        confidence=None if not isinstance(confidence, (int, float)) else float(confidence),
        probabilities=probabilities,
    )


def _probabilities(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    found: dict[str, float] = {}
    for key, item in value.items():
        if isinstance(item, (int, float)):
            found[str(key)] = float(item)
    return found


def _scripted(state: str, questions: tuple[Question, ...]) -> dict[str, object]:
    answers: dict[str, object] = {}
    for item in questions:
        if item.kind == "noul":
            answers[item.question_id] = {"type": "noul", "noul": _scripted_noul(item.question_id, state)}
        elif item.kind == "choice":
            choice = _scripted_choice(state)
            answers[item.question_id] = {
                "type": "choice",
                "choice": choice,
                "confidence": 0.90,
                "probabilities": {choice: 0.90, "other": 0.10},
            }
        else:
            level = "high" if "stated deadline" in state else "none"
            answers[item.question_id] = {
                "type": "score",
                "score": 2.0 if level == "high" else 0.0,
                "confidence": 0.90,
                "probabilities": {level: 0.90},
            }
    return answers


def _scripted_noul(question_id: str, state: str) -> float:
    if question_id == "any-answers":
        return 0.95 if "answers-query" in state else 0.05
    if question_id.startswith("pair-"):
        prefix = f"thread {question_id.removeprefix('pair-')}:"
        for line in state.splitlines():
            if line.startswith(prefix):
                return 0.95 if "answers-query" in line else 0.05
        return 0.05
    if question_id == "asks-reply" and "verify your password" in state:
        return 0.50
    markers = {
        "asks-reply": "please reply",
        "asks-deadline": "stated deadline",
        "is-receipt": "invoice receipt",
        "is-bulk": "bulk marketing",
    }
    marker = markers.get(question_id)
    if marker is not None and marker in state:
        return 0.95
    return 0.05


def _scripted_choice(state: str) -> str:
    if "bulk marketing" in state:
        return "CATEGORY_PROMOTIONS"
    if "please reply" in state:
        return "CATEGORY_PERSONAL"
    return "other"


def _refuse_alias(model: str) -> None:
    if model == "jev-latest" or model == "jev-preview":
        raise UsageError("jev-latest is not a pin")
    if model != MODEL_ID:
        raise UsageError(f"jev model {model}")


def _image_only(conn: sqlite3.Connection, thread_pk: int) -> bool:
    row = conn.execute(
        "SELECT image_only FROM threads WHERE thread_pk = ?",
        (thread_pk,),
    ).fetchone()
    return row is not None and int(row[0]) == 1


def _thread_state(conn: sqlite3.Connection, thread_pk: int) -> str:
    rows = conn.execute(
        """
        SELECT body_text FROM messages
        WHERE thread_pk = ?
        ORDER BY internal_date, msg_pk
        """,
        (thread_pk,),
    ).fetchall()
    return "\n".join(row[0] or "" for row in rows)


def _load_answers(conn: sqlite3.Connection, thread_pk: int) -> dict[str, Answer] | None | str:
    rows = conn.execute(
        """
        SELECT question_id, kind, answer_json FROM jev_answers
        WHERE thread_pk = ? AND battery_version = ? AND model_id = ?
        """,
        (thread_pk, BATTERY_VERSION, MODEL_ID),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != len(BATTERY):
        return "partial"
    found: dict[str, Answer] = {}
    for question_id, kind, raw in rows:
        payload = json.loads(raw)
        item = Question(question_id, kind, "")
        found[question_id] = _parse_answer(item, payload)
    return found


def _save_answers(
    conn: sqlite3.Connection,
    thread_pk: int,
    model: str,
    answers: dict[str, Answer],
) -> None:
    for item in BATTERY:
        answer = answers[item.question_id]
        conn.execute(
            """
            INSERT INTO jev_answers(
              thread_pk, question_id, battery_version, model_id, kind, answer_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                thread_pk,
                item.question_id,
                BATTERY_VERSION,
                model,
                item.kind,
                json.dumps(_answer_json(answer), sort_keys=True, separators=(",", ":")),
            ),
        )


def _answer_json(answer: Answer) -> dict[str, object]:
    if answer.kind == "noul":
        return {"type": "noul", "noul": answer.noul}
    if answer.kind == "choice":
        return {
            "type": "choice",
            "choice": answer.choice,
            "confidence": answer.confidence,
            "probabilities": answer.probabilities or {},
        }
    return {
        "type": "score",
        "score": answer.score,
        "confidence": answer.confidence,
        "probabilities": answer.probabilities or {},
    }


def _thread_text(
    conn: sqlite3.Connection,
    msg_pks: list[int],
) -> list[tuple[int, int, str]]:
    if not msg_pks:
        return []
    marks = ",".join("?" for _ in msg_pks)
    rows = conn.execute(
        f"""
        SELECT msg_pk, thread_pk, body_text FROM messages
        WHERE msg_pk IN ({marks})
        """,
        msg_pks,
    ).fetchall()
    by_msg = {int(row[0]): (int(row[1]), row[2] or "") for row in rows}
    return [(msg_pk, *by_msg[msg_pk]) for msg_pk in msg_pks if msg_pk in by_msg]


def _search_state(query: str, texts: list[tuple[int, int, str]]) -> str:
    lines = [f"query: {query}"]
    for _msg, thread_pk, text in texts:
        lines.append(f"thread {thread_pk}: {text}")
    return "\n".join(lines)


def _search_questions(texts: list[tuple[int, int, str]]) -> tuple[Question, ...]:
    pairs = tuple(
        Question(
            f"pair-{thread_pk}",
            "noul",
            "Does this thread answer the query?",
        )
        for _msg, thread_pk, _text in texts
    )
    return pairs + (
        Question("any-answers", "noul", "Does any row answer the query?"),
    )


def _cached_scores(
    conn: sqlite3.Connection,
    query: str,
    thread_pks: list[int],
) -> dict[int, float] | None:
    found: dict[int, float] = {}
    for thread_pk in thread_pks:
        row = conn.execute(
            """
            SELECT value FROM jev_rerank
            WHERE query_text = ? AND thread_pk = ? AND question_id = ?
              AND model_id = ? AND question_version = ?
            """,
            (query, thread_pk, f"pair-{thread_pk}", MODEL_ID, SEARCH_VERSION),
        ).fetchone()
        if row is None:
            return None
        found[thread_pk] = float(row[0])
    return found


def _save_rerank(
    conn: sqlite3.Connection,
    query: str,
    thread_pk: int,
    question_id: str,
    model: str,
    value: float,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO jev_rerank(
          query_text, thread_pk, question_id, model_id, question_version, value
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (query, thread_pk, question_id, model, SEARCH_VERSION, value),
    )


def _noul_value(answer: Answer | None) -> float:
    if answer is None or answer.noul is None:
        return 0.0
    return float(answer.noul)
