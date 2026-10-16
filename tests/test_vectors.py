"""Phase 3 vectors stay off unless config turns the point on."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from mailcrawl.cli import main
from mailcrawl.configfile import inference_config, write_account
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.store import connect, index_message
from mailcrawl.vectors import (
    LOCAL_VERIFY,
    CLASSIFICATION_PREFIX,
    NATIVE_DIM,
    FakeEmbedder,
    ScriptedEmbedder,
    embed_threads,
    knn_decide,
    open_embedder,
    truncate,
)
from mailcrawl.search import search_messages


class CountingEmbedder:
    def __init__(self, inner: ScriptedEmbedder | FakeEmbedder) -> None:
        self.inner = inner
        self.calls = 0
        self.model_id = inner.model_id
        self.dimension = inner.dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return self.inner.embed(texts)


def _seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute(
        "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
    )
    account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
    rows = (
        ("alpha", "t-alpha", "m-alpha", "Alpha keyword", "alpha keyword"),
        ("vec", "t-vec", "m-vec", "Vec only", "vec-only marker"),
        ("near", "t-near", "m-near", "Neighbour reviewed", "same neighbour text"),
        ("target", "t-target", "m-target", "Neighbour target", "same neighbour text"),
        ("far", "t-far", "m-far", "Far note", "completely different archive note"),
    )
    ids: dict[str, int] = {}
    for offset, (key, thread_id, message_id, subject, body) in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO threads(account_id, gmail_thread_id, msg_count)
            VALUES (?, ?, 1)
            """,
            (account_id, thread_id),
        )
        thread_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, subject,
              body_text, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, message_id, thread_pk, offset, subject, body, f"sha-{key}", f"prov-{key}", 1),
        )
        msg_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        index_message(conn, msg_pk, subject, body, "ada@example.com", "")
        ids[key] = thread_pk
        ids[f"{key}-msg"] = msg_pk
    conn.commit()
    return ids


