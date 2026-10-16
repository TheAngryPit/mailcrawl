"""Compare embedders on a read-only archive and a temporary vector copy."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from mailcrawl.inference import invoke
from mailcrawl.paths import UsageError
from mailcrawl.vectors import (
    classification_text,
    open_embedder,
    _kept_images,
    _mean,
    _threads,
)

_ON = {"embeddings": True}


@dataclass(frozen=True)
class Drawn:
    texts: tuple[tuple[int, str], ...]
    images: tuple[tuple[int, int, bytes], ...]
    image_only: frozenset[int]
    human: dict[int, str]


@dataclass(frozen=True)
class Measured:
    name: str
    model_id: str
    load_s: float
    peak_rss_bytes: int
    texts: int
    images: int
    texts_per_min: float
    images_per_min: float
    thread_ids: tuple[int, ...]
    thread_vectors: tuple[tuple[float, ...], ...]
    item_vectors: tuple[tuple[float, ...], ...]


def run_bench(source: Path, names: list[str], sample: int) -> dict[str, object]:
    if sample < 1:
        raise UsageError("sample needs a positive count")
    if not names:
        raise UsageError("bench needs an embedder")
    if any("db-v2" in part.lower() for part in source.parts):
        raise UsageError("refusing a path that includes db-v2")
    if not source.is_file():
        raise UsageError("account is not initialized")
    before = _digest(source)
    drawn = _draw(source, sample)
    if _digest(source) != before:
        raise UsageError("bench wrote the source database")
    measured: list[Measured] = []
    stored: list[tuple[str, Measured]] = []
    for name in names:
        embedder = open_embedder(name)
        one = _measure(name, embedder, drawn)
        measured.append(one)
        stored.append((name, one))
    vector_db = _write_copy(source, stored)
    if _digest(source) != before:
        raise UsageError("bench wrote the source database")
    return _document(sample, drawn, measured, vector_db)


def _draw(source: Path, sample: int) -> Drawn:
    conn = _open_ro(source)
    try:
        grouped = sorted(_threads(conn), key=lambda row: row[0])[:sample]
        ids = {row[0] for row in grouped}
        images = tuple(
            (item.thread_pk, item.att_pk, item.blob)
            for item in _kept_images(conn, ids)
        )
        image_only = frozenset(
            int(row[0])
            for row in conn.execute(
                "SELECT thread_pk FROM threads WHERE image_only = 1"
            )
            if int(row[0]) in ids
        )
        human = {
            int(row[0]): str(row[1])
            for row in conn.execute(
                """
                SELECT thread_pk, next_state FROM decisions
                WHERE origin = 'human' AND superseded_by IS NULL
                """
            )
            if int(row[0]) in ids
        }
        texts = tuple((row[0], classification_text(row[2])) for row in grouped)
    finally:
        conn.close()
    return Drawn(texts, images, image_only, human)


def _measure(name: str, embedder: object, drawn: Drawn) -> Measured:
    rss = [_rss_bytes()]
    load_s = 0.0
    prepare = getattr(embedder, "prepare", None)
    if prepare is not None:
        started = time.perf_counter()
        invoke("embeddings", lambda: prepare(images=bool(drawn.images)), _ON)
        load_s = time.perf_counter() - started
        rss.append(_rss_bytes())
    texts = [text for _pk, text in drawn.texts]
    started = time.perf_counter()
    text_vectors = invoke("embeddings", lambda: embedder.embed(texts), _ON)
    text_s = time.perf_counter() - started
    rss.append(_rss_bytes())
    blobs = [blob for _thread, _att, blob in drawn.images]
    started = time.perf_counter()
    image_vectors = invoke(
        "embeddings",
        lambda: embedder.embed_images(blobs) if blobs else [],
        _ON,
    )
    image_s = time.perf_counter() - started
    rss.append(_rss_bytes())
    if text_vectors is None or image_vectors is None:
        raise UsageError("bench embeddings point did not run")
    if len(text_vectors) != len(texts) or len(image_vectors) != len(blobs):
        raise UsageError(f"{name} returned the wrong number of vectors")
    by_thread: dict[int, list[list[float]]] = {}
    for (thread_pk, _att, _blob), vector in zip(drawn.images, image_vectors):
        by_thread.setdefault(thread_pk, []).append(vector)
    thread_ids: list[int] = []
    thread_vectors: list[tuple[float, ...]] = []
    for index, (thread_pk, _text) in enumerate(drawn.texts):
        thread_ids.append(thread_pk)
        if thread_pk in drawn.image_only and by_thread.get(thread_pk):
            thread_vectors.append(tuple(_mean(by_thread[thread_pk])))
        else:
            thread_vectors.append(tuple(text_vectors[index]))
    items = tuple(tuple(vector) for vector in list(text_vectors) + list(image_vectors))
    return Measured(
        name=name,
        model_id=str(embedder.model_id),
        load_s=load_s,
        peak_rss_bytes=max(rss),
        texts=len(texts),
        images=len(blobs),
        texts_per_min=_rate(len(texts), text_s),
        images_per_min=_rate(len(blobs), image_s),
        thread_ids=tuple(thread_ids),
        thread_vectors=tuple(thread_vectors),
        item_vectors=items,
    )


def _document(
    sample: int,
    drawn: Drawn,
    measured: list[Measured],
    vector_db: Path,
) -> dict[str, object]:
    embedders = [
        {
            "images": row.images,
            "images_per_min": row.images_per_min,
            "load_s": row.load_s,
            "model_id": row.model_id,
            "name": row.name,
            "peak_rss_bytes": row.peak_rss_bytes,
            "texts": row.texts,
            "texts_per_min": row.texts_per_min,
        }
        for row in measured
    ]
    pairs = []
    for left_index, left in enumerate(measured):
        for right in measured[left_index + 1 :]:
            cosines = [
                _cosine(a, b)
                for a, b in zip(left.item_vectors, right.item_vectors)
            ]
            pairs.append(
                {
                    "cosine_mean": sum(cosines) / len(cosines) if cosines else None,
                    "cosine_min": min(cosines) if cosines else None,
                    "knn_top10_overlap": _overlap(left, right),
                    "left": left.name,
                    "right": right.name,
                }
            )
    decisions = []
    for row in measured:
        compared, matched, accuracy = _accuracy(row, drawn.human)
        decisions.append(
            {
                "accuracy": accuracy,
                "compared": compared,
                "embedder": row.name,
                "matched": matched,
            }
        )
    markdown = _markdown(embedders, pairs, decisions)
    return {
        "decisions": decisions,
        "embedders": embedders,
        "markdown": markdown,
        "ok": True,
        "pairs": pairs,
        "sample": sample,
        "sampled_images": len(drawn.images),
        "sampled_texts": len(drawn.texts),
        "source_read_only": True,
        "vector_db": str(vector_db),
    }


def _markdown(embedders: list[dict[str, object]], pairs: list[dict[str, object]], decisions: list[dict[str, object]]) -> str:
    lines = [
        "| embedder | load_s | texts/min | images/min | peak_rss_bytes | human_accuracy |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row, decision in zip(embedders, decisions):
        lines.append(
            "| {name} | {load_s:.3f} | {texts_per_min:.3f} | {images_per_min:.3f} | {peak_rss_bytes} | {accuracy} |".format(
                name=row["name"],
                load_s=float(row["load_s"]),
                texts_per_min=float(row["texts_per_min"]),
                images_per_min=float(row["images_per_min"]),
                peak_rss_bytes=row["peak_rss_bytes"],
                accuracy=_cell(decision["accuracy"]),
            )
        )
    lines.extend(
        [
            "",
            "| pair | cosine_min | cosine_mean | knn_top10_overlap |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in pairs:
        lines.append(
            "| {left} vs {right} | {cosine_min} | {cosine_mean} | {overlap} |".format(
                left=row["left"],
                right=row["right"],
                cosine_min=_cell(row["cosine_min"]),
                cosine_mean=_cell(row["cosine_mean"]),
                overlap=_cell(row["knn_top10_overlap"]),
            )
        )
    return "\n".join(lines) + "\n"


def _accuracy(row: Measured, human: dict[int, str]) -> tuple[int, int, float | None]:
    compared = 0
    matched = 0
    for index, thread_pk in enumerate(row.thread_ids):
        if thread_pk not in human:
            continue
        best: int | None = None
        best_score: float | None = None
        for other_index, other in enumerate(row.thread_ids):
            if other == thread_pk or other not in human:
                continue
            score = _cosine(row.thread_vectors[index], row.thread_vectors[other_index])
            if best is None or best_score is None or score > best_score or (score == best_score and other < best):
                best = other
                best_score = score
        if best is None:
            continue
        compared += 1
        if human[best] == human[thread_pk]:
            matched += 1
    if compared == 0:
        return 0, 0, None
    return compared, matched, matched / compared


def _overlap(left: Measured, right: Measured) -> float:
    right_top = _top(right)
    scores: list[float] = []
    for thread_pk, neighbours in _top(left).items():
        other = right_top[thread_pk]
        width = max(len(neighbours), len(other), 1)
        scores.append(len(set(neighbours) & set(other)) / width)
    if not scores:
        return 1.0
    return sum(scores) / len(scores)


def _top(row: Measured) -> dict[int, list[int]]:
    found: dict[int, list[int]] = {}
    for index, thread_pk in enumerate(row.thread_ids):
        scored: list[tuple[float, int]] = []
        for other_index, other in enumerate(row.thread_ids):
            if other == thread_pk:
                continue
            scored.append((_cosine(row.thread_vectors[index], row.thread_vectors[other_index]), other))
        scored.sort(key=lambda item: (-item[0], item[1]))
        found[thread_pk] = [other for _score, other in scored[:10]]
    return found


def _write_copy(source: Path, stored: list[tuple[str, Measured]]) -> Path:
    dest_dir = Path(tempfile.mkdtemp(prefix="mailcrawl-bench-"))
    dest = dest_dir / source.name
    source_conn = _open_ro(source)
    target = sqlite3.connect(dest)
    try:
        source_conn.backup(target)
        target.execute(
            """
            CREATE TABLE bench_vectors (
              embedder TEXT NOT NULL,
              kind TEXT NOT NULL,
              item_key TEXT NOT NULL,
              dim INTEGER NOT NULL,
              vector TEXT NOT NULL,
              PRIMARY KEY (embedder, kind, item_key)
            )
            """
        )
        for offset, (name, row) in enumerate(stored):
            label = f"{name}#{offset}"
            for index, vector in enumerate(row.item_vectors):
                target.execute(
                    """
                    INSERT INTO bench_vectors (embedder, kind, item_key, dim, vector)
                    VALUES (?, 'item', ?, ?, ?)
                    """,
                    (label, str(index), len(vector), json.dumps(vector)),
                )
        target.commit()
    finally:
        target.close()
        source_conn.close()
    return dest


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _rss_bytes() -> int:
    raw = subprocess.check_output(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        text=True,
    )
    return int(raw.strip().splitlines()[0]) * 1024


def _rate(count: int, seconds: float) -> float:
    if count == 0:
        return 0.0
    return count / max(seconds, 1e-9) * 60.0


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / ((left_norm ** 0.5) * (right_norm ** 0.5))


def _cell(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.3f}"
