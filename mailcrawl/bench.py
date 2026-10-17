"""Compare embedders on a read-only archive and a temporary vector copy."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from mailcrawl.configfile import DEFAULT_BATCH_SIZE, DEFAULT_MAX_TOKENS
from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.inference import invoke
from mailcrawl.paths import UsageError
from mailcrawl.threads import embeddable_image, image_size
from mailcrawl.vectors import (
    classification_text,
    open_embedder,
    _kept_images,
    _mean,
    _threads,
)

_ON = {"embeddings": True}
LABEL_TRUTH_VERIFY = (
    "python -m mailcrawl --json --db ~/MailVault/db-v4-sandbox "
    "bench --account thehive --embedders gemma,mlx --sample 64 "
    "--truth labels --taxonomy ~/MailVault/taxonomy.thehive.json"
)
_TRUTHS = frozenset({"decisions", "labels"})
_VOTE_K = 10
_LAYERS = (1, 2)


@dataclass(frozen=True)
class LabelPath:
    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.segments or any(segment == "" for segment in self.segments):
            raise ValueError("a label path needs segments")

    @staticmethod
    def parse(name: str) -> LabelPath | None:
        segments = tuple(segment.strip() for segment in name.split("/") if segment.strip())
        if not segments:
            return None
        return LabelPath(segments)

    @property
    def layer1(self) -> str:
        return self.segments[0]

    @property
    def full(self) -> str:
        return "/".join(self.segments)


@dataclass(frozen=True)
class ThreadRow:
    thread_pk: int
    subject: str
    body: str
    image_only: bool
    has_images: bool
    human_label: str | None
    categories: frozenset[str] = frozenset()
    paths: frozenset[LabelPath] = frozenset()


@dataclass(frozen=True)
class Drawn:
    texts: tuple[tuple[int, str], ...]
    images: tuple[tuple[int, int, bytes], ...]
    image_only: frozenset[int]
    human: dict[int, str]
    categories: dict[int, frozenset[str]]
    truth: str
    paths: dict[int, frozenset[LabelPath]]


@dataclass(frozen=True)
class Measured:
    name: str
    model_id: str
    load_s: float
    peak_rss_mb: float
    texts: int
    images: int
    texts_per_min: float
    images_per_min: float
    thread_ids: tuple[int, ...]
    thread_vectors: tuple[tuple[float, ...], ...]
    item_vectors: tuple[tuple[float, ...], ...]


def run_bench(
    source: Path,
    names: list[str],
    sample: int,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    truth: str = "decisions",
    label_categories: dict[str, frozenset[str]] | None = None,
) -> dict[str, object]:
    if sample < 1:
        raise UsageError("sample needs a positive count")
    if not names:
        raise UsageError("bench needs an embedder")
    if truth not in _TRUTHS:
        raise UsageError("truth is decisions or labels")
    if truth == "labels" and label_categories is None:
        raise UsageError("labels truth needs a taxonomy")
    if any("db-v2" in part.lower() for part in source.parts):
        raise UsageError("refusing a path that includes db-v2")
    if not source.is_file():
        raise UsageError("account is not initialized")
    before = _digest(source)
    drawn = _draw(source, sample, truth, label_categories or {})
    if _digest(source) != before:
        raise UsageError("bench wrote the source database")
    measured: list[Measured] = []
    stored: list[tuple[str, Measured]] = []
    for name in names:
        one = _measure(name, drawn, batch_size, max_tokens)
        measured.append(one)
        stored.append((name, one))
    vector_db = _write_copy(source, stored)
    if _digest(source) != before:
        raise UsageError("bench wrote the source database")
    return _document(sample, drawn, measured, vector_db)


def select_sample(rows: list[ThreadRow], sample: int, *, truth: str = "decisions") -> list[ThreadRow]:
    if truth == "labels":
        return _select_paths(rows, sample)
    ordered = sorted(rows, key=lambda row: row.thread_pk)
    image_rows = [row for row in ordered if row.image_only or row.has_images]
    image_rows.sort(key=lambda row: (not row.image_only, row.thread_pk))
    quota = min(len(image_rows), max(1, sample // 4)) if image_rows else 0
    picked: list[ThreadRow] = []
    seen: set[int] = set()
    for row in image_rows[:quota]:
        picked.append(row)
        seen.add(row.thread_pk)
    buckets: dict[str, list[ThreadRow]] = {}
    for row in ordered:
        if row.human_label is None or row.thread_pk in seen:
            continue
        buckets.setdefault(row.human_label, []).append(row)
    labels = sorted(buckets)
    while len(picked) < sample and any(buckets[label] for label in labels):
        for label in labels:
            bucket = buckets[label]
            if not bucket or len(picked) >= sample:
                continue
            row = bucket.pop(0)
            picked.append(row)
            seen.add(row.thread_pk)
    for row in ordered:
        if len(picked) >= sample:
            break
        if row.thread_pk in seen:
            continue
        picked.append(row)
        seen.add(row.thread_pk)
    return picked


def _select_paths(rows: list[ThreadRow], sample: int) -> list[ThreadRow]:
    ordered = [row for row in sorted(rows, key=lambda row: row.thread_pk) if row.paths]
    image_rows = [row for row in ordered if row.image_only or row.has_images]
    image_rows.sort(key=lambda row: (not row.image_only, row.thread_pk))
    quota = min(len(image_rows), max(1, sample // 4)) if image_rows else 0
    picked: list[ThreadRow] = []
    seen: set[int] = set()
    for row in image_rows[:quota]:
        picked.append(row)
        seen.add(row.thread_pk)
    pool = [row for row in ordered if row.thread_pk not in seen]
    while len(picked) < sample and pool:
        layer1_counts = _node_counts(pool, 1)
        rare_layer1 = min(layer1_counts, key=lambda node: (layer1_counts[node], node))
        group = [row for row in pool if rare_layer1 in _nodes(row.paths, 1)]
        layer2_counts: dict[str, int] = {}
        for row in group:
            for path in row.paths:
                if path.layer1 != rare_layer1:
                    continue
                layer2_counts[path.full] = layer2_counts.get(path.full, 0) + 1
        rare_layer2 = min(layer2_counts, key=lambda node: (layer2_counts[node], node))
        choice = next(row for row in group if any(path.full == rare_layer2 for path in row.paths))
        picked.append(choice)
        seen.add(choice.thread_pk)
        pool = [row for row in pool if row.thread_pk != choice.thread_pk]
    return picked


def _nodes(paths: frozenset[LabelPath], layer: int) -> frozenset[str]:
    if layer == 1:
        return frozenset(path.layer1 for path in paths)
    if layer == 2:
        return frozenset(path.full for path in paths)
    raise UsageError("label layers are 1 and 2")


def _node_counts(rows: list[ThreadRow], layer: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for node in _nodes(row.paths, layer):
            counts[node] = counts.get(node, 0) + 1
    return counts


def _draw(
    source: Path,
    sample: int,
    truth: str,
    label_categories: dict[str, frozenset[str]],
) -> Drawn:
    conn = _open_ro(source)
    try:
        image_only_ids = {
            int(row[0])
            for row in conn.execute("SELECT thread_pk FROM threads WHERE image_only = 1")
        }
        with_images = _threads_with_images(conn)
        human = {
            int(row[0]): str(row[1])
            for row in conn.execute(
                """
                SELECT thread_pk, next_state FROM decisions
                WHERE origin = 'human' AND superseded_by IS NULL
                """
            )
        }
        labelled = _label_sets(conn, label_categories) if truth == "labels" else {}
        blank = (frozenset(), frozenset())
        rows = []
        for thread_pk, subject, body in _threads(conn):
            paths, categories = labelled.get(thread_pk, blank)
            rows.append(
                ThreadRow(
                    thread_pk,
                    subject,
                    body,
                    thread_pk in image_only_ids,
                    thread_pk in with_images,
                    human.get(thread_pk),
                    categories,
                    paths,
                )
            )
        chosen = select_sample(rows, sample, truth=truth)
        ids = {row.thread_pk for row in chosen}
        images = tuple(
            (item.thread_pk, item.att_pk, item.blob)
            for item in _kept_images(conn, ids)
        )
        texts = tuple((row.thread_pk, classification_text(row.body)) for row in chosen)
        image_only = frozenset(row.thread_pk for row in chosen if row.image_only)
        chosen_human = {
            row.thread_pk: row.human_label
            for row in chosen
            if row.human_label is not None
        }
        chosen_categories = {
            row.thread_pk: row.categories for row in chosen if row.categories
        }
        chosen_paths = {row.thread_pk: row.paths for row in chosen if row.paths}
    finally:
        conn.close()
    return Drawn(
        texts,
        images,
        image_only,
        chosen_human,
        chosen_categories,
        truth,
        chosen_paths,
    )


def _label_sets(
    conn: sqlite3.Connection,
    mapping: dict[str, frozenset[str]],
) -> dict[int, tuple[frozenset[LabelPath], frozenset[str]]]:
    rows = conn.execute(
        f"""
        SELECT messages.thread_pk, labels.name
        FROM message_labels
        JOIN messages ON messages.msg_pk = message_labels.msg_pk
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
        WHERE {GMAIL_LABEL_SQL}
        """
    ).fetchall()
    found_paths: dict[int, set[LabelPath]] = {}
    found_categories: dict[int, set[str]] = {}
    for thread_pk, name in rows:
        categories = mapping.get(str(name), frozenset())
        path = LabelPath.parse(str(name))
        if not categories or path is None:
            continue
        key = int(thread_pk)
        found_paths.setdefault(key, set()).add(path)
        found_categories.setdefault(key, set()).update(categories)
    return {
        thread_pk: (frozenset(found_paths[thread_pk]), frozenset(found_categories[thread_pk]))
        for thread_pk in found_paths
    }


def _threads_with_images(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        """
        SELECT threads.thread_pk, attachments.mime_type, attachments.is_inline,
               attachments.size_bytes, attachments.stored_path
        FROM attachments
        JOIN messages ON messages.msg_pk = attachments.msg_pk
        JOIN threads ON threads.thread_pk = messages.thread_pk
        """
    ).fetchall()
    found: set[int] = set()
    for thread_pk, mime, inline, size, stored in rows:
        path = Path(str(stored)) if stored else None
        if path is None or not path.is_file():
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
            continue
        found.add(int(thread_pk))
    return found


def _measure(name: str, drawn: Drawn, batch_size: int, max_tokens: int) -> Measured:
    payload = {
        "batch_size": batch_size,
        "image_only": sorted(drawn.image_only),
        "images": [
            [thread_pk, att_pk, base64.b64encode(blob).decode("ascii")]
            for thread_pk, att_pk, blob in drawn.images
        ],
        "max_tokens": max_tokens,
        "name": name,
        "texts": [[thread_pk, text] for thread_pk, text in drawn.texts],
    }
    descriptor, out_path = tempfile.mkstemp(prefix="mailcrawl-bench-", suffix=".json")
    os.close(descriptor)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mailcrawl.bench", out_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stderr = proc.communicate(json.dumps(payload))[1]
        if proc.returncode != 0:
            detail = (stderr or "").strip() or "bench embedder failed"
            raise UsageError(detail)
        raw = json.loads(Path(out_path).read_text(encoding="utf-8"))
    finally:
        Path(out_path).unlink(missing_ok=True)
    text_vectors = [tuple(vector) for vector in raw["text_vectors"]]
    image_vectors = [tuple(vector) for vector in raw["image_vectors"]]
    return _assemble(
        name,
        str(raw["model_id"]),
        float(raw["load_s"]),
        ru_maxrss_mb(int(raw["ru_maxrss"]), sys.platform),
        float(raw["text_s"]),
        float(raw["image_s"]),
        drawn,
        text_vectors,
        image_vectors,
    )


def _measure_here(name: str, embedder: object, drawn: Drawn) -> dict[str, object]:
    load_s = 0.0
    prepare = getattr(embedder, "prepare", None)
    if prepare is not None:
        started = time.perf_counter()
        invoke("embeddings", lambda: prepare(images=bool(drawn.images)), _ON)
        load_s = time.perf_counter() - started
    texts = [text for _pk, text in drawn.texts]
    started = time.perf_counter()
    text_vectors = invoke("embeddings", lambda: embedder.embed(texts), _ON)
    text_s = time.perf_counter() - started
    blobs = [blob for _thread, _att, blob in drawn.images]
    started = time.perf_counter()
    image_vectors = invoke(
        "embeddings",
        lambda: embedder.embed_images(blobs) if blobs else [],
        _ON,
    )
    image_s = time.perf_counter() - started
    if text_vectors is None or image_vectors is None:
        raise UsageError("bench embeddings point did not run")
    if len(text_vectors) != len(texts) or len(image_vectors) != len(blobs):
        raise UsageError(f"{name} returned the wrong number of vectors")
    return {
        "image_s": image_s,
        "image_vectors": image_vectors,
        "load_s": load_s,
        "model_id": str(embedder.model_id),
        "ru_maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "text_s": text_s,
        "text_vectors": text_vectors,
    }


def _assemble(
    name: str,
    model_id: str,
    load_s: float,
    peak_rss_mb: float,
    text_s: float,
    image_s: float,
    drawn: Drawn,
    text_vectors: list[tuple[float, ...]],
    image_vectors: list[tuple[float, ...]],
) -> Measured:
    by_thread: dict[int, list[list[float]]] = {}
    for (thread_pk, _att, _blob), vector in zip(drawn.images, image_vectors):
        by_thread.setdefault(thread_pk, []).append(list(vector))
    thread_ids: list[int] = []
    thread_vectors: list[tuple[float, ...]] = []
    for index, (thread_pk, _text) in enumerate(drawn.texts):
        thread_ids.append(thread_pk)
        if thread_pk in drawn.image_only and by_thread.get(thread_pk):
            thread_vectors.append(tuple(_mean(by_thread[thread_pk])))
        else:
            thread_vectors.append(text_vectors[index])
    items = tuple(text_vectors) + tuple(image_vectors)
    return Measured(
        name=name,
        model_id=model_id,
        load_s=load_s,
        peak_rss_mb=peak_rss_mb,
        texts=len(drawn.texts),
        images=len(drawn.images),
        texts_per_min=_rate(len(drawn.texts), text_s),
        images_per_min=_rate(len(drawn.images), image_s),
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
            "peak_rss_mb": row.peak_rss_mb,
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
    labels = []
    for row in measured:
        if drawn.truth == "labels":
            score = score_label_layers(
                row.thread_ids,
                row.thread_vectors,
                drawn.paths,
                drawn.categories,
            )
            score["embedder"] = row.name
            labels.append(score)
            continue
        compared, matched, accuracy = _accuracy(row, drawn.human)
        decisions.append(
            {
                "accuracy": accuracy,
                "compared": compared,
                "embedder": row.name,
                "matched": matched,
            }
        )
    markdown = _markdown(embedders, pairs, decisions, labels)
    counts: dict[str, int] = {}
    for categories in drawn.categories.values():
        for category in categories:
            counts[category] = counts.get(category, 0) + 1
    report: dict[str, object] = {
        "embedders": embedders,
        "markdown": markdown,
        "ok": True,
        "pairs": pairs,
        "n_image_only": len(drawn.image_only),
        "n_with_images": len({thread_pk for thread_pk, _att, _blob in drawn.images}),
        "sample": sample,
        "sampled_images": len(drawn.images),
        "sampled_texts": len(drawn.texts),
        "source_read_only": True,
        "truth": drawn.truth,
        "vector_db": str(vector_db),
    }
    if drawn.truth == "labels":
        report["labels"] = labels
        report["n_labelled"] = len(drawn.paths)
        report["per_category"] = dict(sorted(counts.items()))
        report["per_layer"] = [
            {"counts": _count_nodes(drawn.paths, layer), "layer": layer}
            for layer in _LAYERS
        ]
    else:
        report["decisions"] = decisions
        report["n_human"] = len(drawn.human)
    return report


def _markdown(
    embedders: list[dict[str, object]],
    pairs: list[dict[str, object]],
    decisions: list[dict[str, object]],
    labels: list[dict[str, object]],
) -> str:
    if labels:
        lines = [
            "| embedder | load_s | texts/min | images/min | peak_rss_mb |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in embedders:
            lines.append(
                "| {name} | {load_s:.3f} | {texts_per_min:.3f} | {images_per_min:.3f} | {peak_rss_mb:.3f} |".format(
                    name=row["name"],
                    load_s=float(row["load_s"]),
                    texts_per_min=float(row["texts_per_min"]),
                    images_per_min=float(row["images_per_min"]),
                    peak_rss_mb=float(row["peak_rss_mb"]),
                )
            )
        lines.extend(
            [
                "",
                "| embedder | layer | overlap@1 | jaccard@1 | vote_f1@10 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for score in labels:
            views: list[tuple[str, dict[str, object]]] = [
                (str(layer["layer"]), layer)
                for layer in score["layers"]
            ]
            views.append(("categories", score["categories"]))
            for layer_name, view in views:
                lines.append(
                    "| {name} | {layer} | {overlap} | {jaccard} | {vote} |".format(
                        name=score["embedder"],
                        layer=layer_name,
                        overlap=_cell(view["overlap_at_1"]),
                        jaccard=_cell(view["jaccard_at_1"]),
                        vote=_cell(view["vote_f1_at_10"]),
                    )
                )
    else:
        lines = [
            "| embedder | load_s | texts/min | images/min | peak_rss_mb | human_accuracy |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row, decision in zip(embedders, decisions):
            lines.append(
                "| {name} | {load_s:.3f} | {texts_per_min:.3f} | {images_per_min:.3f} | {peak_rss_mb:.3f} | {accuracy} |".format(
                    name=row["name"],
                    load_s=float(row["load_s"]),
                    texts_per_min=float(row["texts_per_min"]),
                    images_per_min=float(row["images_per_min"]),
                    peak_rss_mb=float(row["peak_rss_mb"]),
                    accuracy=_accuracy_cell(decision),
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


def score_label_layers(
    thread_ids: tuple[int, ...] | list[int],
    vectors: tuple[tuple[float, ...], ...] | list[tuple[float, ...]],
    paths: dict[int, frozenset[LabelPath]],
    categories: dict[int, frozenset[str]],
) -> dict[str, object]:
    layers = []
    for layer in _LAYERS:
        nodes = {thread_pk: _nodes(found, layer) for thread_pk, found in paths.items()}
        scored = label_scores(thread_ids, vectors, nodes)
        scored["layer"] = layer
        layers.append(scored)
    return {
        "categories": label_scores(thread_ids, vectors, categories),
        "layers": layers,
    }


def _count_nodes(paths: dict[int, frozenset[LabelPath]], layer: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for found in paths.values():
        for node in _nodes(found, layer):
            counts[node] = counts.get(node, 0) + 1
    return dict(sorted(counts.items()))


def label_scores(
    thread_ids: tuple[int, ...] | list[int],
    vectors: tuple[tuple[float, ...], ...] | list[tuple[float, ...]],
    categories: dict[int, frozenset[str]],
) -> dict[str, object]:
    labelled = [index for index, thread_pk in enumerate(thread_ids) if thread_pk in categories]
    overlaps = 0
    jaccards: list[float] = []
    tp = 0
    fp = 0
    fn = 0
    recall_hits: dict[str, int] = {}
    recall_total: dict[str, int] = {}
    compared = 0
    for index in labelled:
        neighbours = _labelled_neighbours(thread_ids, vectors, categories, index)
        if not neighbours:
            continue
        compared += 1
        neighbour = neighbours[0]
        own = categories[thread_ids[index]]
        other = categories[neighbour]
        if own & other:
            overlaps += 1
        union = own | other
        jaccards.append(len(own & other) / len(union) if union else 0.0)
        voted = _vote(neighbours[:_VOTE_K], categories)
        tp += len(voted & own)
        fp += len(voted - own)
        fn += len(own - voted)
        for category in own:
            recall_total[category] = recall_total.get(category, 0) + 1
            if category in voted:
                recall_hits[category] = recall_hits.get(category, 0) + 1
    overlap = None if compared == 0 else overlaps / compared
    jaccard = None if not jaccards else sum(jaccards) / len(jaccards)
    if compared == 0:
        vote = None
    elif tp == 0:
        vote = 0.0
    else:
        vote = (2 * tp) / (2 * tp + fp + fn)
    recall = {
        category: recall_hits.get(category, 0) / recall_total[category]
        for category in sorted(recall_total)
    }
    return {
        "compared": compared,
        "jaccard_at_1": jaccard,
        "overlap_at_1": overlap,
        "recall": recall,
        "vote_f1_at_10": vote,
    }


def _labelled_neighbours(
    thread_ids: tuple[int, ...] | list[int],
    vectors: tuple[tuple[float, ...], ...] | list[tuple[float, ...]],
    categories: dict[int, frozenset[str]],
    index: int,
) -> list[int]:
    thread_pk = thread_ids[index]
    ranked: list[tuple[float, int]] = []
    for other_index, other in enumerate(thread_ids):
        if other == thread_pk or other not in categories:
            continue
        ranked.append((_cosine(vectors[index], vectors[other_index]), other))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [other for _score, other in ranked]


def _vote(neighbours: list[int], categories: dict[int, frozenset[str]]) -> frozenset[str]:
    if not neighbours:
        return frozenset()
    votes: dict[str, int] = {}
    for thread_pk in neighbours:
        for category in categories[thread_pk]:
            votes[category] = votes.get(category, 0) + 1
    width = len(neighbours)
    return frozenset(category for category, count in votes.items() if count * 2 > width)


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


def ru_maxrss_mb(ru_maxrss: int, platform: str) -> float:
    if platform == "darwin":
        return ru_maxrss / (1024 * 1024)
    return ru_maxrss / 1024


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


def _accuracy_cell(decision: dict[str, object]) -> str:
    count = int(decision["compared"])
    if decision["accuracy"] is None:
        return f"n/a (n={count})"
    return f"{float(decision['accuracy']):.3f} (n={count})"


def _cell(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.3f}"


def _child() -> None:
    if len(sys.argv) != 2:
        sys.stderr.write("bench child needs an output path\n")
        raise SystemExit(1)
    out_path = sys.argv[1]
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        payload = json.loads(sys.stdin.read())
        images = tuple(
            (int(thread_pk), int(att_pk), base64.b64decode(blob))
            for thread_pk, att_pk, blob in payload["images"]
        )
        drawn = Drawn(
            texts=tuple((int(thread_pk), str(text)) for thread_pk, text in payload["texts"]),
            images=images,
            image_only=frozenset(int(thread_pk) for thread_pk in payload["image_only"]),
            human={},
            categories={},
            truth="decisions",
            paths={},
        )
        embedder = open_embedder(str(payload["name"]))
        embedder.batch_size = int(payload["batch_size"])
        embedder.max_tokens = int(payload["max_tokens"])
        raw = _measure_here(str(payload["name"]), embedder, drawn)
        Path(out_path).write_text(json.dumps(raw), encoding="utf-8")
    except Exception as exc:
        sys.stderr.write(f"{exc}\n")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    _child()