class VectorSearchTest(unittest.TestCase):
    def test_off_search_matches_keyword_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                _seed(conn)
                embedder = CountingEmbedder(ScriptedEmbedder())
                plain = search_messages(conn, "alpha")
                gated = search_messages(
                    conn,
                    "alpha",
                    embedder=embedder,
                    config=None,
                    fusion="rrf",
                    vec_weight=100,
                    k=1,
                )
                self.assertEqual(embedder.calls, 0)
                self.assertEqual(
                    [(hit["gmail_message_id"], hit["source"]) for hit in gated],
                    [(hit["gmail_message_id"], hit["source"]) for hit in plain],
                )
                self.assertEqual(gated, [{"gmail_message_id": "m-alpha", "snippet": "Alpha keyword alpha keyword", "source": "fts", "subject": "Alpha keyword"}])
                self.assertEqual(embed_threads(conn, embedder, None), 0)
                self.assertEqual(embedder.calls, 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_search").fetchone()[0], 0)
                self.assertIsNone(knn_decide(conn, 1, embedder, None, 10))
            finally:
                conn.close()

    def test_scripted_fusion_flips_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                _seed(conn)
                embedder = CountingEmbedder(ScriptedEmbedder())
                flags = {"embeddings": True, "search-embedding": True}
                self.assertEqual(embed_threads(conn, embedder, flags), 5)
                self.assertEqual(embed_threads(conn, embedder, flags), 5)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_search").fetchone()[0], 5)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_classification").fetchone()[0], 5)
                prefix, dim = conn.execute(
                    "SELECT prefix, dim FROM vec_classification WHERE thread_pk = ?",
                    (1,),
                ).fetchone()
                self.assertEqual(prefix, CLASSIFICATION_PREFIX)
                self.assertEqual(dim, NATIVE_DIM)
                keyword = search_messages(
                    conn,
                    "alpha",
                    embedder=embedder,
                    config=flags,
                    fusion="keyword",
                    k=1,
                )
                fused = search_messages(
                    conn,
                    "alpha",
                    embedder=embedder,
                    config=flags,
                    fusion="rrf",
                    keyword_weight=1,
                    vec_weight=100,
                    k=1,
                )
                self.assertEqual(
                    [(hit["gmail_message_id"], hit["source"]) for hit in keyword],
                    [("m-alpha", "fts"), ("m-vec", "vec")],
                )
                self.assertEqual(
                    [(hit["gmail_message_id"], hit["source"]) for hit in fused],
                    [("m-vec", "fused"), ("m-alpha", "fused")],
                )
                self.assertGreater(embedder.calls, 0)
            finally:
                conn.close()

    def test_fake_knn_copies_the_near_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                flags = {"embeddings": True}
                embedder = FakeEmbedder()
                self.assertEqual(embed_threads(conn, embedder, flags), 5)
                decide_once(
                    conn,
                    ids["near"],
                    10,
                    lambda: DecisionDraft(
                        next_state="reference",
                        lane="paper_trail",
                        rule_id="3",
                        rules_version="rules-v4",
                        origin="human",
                        source_detail="review",
                    ),
                )
                decide_once(
                    conn,
                    ids["far"],
                    10,
                    lambda: DecisionDraft(
                        next_state="noise",
                        lane="feed",
                        rule_id="else",
                        rules_version="rules-v4",
                        origin="human",
                        source_detail="review",
                    ),
                )
                first = knn_decide(conn, ids["target"], embedder, flags, 20)
                second = knn_decide(conn, ids["target"], embedder, flags, 30)
                self.assertIsNotNone(first)
                self.assertEqual(int(first["decision_id"]), int(second["decision_id"]))
                self.assertEqual(first["origin"], "knn")
                self.assertEqual(first["source"], "embedding-cluster")
                self.assertEqual(first["source_detail"], "fake-embedder")
                self.assertEqual(first["next_state"], "reference")
                self.assertEqual(first["lane"], "paper_trail")
                self.assertEqual(first["rule_id"], "3")
                evidence = json.loads(first["evidence"])
                self.assertEqual(evidence, [{"distance": 0.0, "neighbour_thread_pk": ids["near"]}])
                count = conn.execute(
                    "SELECT count(*) FROM decisions WHERE thread_pk = ?",
                    (ids["target"],),
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                conn.close()

    def test_truncation_matches_in_256_dimensions(self) -> None:
        from mailcrawl.inference import invoke

        flags = {"embeddings": True}
        embedder = FakeEmbedder()
        full = invoke("embeddings", lambda: embedder.embed(["synthetic thread"]), flags)
        other = invoke("embeddings", lambda: embedder.embed(["other synthetic thread"]), flags)
        short = truncate(full[0], 256)
        far = truncate(other[0], 256)
        self.assertEqual(len(short), 256)
        self.assertAlmostEqual(sum(value * value for value in short), 1.0, places=6)
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE vec_trunc USING vec0(
                      embedding float[256] distance_metric=cosine,
                      +thread_pk integer
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO vec_trunc(embedding, thread_pk) VALUES (?, ?)",
                    (json.dumps(short), 1),
                )
                conn.execute(
                    "INSERT INTO vec_trunc(embedding, thread_pk) VALUES (?, ?)",
                    (json.dumps(far), 2),
                )
                rows = conn.execute(
                    """
                    SELECT thread_pk, distance FROM vec_trunc
                    WHERE embedding MATCH ? AND k = 1
                    """,
                    (json.dumps(short),),
                ).fetchall()
                self.assertEqual([(int(rows[0][0]), float(rows[0][1]))], [(1, 0.0)])
                stored = conn.execute(
                    "SELECT dim FROM vec_classification"
                ).fetchone()
                self.assertIsNone(stored)
            finally:
                conn.close()


class VectorCliTest(unittest.TestCase):
    def test_gemma_is_local_verify_and_db_v2_is_refused(self) -> None:
        with self.assertRaises(Exception) as caught:
            open_embedder("gemma")
        self.assertEqual(str(caught.exception), f"local-verify: {LOCAL_VERIFY}")
        self.assertNotIn("db-v2", LOCAL_VERIFY)
        spec = Path(__file__).resolve().parents[1].joinpath("docs/SPEC.md").read_text(encoding="utf-8")
        self.assertIn(LOCAL_VERIFY, spec)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            buf = StringIO()
            with redirect_stdout(buf):
                code = main(
                    [
                        "--json",
                        "--home",
                        str(home),
                        "embed",
                        "--account",
                        "personal",
                        "--embedder",
                        "gemma",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn(LOCAL_VERIFY, buf.getvalue())
            self.assertNotIn("sentence_transformers", buf.getvalue())
            refused = home / "db-v2"
            buf = StringIO()
            with redirect_stdout(buf):
                code = main(
                    [
                        "--json",
                        "--db",
                        str(refused),
                        "embed",
                        "--account",
                        "personal",
                        "--embedder",
                        "fake",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("db-v2", buf.getvalue())
            self.assertFalse(refused.exists())

    def test_write_account_keeps_inference_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[inference]\nembeddings = true\nsearch-embedding = false\n\n",
                encoding="utf-8",
            )
            write_account(path, "personal", "ada@example.com")
            flags = inference_config(path)
            self.assertEqual(flags, {"embeddings": True, "search-embedding": False})
            write_account(path, "personal", "ada@example.com")
            self.assertEqual(inference_config(path), flags)
