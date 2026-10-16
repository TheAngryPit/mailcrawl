"""Optional vec0 tables. Weights load only inside a gated embed call."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Protocol

from mailcrawl.decisions import DecisionDraft, current_decision, decide_once
from mailcrawl.inference import enabled_points, enter_model, invoke
from mailcrawl.paths import UsageError

NATIVE_DIM = 768
CLASSIFICATION_PREFIX = "task: classification | query: "
SEARCH_QUERY_PREFIX = "task: search result | query: "
SEARCH_TITLE_PREFIX = "title: "
LOCAL_VERIFY = (
    "python3 -m mailcrawl --db ~/MailVault/db-v3-sandbox "
    "embed --account thehive --embedder gemma"
)
_TABLE = {
    "classification": "vec_classification",
    "search": "vec_search",
}


class FakeEmbedder:
    model_id = "fake-embedder"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        return [_hash_vector(text, self.dimension) for text in texts]


class ScriptedEmbedder:
    model_id = "scripted-embedder"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        return [_scripted_vector(text, self.dimension) for text in texts]


class GemmaEmbedder:
    model_id = "google/embeddinggemma-2"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise UsageError(
                "sentence-transformers>=6.1 is not installed. "
                f"local-verify: {LOCAL_VERIFY}"
            ) from exc
        try:
            import torch
        except ImportError as exc:
            raise UsageError(
                "torch is not installed. "
                f"local-verify: {LOCAL_VERIFY}"
            ) from exc
        model = _load_gemma(SentenceTransformer, torch)
        if getattr(model, "default_prompt_name", None) is not None:
            model.default_prompt_name = None
        raw = model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vectors: list[list[float]] = []
        for row in raw:
            values = [float(item) for item in list(row)]
            if len(values) != NATIVE_DIM:
                raise UsageError(f"{self.model_id} returned {len(values)} dimensions")
            vectors.append(truncate(values, NATIVE_DIM))
        return vectors


class Embedder(Protocol):
    model_id: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def open_embedder(name: str) -> Embedder:
    if name == "fake":
        return FakeEmbedder()
    if name == "scripted":
        return ScriptedEmbedder()
    if name == "gemma":
        return GemmaEmbedder()
    raise UsageError(f"unknown embedder {name}")


def require_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ImportError as exc:
        raise UsageError(
            "sqlite-vec is not installed. The vectors extra provides sqlite-vec "
            "and sentence-transformers>=6.1."
        ) from exc
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    create_vec_tables(conn)


def _load_gemma(sentence_transformer, torch):
    dtypes = []
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    if bool(torch.cuda.is_available()) or (mps is not None and bool(mps.is_available())):
        dtypes.append(torch.bfloat16)
    dtypes.append(torch.float32)
    last: Exception | None = None
    for dtype in dtypes:
        try:
            return sentence_transformer(
                "google/embeddinggemma-2",
                model_kwargs={"torch_dtype": dtype},
            )
        except Exception as exc:
            last = exc
    raise UsageError(
        f"could not load google/embeddinggemma-2 in bfloat16 or float32: {last}"
    )


def create_vec_tables(conn: sqlite3.Connection) -> None:
    for name in _TABLE.values():
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            (name,),
        ).fetchone()
        if found:
            continue
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE {name} USING vec0(
              embedding float[{NATIVE_DIM}] distance_metric=cosine,
              +thread_pk integer,
              +prefix text,
              +dim integer
            )
            """
        )


def classification_text(body: str) -> str:
    return CLASSIFICATION_PREFIX + body.strip()


def search_document(subject: str | None, body: str) -> str:
    title = (subject or "").strip()
    head = f"title: {title}" if title else "title: none"
    return f"{head} | text: {body.strip()}"


def search_query(text: str) -> str:
    return SEARCH_QUERY_PREFIX + text.strip()


def truncate(vector: list[float], dim: int) -> list[float]:
    if dim < 1 or dim > len(vector):
        raise ValueError(dim)
    head = vector[:dim]
    norm = math.sqrt(sum(value * value for value in head))
    if norm == 0:
        return head
    return [value / norm for value in head]


def embed_threads(conn: sqlite3.Connection, embedder: Embedder, config: dict[str, bool] | None) -> int:
    if "embeddings" not in enabled_points(config):
        return 0
    grouped = _threads(conn)
    if not grouped:
        return 0
    require_vec(conn)
    texts: list[str] = []
    for _thread_pk, subject, body in grouped:
        texts.append(classification_text(body))
        texts.append(search_document(subject, body))
    vectors = invoke("embeddings", lambda: embedder.embed(texts), config)
    if vectors is None:
        return 0
    if len(vectors) != len(texts):
        raise ValueError("embedder returned the wrong number of vectors")
    for index, (thread_pk, _subject, _body) in enumerate(grouped):
        _upsert(
            conn,
            "classification",
            thread_pk,
            vectors[2 * index],
            CLASSIFICATION_PREFIX,
        )
        _upsert(
            conn,
            "search",
            thread_pk,
            vectors[2 * index + 1],
            SEARCH_TITLE_PREFIX,
        )
    conn.commit()
    return len(grouped)


def nearest_threads(
    conn: sqlite3.Connection,
    kind: str,
    vector: list[float] | bytes,
    k: int,
) -> list[tuple[int, float]]:
    table = _TABLE[kind]
    payload: object = vector if isinstance(vector, bytes) else json.dumps(vector)
    rows = conn.execute(
        f"""
        SELECT thread_pk, distance FROM {table}
        WHERE embedding MATCH ? AND k = ?
        """,
        (payload, k),
    ).fetchall()
    return [(int(row[0]), float(row[1])) for row in rows]


