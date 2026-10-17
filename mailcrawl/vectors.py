"""Optional vec0 tables. Weights load only inside a gated embed call."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mailcrawl.configfile import DEFAULT_BATCH_SIZE, DEFAULT_MAX_TOKENS
from mailcrawl.decisions import (
    DecisionDraft,
    current_decision,
    decide_open,
    eligible_thread_pks,
    open_for_model,
)
from mailcrawl.inference import BoundaryError, enabled_points, enter_model, invoke
from mailcrawl.paths import UsageError
from mailcrawl.threads import dimension_skip_reason, embeddable_image, image_size

NATIVE_DIM = 768
CLASSIFICATION_PREFIX = "task: classification | query: "
SEARCH_QUERY_PREFIX = "task: search result | query: "
SEARCH_TITLE_PREFIX = "title: "
LOCAL_VERIFY = (
    "python3 -m mailcrawl --db ~/MailVault/db-v3-sandbox "
    "embed --account thehive --embedder gemma"
)
MLX_MODEL_ID = "mlx-community/embeddinggemma-2-8bit"
MLX_LOCAL_VERIFY = (
    "python3 -m mailcrawl --db ~/MailVault/db-v3-sandbox "
    "bench --account thehive --embedders gemma,mlx --sample 32"
)
MLX_PROCESSOR_ERROR = (
    "transformers does not expose EmbeddingGemma2Processor. "
    "The image path stops instead of falling back to text-only. "
    "See https://huggingface.co/mlx-community/embeddinggemma-2-8bit. "
    f"local-verify: {MLX_LOCAL_VERIFY}"
)
GEMMA_CONFIG = {"audio_config": None}
IMAGE_PROCESSING = {"image": {"max_soft_tokens": 280}}
CHARS_PER_TOKEN = 4
_TABLE = {
    "classification": "vec_classification",
    "search": "vec_search",
}
VEC_IMAGE = "vec_image"


def truncate_text(text: str, max_tokens: int) -> str:
    words = text.split()
    cap = max_tokens * CHARS_PER_TOKEN
    if len(words) <= max_tokens and len(text) <= cap:
        return text
    kept = 0
    index = 0
    length = len(text)
    while index < length and kept < max_tokens:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        while index < length and not text[index].isspace():
            index += 1
        kept += 1
    clipped = text[:index]
    if len(clipped) > cap:
        return clipped[:cap]
    return clipped


def _max_tokens(embedder: object) -> int:
    value = getattr(embedder, "max_tokens", DEFAULT_MAX_TOKENS)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else DEFAULT_MAX_TOKENS


def _batch_size(embedder: object) -> int:
    value = getattr(embedder, "batch_size", DEFAULT_BATCH_SIZE)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else DEFAULT_BATCH_SIZE


def bounded_text_rows(embedder: object, texts: list[str], encode_chunk) -> list[list[float]]:
    cap = _max_tokens(embedder)
    size = _batch_size(embedder)
    clipped = [truncate_text(text, cap) for text in texts]
    rows: list[list[float]] = []
    batches: list[list[str]] = []
    for start in range(0, len(clipped), size):
        chunk = clipped[start : start + size]
        batches.append(chunk)
        try:
            rows.extend(encode_chunk(chunk))
        finally:
            _release_batch(embedder)
    embedder.batches = batches
    return rows


def bounded_image_rows(embedder: object, images: list[bytes], encode_one) -> list[list[float]]:
    rows: list[list[float]] = []
    seen: list[bytes] = []
    for blob in images:
        seen.append(blob)
        try:
            rows.extend(encode_one(blob))
        finally:
            _release_batch(embedder)
    embedder.image_batches = seen
    return rows


def _release_batch(embedder: object) -> None:
    release = getattr(embedder, "release_batch", None)
    if callable(release):
        release()


class FakeEmbedder:
    model_id = "fake-embedder"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        return bounded_text_rows(
            self,
            texts,
            lambda chunk: [_hash_vector(text, self.dimension) for text in chunk],
        )

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        enter_model()
        return bounded_image_rows(
            self,
            images,
            lambda blob: [_hash_vector(blob.hex(), self.dimension)],
        )


class ScriptedEmbedder:
    model_id = "scripted-embedder"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        return bounded_text_rows(
            self,
            texts,
            lambda chunk: [_scripted_vector(text, self.dimension) for text in chunk],
        )

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        enter_model()
        return bounded_image_rows(
            self,
            images,
            lambda _blob: [_scripted_vector("image", self.dimension)],
        )


class GemmaEmbedder:
    model_id = "google/embeddinggemma-2"
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        if not texts:
            self.batches = []
            return []
        model = self._gemma()
        model.max_seq_length = _max_tokens(self)

        def encode_chunk(chunk: list[str]) -> list[list[float]]:
            raw = _encode_guarded(model, list(chunk), _device_name(self), image=False)
            return _rows(self.model_id, raw)

        return bounded_text_rows(self, texts, encode_chunk)

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        enter_model()
        if not images:
            self.image_batches = []
            return []
        model = self._gemma()

        def encode_one(blob: bytes) -> list[list[float]]:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"0{_image_suffix(blob)}"
                path.write_bytes(blob)
                raw = _encode_guarded(
                    model,
                    [{"image": str(path)}],
                    _device_name(self),
                    image=True,
                )
            return _rows(self.model_id, raw)

        return bounded_image_rows(self, images, encode_one)

    def release_batch(self) -> None:
        enter_model()
        torch = sys.modules.get("torch")
        if torch is None:
            return
        _empty_device_cache(torch, _device_name(self))

    def _gemma(self):
        enter_model()
        loaded = getattr(self, "_loaded", None)
        if loaded is not None:
            return loaded
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise UsageError(
                "sentence-transformers[image]>=6.1 is not installed. "
                f"local-verify: {LOCAL_VERIFY}"
            ) from exc
        try:
            import torch
        except ImportError as exc:
            raise UsageError(
                "torch is not installed. "
                f"local-verify: {LOCAL_VERIFY}"
            ) from exc
        model = _load_gemma(SentenceTransformer, torch, _device_name(self))
        if getattr(model, "default_prompt_name", None) is not None:
            model.default_prompt_name = None
        self._loaded = model
        return model

    def prepare(self, *, images: bool) -> None:
        enter_model()
        self._gemma()


class MlxEmbedder:
    model_id = MLX_MODEL_ID
    dimension = NATIVE_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        enter_model()
        if not texts:
            self.batches = []
            return []
        model, tokenizer, mx = self._text_runtime()
        cap = _max_tokens(self)

        def encode_chunk(chunk: list[str]) -> list[list[float]]:
            inputs = tokenizer(
                list(chunk),
                padding=True,
                truncation=True,
                max_length=cap,
                return_tensors="np",
            )
            embeddings = model(**{key: mx.array(value) for key, value in inputs.items()}).text_embeds
            mx.eval(embeddings)
            return _rows(self.model_id, embeddings.tolist())

        return bounded_text_rows(self, texts, encode_chunk)

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        enter_model()
        if not images:
            self.image_batches = []
            return []
        model, processor, mx = self._vision_runtime()
        from PIL import Image

        def encode_one(blob: bytes) -> list[list[float]]:
            picture = Image.open(io.BytesIO(blob))
            picture.load()
            inputs = processor(images=[picture], return_tensors="np")
            embeddings = model(**{key: mx.array(value) for key, value in inputs.items()}).text_embeds
            mx.eval(embeddings)
            return _rows(self.model_id, embeddings.tolist())

        return bounded_image_rows(self, images, encode_one)

    def prepare(self, *, images: bool) -> None:
        enter_model()
        self._text_runtime()
        if images:
            self._vision_runtime()

    def _text_runtime(self):
        enter_model()
        loaded = getattr(self, "_text", None)
        if loaded is not None:
            return loaded
        try:
            import mlx.core as mx
            from mlx_vlm.embedding_loader import load_embedding_model
            from mlx_vlm.utils import get_model_path
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise UsageError(
                "the mlx extra is not installed. It is for Apple Silicon. "
                f"local-verify: {MLX_LOCAL_VERIFY}"
            ) from exc
        path = get_model_path(self.model_id)
        model = load_embedding_model(path)
        tokenizer = AutoTokenizer.from_pretrained(path)
        self._text = (model, tokenizer, mx)
        return self._text

    def _vision_runtime(self):
        enter_model()
        loaded = getattr(self, "_vision", None)
        if loaded is not None:
            return loaded
        processor_cls = _embedding_gemma_processor()
        try:
            import mlx.core as mx
            from mlx_vlm import load
        except ImportError as exc:
            raise UsageError(
                "the mlx extra is not installed. It is for Apple Silicon. "
                f"local-verify: {MLX_LOCAL_VERIFY}"
            ) from exc
        model, processor = load(self.model_id)
        if not isinstance(processor, processor_cls):
            raise UsageError(
                "mlx_vlm did not return EmbeddingGemma2Processor. "
                "The image path stops instead of falling back to text-only. "
                f"local-verify: {MLX_LOCAL_VERIFY}"
            )
        self._vision = (model, processor, mx)
        return self._vision


def _embedding_gemma_processor():
    enter_model()
    try:
        from transformers import EmbeddingGemma2Processor
    except ImportError as exc:
        raise UsageError(MLX_PROCESSOR_ERROR) from exc
    return EmbeddingGemma2Processor


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
    if name == "mlx":
        return MlxEmbedder()
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


def _load_gemma(sentence_transformer, torch, requested: str = "auto"):
    device = _resolve_device(torch, requested)
    dtypes = []
    if device in ("cuda", "mps"):
        dtypes.append(torch.bfloat16)
    dtypes.append(torch.float32)
    last: Exception | None = None
    for dtype in dtypes:
        try:
            return sentence_transformer(
                "google/embeddinggemma-2",
                device=device,
                config_kwargs=GEMMA_CONFIG,
                model_kwargs={"torch_dtype": dtype},
            )
        except Exception as exc:
            last = exc
    raise UsageError(
        f"could not load google/embeddinggemma-2 in bfloat16 or float32: {last}"
    )


def _device_name(embedder: object) -> str:
    value = getattr(embedder, "device", "auto")
    if value in ("auto", "mps", "cuda", "cpu"):
        return str(value)
    return "auto"


def _resolve_device(torch, requested: str) -> str:
    cuda = bool(getattr(getattr(torch, "cuda", None), "is_available", lambda: False)())
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    has_mps = mps is not None and bool(mps.is_available())
    if requested == "cpu":
        return "cpu"
    if requested == "cuda":
        if not cuda:
            raise UsageError("cuda is not available")
        return "cuda"
    if requested == "mps":
        if not has_mps:
            raise UsageError("mps is not available")
        return "mps"
    if requested != "auto":
        raise UsageError("device is auto, mps, cuda, or cpu")
    if cuda:
        return "cuda"
    if has_mps:
        return "mps"
    return "cpu"


def _encode_guarded(model, payload, device: str, *, image: bool):
    enter_model()
    torch = _optional_torch()
    scope = torch.inference_mode() if torch is not None else contextlib.nullcontext()
    with scope:
        kwargs = {
            "normalize_embeddings": True,
            "convert_to_numpy": True,
            "show_progress_bar": False,
        }
        if image:
            kwargs["processing_kwargs"] = IMAGE_PROCESSING
        raw = model.encode(payload, **kwargs)
        copied = [row for row in raw]
    del raw
    if torch is not None:
        _empty_device_cache(torch, device)
    return copied


def _optional_torch():
    enter_model()
    try:
        import torch
    except ImportError:
        return None
    return torch


def _empty_device_cache(torch, requested: str) -> None:
    device = _resolve_device(torch, requested)
    if device == "mps":
        empty = getattr(getattr(torch, "mps", None), "empty_cache", None)
        if callable(empty):
            empty()
    elif device == "cuda":
        empty = getattr(getattr(torch, "cuda", None), "empty_cache", None)
        if callable(empty):
            empty()


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
    written = 0
    for row in grouped:
        if _vectors_stored(conn, row[0]):
            continue
        if _embed_group(conn, embedder, config, [row]):
            written += 1
    return written


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
    embedder: Embedder | None,
    config: dict[str, bool] | None,
    now: int,
) -> sqlite3.Row | None:
    if "embeddings" not in enabled_points(config) or embedder is None:
        return None
    stored = current_decision(conn, thread_pk)
    if not open_for_model(stored):
        return stored
    require_vec(conn)
    _ensure_classification(conn, thread_pk, embedder, config)
    blob = _stored_embedding(conn, "classification", thread_pk)
    if blob is None:
        return stored
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
        return stored
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

    return decide_open(conn, thread_pk, now, produce)


def knn_account(
    conn: sqlite3.Connection,
    embedder: Embedder | None,
    config: dict[str, bool] | None,
    now: int,
) -> tuple[int, int, int]:
    eligible = eligible_thread_pks(conn)
    decided = 0
    review = 0
    for thread_pk in eligible:
        decision = knn_decide(conn, thread_pk, embedder, config, now)
        if decision is None or decision["next_state"] == "needs_review":
            review += 1
        else:
            decided += 1
    return len(eligible), decided, review


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
    if _vectors_stored(conn, thread_pk):
        return
    rows = [row for row in _threads(conn) if row[0] == thread_pk]
    if not rows:
        return
    _embed_group(conn, embedder, config, rows)


def _vectors_stored(conn: sqlite3.Connection, thread_pk: int) -> bool:
    return (
        _stored_embedding(conn, "classification", thread_pk) is not None
        and _stored_embedding(conn, "search", thread_pk) is not None
    )


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
) -> bool:
    wrote = False
    image_only = {
        int(row[0])
        for row in conn.execute("SELECT thread_pk FROM threads WHERE image_only = 1")
    }
    for thread_pk, subject, body in grouped:
        texts = [classification_text(body), search_document(subject, body)]
        try:
            text_vectors = invoke("embeddings", lambda texts=texts: embedder.embed(texts), config)
        except (UsageError, BoundaryError):
            raise
        except Exception as exc:
            _log_skip(f"thread {thread_pk}", exc)
            continue
        if text_vectors is None:
            continue
        if len(text_vectors) != 2:
            _log_skip(f"thread {thread_pk}", "embedder returned the wrong number of vectors")
            continue
        _upsert(conn, "classification", thread_pk, text_vectors[0], CLASSIFICATION_PREFIX)
        _upsert(conn, "search", thread_pk, text_vectors[1], SEARCH_TITLE_PREFIX)
        image_vectors: list[list[float]] = []
        for item in _kept_images(conn, {thread_pk}):
            try:
                got = invoke(
                    "embeddings",
                    lambda blob=item.blob: embedder.embed_images([blob]),
                    config,
                )
            except (UsageError, BoundaryError):
                raise
            except Exception as exc:
                _log_skip(f"image {item.att_pk}", exc)
                continue
            if not got:
                continue
            _upsert_image(conn, item.thread_pk, item.att_pk, got[0])
            image_vectors.append(got[0])
        if thread_pk in image_only and image_vectors:
            mean = _mean(image_vectors)
            _upsert(conn, "classification", thread_pk, mean, "")
            _upsert(conn, "search", thread_pk, mean, "")
        conn.commit()
        wrote = True
    return wrote


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
        path = Path(str(stored))
        if not path.is_file():
            continue
        blob = path.read_bytes()
        width, height = image_size(blob) or (None, None)
        if not embeddable_image(
            mime,
            int(inline or 0),
            None if size is None else int(size),
            width,
            height,
        ):
            reason = dimension_skip_reason(width, height)
            if reason:
                _log_skip(f"image {int(att_pk)}", reason)
            continue
        found.append(_KeptImage(int(thread_pk), int(att_pk), blob))
    return found


def _log_skip(item: str, reason: object) -> None:
    text = str(reason).splitlines()[0][:200]
    sys.stderr.write(f"skipped {item}: {text}\n")


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
