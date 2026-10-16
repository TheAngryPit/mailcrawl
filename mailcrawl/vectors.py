"""Optional vec0 tables. Weights load only inside a gated embed call."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mailcrawl.decisions import DecisionDraft, current_decision, decide_once
from mailcrawl.inference import enabled_points, enter_model, invoke
from mailcrawl.paths import UsageError
from mailcrawl.threads import embeddable_image

NATIVE_DIM = 768
CLASSIFICATION_PREFIX = "task: classification | query: "
SEARCH_QUERY_PREFIX = "task: search result | query: "
SEARCH_TITLE_PREFIX = "title: "
LOCAL_VERIFY = (
    "python3 -m mailcrawl --db ~/MailVault/db-v3-sandbox "
    "embed --account thehive --embedder gemma"
)
GEMMA_CONFIG = {"audio_config": None}
IMAGE_PROCESSING = {"image": {"max_soft_tokens": 280}}
_TABLE = {
    "classification": "vec_classification",
    "search": "vec_search",
}
VEC_IMAGE = "vec_image"


class FakeEmbedder:
    model_id = "fake-embedder"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        return [_hash_vector(text, self.dimension) for text in texts]

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        enter_model()
        return [_hash_vector(blob.hex(), self.dimension) for blob in images]


class ScriptedEmbedder:
    model_id = "scripted-embedder"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        return [_scripted_vector(text, self.dimension) for text in texts]

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        enter_model()
        return [_scripted_vector("image", self.dimension) for _blob in images]


class GemmaEmbedder:
    model_id = "google/embeddinggemma-2"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        model = self._gemma()
        raw = model.encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return _rows(self.model_id, raw)

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        enter_model()
        if not images:
            return []
        model = self._gemma()
        with tempfile.TemporaryDirectory() as tmp:
            batch = []
            for index, blob in enumerate(images):
                path = Path(tmp) / f"{index}{_image_suffix(blob)}"
                path.write_bytes(blob)
                batch.append({"image": str(path)})
            raw = model.encode(
                batch,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
                processing_kwargs=IMAGE_PROCESSING,
            )
        return _rows(self.model_id, raw)

    def _gemma(self):
        enter_model()
        loaded = getattr(self, "_loaded", None)
        if loaded is not None:
            return loaded
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
        self._loaded = model
        return model


class Embedder(Protocol):
    model_id: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_images(self, images: list[bytes]) -> list[list[float]]: ...


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
                config_kwargs=GEMMA_CONFIG,
                model_kwargs={"torch_dtype": dtype},
            )
        except Exception as exc:
            last = exc
    raise UsageError(
        f"could not load google/embeddinggemma-2 in bfloat16 or float32: {last}"
    )


def create_vec_tables(conn: sqlite3.Connection) -> None:
    statements = {
        name: f"""
            CREATE VIRTUAL TABLE {name} USING vec0(
              embedding float[{NATIVE_DIM}] distance_metric=cosine,
              +thread_pk integer,
              +prefix text,
              +dim integer
            )
        """
        for name in _TABLE.values()
    }
    statements[VEC_IMAGE] = f"""
        CREATE VIRTUAL TABLE {VEC_IMAGE} USING vec0(
          embedding float[{NATIVE_DIM}] distance_metric=cosine,
          +thread_pk integer,
          +att_pk integer,
          +prefix text,
          +dim integer
        )
    """
    for name, sql in statements.items():
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            (name,),
        ).fetchone()
        if found:
            continue
        conn.execute(sql)


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
    _embed_group(conn, embedder, config, grouped)
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
    _embed_group(conn, embedder, config, rows)


@dataclass(frozen=True)
class _KeptImage:
    thread_pk: int
    att_pk: int
    blob: bytes


def _embed_group(
    conn: sqlite3.Connection,
    embedder: Embedder,
    config: dict[str, bool] | None,
    grouped: list[tuple[int, str, str]],
) -> None:
    thread_ids = {row[0] for row in grouped}
    images = _kept_images(conn, thread_ids)
    texts: list[str] = []
    for _thread_pk, subject, body in grouped:
        texts.append(classification_text(body))
        texts.append(search_document(subject, body))
    blobs = [item.blob for item in images]

    result = invoke(
        "embeddings",
        lambda: (
            embedder.embed(texts),
            embedder.embed_images(blobs) if blobs else [],
        ),
        config,
    )
    if result is None:
        return
    text_vectors, image_vectors = result
    if len(text_vectors) != len(texts) or len(image_vectors) != len(blobs):
        raise ValueError("embedder returned the wrong number of vectors")
    for index, (thread_pk, _subject, _body) in enumerate(grouped):
        _upsert(conn, "classification", thread_pk, text_vectors[2 * index], CLASSIFICATION_PREFIX)
        _upsert(conn, "search", thread_pk, text_vectors[2 * index + 1], SEARCH_TITLE_PREFIX)
    by_thread: dict[int, list[list[float]]] = {}
    for item, vector in zip(images, image_vectors):
        _upsert_image(conn, item.thread_pk, item.att_pk, vector)
        by_thread.setdefault(item.thread_pk, []).append(vector)
    image_only = {
        int(row[0])
        for row in conn.execute("SELECT thread_pk FROM threads WHERE image_only = 1")
    }
    for thread_pk, vectors in by_thread.items():
        if thread_pk not in image_only:
            continue
        mean = _mean(vectors)
        _upsert(conn, "classification", thread_pk, mean, "")
        _upsert(conn, "search", thread_pk, mean, "")
    conn.commit()


def _kept_images(conn: sqlite3.Connection, thread_ids: set[int]) -> list[_KeptImage]:
    if not thread_ids:
        return []
    rows = conn.execute(
        """
        SELECT threads.thread_pk, attachments.att_pk, attachments.mime_type,
               attachments.is_inline, attachments.size_bytes, attachments.stored_path
        FROM attachments
        JOIN messages ON messages.msg_pk = attachments.msg_pk
        JOIN threads ON threads.thread_pk = messages.thread_pk
        ORDER BY threads.thread_pk, attachments.att_pk
        """
    ).fetchall()
    found: list[_KeptImage] = []
    for thread_pk, att_pk, mime, inline, size, stored in rows:
        if int(thread_pk) not in thread_ids or not stored:
            continue
        if not embeddable_image(mime, int(inline or 0), None if size is None else int(size)):
            continue
        path = Path(str(stored))
        if not path.is_file():
            continue
        found.append(_KeptImage(int(thread_pk), int(att_pk), path.read_bytes()))
    return found


def _upsert_image(
    conn: sqlite3.Connection,
    thread_pk: int,
    att_pk: int,
    vector: list[float],
) -> None:
    if len(vector) != NATIVE_DIM:
        raise ValueError(len(vector))
    conn.execute(f"DELETE FROM {VEC_IMAGE} WHERE att_pk = ?", (att_pk,))
    conn.execute(
        f"""
        INSERT INTO {VEC_IMAGE}(embedding, thread_pk, att_pk, prefix, dim)
        VALUES (?, ?, ?, ?, ?)
        """,
        (json.dumps(vector), thread_pk, att_pk, "", NATIVE_DIM),
    )


def _mean(vectors: list[list[float]]) -> list[float]:
    width = len(vectors[0])
    total = [0.0] * width
    for vector in vectors:
        for index, value in enumerate(vector):
            total[index] += value
    count = float(len(vectors))
    return truncate([value / count for value in total], width)


def _rows(model_id: str, raw: object) -> list[list[float]]:
    vectors: list[list[float]] = []
    for row in raw:
        values = [float(item) for item in list(row)]
        if len(values) != NATIVE_DIM:
            raise UsageError(f"{model_id} returned {len(values)} dimensions")
        vectors.append(truncate(values, NATIVE_DIM))
    return vectors


def _image_suffix(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG"):
        return ".png"
    if blob.startswith(b"\xff\xd8"):
        return ".jpg"
    if blob.startswith(b"GIF8"):
        return ".gif"
    if blob.startswith(b"RIFF") and b"WEBP" in blob[:16]:
        return ".webp"
    return ".img"