def ordered_hits(
    keyword: list[tuple[int, str]],
    vec: list[int],
    *,
    fusion: str,
    keyword_weight: float,
    vec_weight: float,
) -> list[tuple[int, str]]:
    keyword_ids = [msg_pk for msg_pk, _source in keyword]
    if fusion == "keyword":
        seen = set(keyword_ids)
        filled = list(keyword)
        for msg_pk in vec:
            if msg_pk not in seen:
                filled.append((msg_pk, "vec"))
                seen.add(msg_pk)
        return filled
    if fusion != "rrf":
        raise ValueError(fusion)
    scores: dict[int, float] = {}
    for rank, msg_pk in enumerate(keyword_ids, start=1):
        scores[msg_pk] = scores.get(msg_pk, 0.0) + keyword_weight / (60 + rank)
    for rank, msg_pk in enumerate(vec, start=1):
        scores[msg_pk] = scores.get(msg_pk, 0.0) + vec_weight / (60 + rank)
    ordered = sorted(scores, key=lambda msg_pk: (-scores[msg_pk], msg_pk))
    return [(msg_pk, "fused") for msg_pk in ordered]


def knn_decide(
    conn: sqlite3.Connection,
    thread_pk: int,
    embedder: Embedder,
    config: dict[str, bool] | None,
    now: int,
) -> sqlite3.Row | None:
    if "embeddings" not in enabled_points(config):
        return None
    require_vec(conn)
    _ensure_classification(conn, thread_pk, embedder, config)
    blob = _stored_embedding(conn, "classification", thread_pk)
    if blob is None:
        return None
    count = conn.execute(f"SELECT count(*) FROM {_TABLE['classification']}").fetchone()[0]
    neighbours = nearest_threads(conn, "classification", blob, max(int(count), 1))
    ranked: list[tuple[float, sqlite3.Row]] = []
    for other_pk, distance in neighbours:
        if other_pk == thread_pk:
            continue
        decision = current_decision(conn, other_pk)
        if decision is None:
            continue
        ranked.append((distance, decision))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    distance, neighbour = ranked[0]

    def produce() -> DecisionDraft:
        return DecisionDraft(
            next_state=neighbour["next_state"],
            lane=neighbour["lane"],
            rule_id=neighbour["rule_id"],
            rules_version=neighbour["rules_version"],
            origin="knn",
            source_detail=embedder.model_id,
            evidence=(
                {
                    "distance": distance,
                    "neighbour_thread_pk": int(neighbour["thread_pk"]),
                },
            ),
            priority=neighbour["priority"],
            review_date=neighbour["review_date"],
            muted=int(neighbour["muted"] or 0),
        )

    return decide_once(conn, thread_pk, now, produce)


def _hash_vector(text: str, dim: int) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
        for offset in range(0, len(block), 4):
            chunk = block[offset : offset + 4]
            if len(chunk) < 4:
                continue
            number = int.from_bytes(chunk, "big")
            values.append((number / 2**31) - 1.0)
    return truncate(values[:dim], dim)


def _scripted_vector(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    if text.startswith(SEARCH_QUERY_PREFIX) or "vec-only" in text:
        vector[0] = 1.0
    else:
        vector[1] = 1.0
    return vector


def _threads(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        """
        SELECT thread_pk, subject, body_text
        FROM messages
        ORDER BY thread_pk, internal_date, msg_pk
        """
    ).fetchall()
    grouped: dict[int, tuple[str, list[str]]] = {}
    for thread_pk, subject, body_text in rows:
        key = int(thread_pk)
        title, parts = grouped.get(key, ("", []))
        if not title and subject and str(subject).strip():
            title = str(subject).strip()
        if body_text and str(body_text).strip():
            parts.append(str(body_text).strip())
        grouped[key] = (title, parts)
    return [(key, title, "\n".join(parts)) for key, (title, parts) in grouped.items()]


def _upsert(
    conn: sqlite3.Connection,
    kind: str,
    thread_pk: int,
    vector: list[float],
    prefix: str,
) -> None:
    if len(vector) != NATIVE_DIM:
        raise ValueError(len(vector))
    table = _TABLE[kind]
    conn.execute(f"DELETE FROM {table} WHERE thread_pk = ?", (thread_pk,))
    conn.execute(
        f"""
        INSERT INTO {table}(embedding, thread_pk, prefix, dim)
        VALUES (?, ?, ?, ?)
        """,
        (json.dumps(vector), thread_pk, prefix, NATIVE_DIM),
    )


def _stored_embedding(conn: sqlite3.Connection, kind: str, thread_pk: int) -> bytes | None:
    row = conn.execute(
        f"SELECT embedding FROM {_TABLE[kind]} WHERE thread_pk = ?",
        (thread_pk,),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def _ensure_classification(
    conn: sqlite3.Connection,
    thread_pk: int,
    embedder: Embedder,
    config: dict[str, bool] | None,
) -> None:
    if _stored_embedding(conn, "classification", thread_pk) is not None:
        return
    rows = [row for row in _threads(conn) if row[0] == thread_pk]
    if not rows:
        return
    _thread_pk, subject, body = rows[0]
    texts = [classification_text(body), search_document(subject, body)]
    vectors = invoke("embeddings", lambda: embedder.embed(texts), config)
    if vectors is None:
        return
    _upsert(conn, "classification", thread_pk, vectors[0], CLASSIFICATION_PREFIX)
    _upsert(conn, "search", thread_pk, vectors[1], SEARCH_TITLE_PREFIX)
    conn.commit()
