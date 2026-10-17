"""Phase 3 vectors stay off unless config turns the point on."""

import importlib.util
import json
import sqlite3
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path

from mailcrawl.cli import _embedder, _parser, main
from mailcrawl.configfile import (
    embed_limits,
    embedder_name,
    inference_config,
    inference_device,
    write_account,
)
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.inference import BoundaryError, enter_model, invoke
from mailcrawl.paths import UsageError
from mailcrawl.store import connect, index_message, init_account
from mailcrawl.ingest import import_mbox
from mailcrawl.threads import dimension_skip_reason, embeddable_image, image_gate, opened_limit_reason
from mailcrawl.vectors import (
    LOCAL_VERIFY,
    CLASSIFICATION_PREFIX,
    GEMMA_CONFIG,
    NATIVE_DIM,
    FakeEmbedder,
    GemmaEmbedder,
    ScriptedEmbedder,
    _load_gemma,
    _resolve_device,
    bounded_text_rows,
    embed_threads,
    knn_account,
    knn_decide,
    open_embedder,
    require_vec,
    truncate,
)
from mailcrawl.search import search_messages


def _sqlite_vec_installed() -> bool:
    return importlib.util.find_spec("sqlite_vec") is not None


def _solid_png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + (b"\x10\x20\x30" * width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


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
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
                self.assertNotIn("vec_search", names)
                self.assertNotIn("vec_classification", names)
                self.assertIsNone(knn_decide(conn, 1, embedder, None, 10))
            finally:
                conn.close()

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_scripted_fusion_flips_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                _seed(conn)
                embedder = CountingEmbedder(ScriptedEmbedder())
                flags = {"embeddings": True, "search-embedding": True}
                self.assertEqual(embed_threads(conn, embedder, flags), 5)
                stored_calls = embedder.calls
                self.assertEqual(embed_threads(conn, embedder, flags), 0)
                self.assertEqual(embedder.calls, stored_calls)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_search").fetchone()[0], 5)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_classification").fetchone()[0], 5)
                prefix, dim = conn.execute(
                    "SELECT prefix, dim FROM vec_classification WHERE thread_pk = ?",
                    (1,),
                ).fetchone()
                self.assertEqual(prefix, CLASSIFICATION_PREFIX)
                self.assertEqual(dim, NATIVE_DIM)
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'vec_classification'"
                ).fetchone()[0]
                self.assertIn("float[768]", sql)
                self.assertIn("cosine", sql)
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

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
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

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_truncation_matches_in_256_dimensions(self) -> None:
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
                require_vec(conn)
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

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_image_only_thread_copies_a_fake_neighbour(self) -> None:
        png = _solid_png(32, 32)
        encoded = __import__("base64").b64encode(png).decode("ascii")
        mbox = f"""From 1768000000000000401@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Text note
Date: Thu, 15 Jan 2026 12:00:00 +0000
Message-ID: <phase3b-text@example.com>
X-GM-THRID: 1768000000000000401
X-GM-MSGID: 1768000000000000401
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

alpha keyword text path stays on the classification prefix

From 1768000000000000402@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Image reviewed
Date: Thu, 15 Jan 2026 12:05:00 +0000
Message-ID: <phase3b-reviewed@example.com>
X-GM-THRID: 1768000000000000402
X-GM-MSGID: 1768000000000000402
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="imgbound"

--imgbound
Content-Type: text/plain; charset="utf-8"

scan

--imgbound
Content-Type: image/png; name="scan.png"
Content-Disposition: attachment; filename="scan.png"
Content-Transfer-Encoding: base64

{encoded}
--imgbound--

From 1768000000000000403@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Image target
Date: Thu, 15 Jan 2026 12:10:00 +0000
Message-ID: <phase3b-target@example.com>
X-GM-THRID: 1768000000000000403
X-GM-MSGID: 1768000000000000403
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="imgbound2"

--imgbound2
Content-Type: text/plain; charset="utf-8"

scan

--imgbound2
Content-Type: image/png; name="scan.png"
Content-Disposition: attachment; filename="scan.png"
Content-Transfer-Encoding: base64

{encoded}
--imgbound2--

From 1768000000000000404@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Logo note
Date: Thu, 15 Jan 2026 12:15:00 +0000
Message-ID: <phase3b-logo@example.com>
X-GM-THRID: 1768000000000000404
X-GM-MSGID: 1768000000000000404
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="logobound"

--logobound
Content-Type: text/plain; charset="utf-8"

logo

--logobound
Content-Type: image/png; name="logo.png"
Content-Disposition: inline; filename="logo.png"
Content-Transfer-Encoding: base64

{encoded}
--logobound--
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mbox_path = root / "images.mbox"
            mbox_path.write_text(mbox, encoding="utf-8")
            db_path = init_account(root, "personal", "ada@example.com")
            conn = connect(db_path)
            try:
                imported = import_mbox(
                    conn,
                    alias="personal",
                    mbox_path=mbox_path,
                    labels_path=None,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                )
                self.assertEqual(imported.n_new, 4)
                flags = {
                    row[0]: int(row[1])
                    for row in conn.execute(
                        """
                        SELECT messages.subject, threads.image_only
                        FROM messages JOIN threads ON threads.thread_pk = messages.thread_pk
                        """
                    )
                }
                self.assertEqual(flags["Text note"], 0)
                self.assertEqual(flags["Image reviewed"], 1)
                self.assertEqual(flags["Image target"], 1)
                self.assertEqual(flags["Logo note"], 0)
                stored = conn.execute(
                    """
                    SELECT messages.subject, attachments.stored_path
                    FROM attachments
                    JOIN messages ON messages.msg_pk = attachments.msg_pk
                    ORDER BY messages.subject
                    """
                ).fetchall()
                by_subject = {row[0]: row[1] for row in stored}
                self.assertIsNone(by_subject["Logo note"])
                self.assertTrue(Path(by_subject["Image reviewed"]).is_file())
                self.assertEqual(Path(by_subject["Image reviewed"]).read_bytes(), png)
                threads = {
                    row[1]: int(row[0])
                    for row in conn.execute("SELECT thread_pk, subject FROM messages")
                }
                decide_once(
                    conn,
                    threads["Image reviewed"],
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
                flags_on = {"embeddings": True}
                self.assertEqual(embed_threads(conn, FakeEmbedder(), flags_on), 4)
                text_prefix = conn.execute(
                    "SELECT prefix FROM vec_classification WHERE thread_pk = ?",
                    (threads["Text note"],),
                ).fetchone()[0]
                image_prefix = conn.execute(
                    "SELECT prefix FROM vec_classification WHERE thread_pk = ?",
                    (threads["Image target"],),
                ).fetchone()[0]
                self.assertEqual(text_prefix, CLASSIFICATION_PREFIX)
                self.assertEqual(image_prefix, "")
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_image").fetchone()[0], 2)
                first = knn_decide(conn, threads["Image target"], FakeEmbedder(), flags_on, 20)
                second = knn_decide(conn, threads["Image target"], FakeEmbedder(), flags_on, 30)
                self.assertEqual(first["origin"], "knn")
                self.assertEqual(first["source_detail"], "fake-embedder")
                self.assertEqual(first["next_state"], "reference")
                self.assertEqual(int(first["decision_id"]), int(second["decision_id"]))
                evidence = json.loads(first["evidence"])
                self.assertEqual(
                    evidence,
                    [{"distance": 0.0, "neighbour_thread_pk": threads["Image reviewed"]}],
                )
                self.assertEqual(embed_threads(conn, FakeEmbedder(), flags_on), 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_image").fetchone()[0], 2)
            finally:
                conn.close()


class VectorCliTest(unittest.TestCase):
    def test_omitted_embedder_resolves_to_gemma(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            self.assertEqual(embedder_name(config), "gemma")
            config.write_text("[inference]\nembeddings = true\n", encoding="utf-8")
            self.assertEqual(embedder_name(config), "gemma")
            for argv in (
                ["embed", "--account", "personal"],
                ["knn", "--account", "personal", "--thread", "synthetic"],
                ["search", "synthetic", "--account", "personal"],
            ):
                omitted = _parser().parse_args(["--config", str(config), *argv])
                self.assertIsNone(omitted.embedder)
                self.assertEqual(_embedder(omitted), "gemma")
            flagged = _parser().parse_args(
                [
                    "--config",
                    str(config),
                    "embed",
                    "--account",
                    "personal",
                    "--embedder",
                    "fake",
                ]
            )
            self.assertEqual(_embedder(flagged), "fake")
            config.write_text(
                "[inference]\nembeddings = true\nembedder = \"fake\"\n",
                encoding="utf-8",
            )
            self.assertEqual(embedder_name(config), "fake")
            named = _parser().parse_args(
                ["--config", str(config), "embed", "--account", "personal"]
            )
            self.assertEqual(_embedder(named), "fake")
            self.assertNotIn("sentence_transformers", sys.modules)
            self.assertNotIn("torch", sys.modules)

    def test_image_encode_uses_the_gemma_guide(self) -> None:
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082"
        )
        seen: dict[str, object] = {}

        class Recorder:
            def encode(self, batch, **kwargs):
                seen["samples"] = [
                    {
                        "keys": set(item),
                        "suffix": Path(item["image"]).suffix,
                        "bytes": Path(item["image"]).read_bytes(),
                    }
                    for item in batch
                ]
                seen["kwargs"] = kwargs
                return [[0.0] * NATIVE_DIM]

        embedder = GemmaEmbedder()
        embedder._loaded = Recorder()
        vectors = invoke(
            "embeddings",
            lambda: embedder.embed_images([png]),
            {"embeddings": True},
        )
        self.assertEqual(vectors, [[0.0] * NATIVE_DIM])
        self.assertEqual(
            seen["samples"],
            [{"keys": {"image"}, "suffix": ".png", "bytes": png}],
        )
        self.assertEqual(
            seen["kwargs"],
            {
                "normalize_embeddings": True,
                "convert_to_numpy": True,
                "show_progress_bar": False,
                "processing_kwargs": {"image": {"max_soft_tokens": 280}},
            },
        )

    def test_gemma_is_opt_in_and_db_v2_is_refused(self) -> None:
        embedder = open_embedder("gemma")
        self.assertIsInstance(embedder, GemmaEmbedder)
        self.assertEqual(embedder.model_id, "google/embeddinggemma-2")
        self.assertEqual(embedder.dimension, 768)
        self.assertEqual(GEMMA_CONFIG, {"audio_config": None})
        self.assertNotIn("vision_config", GEMMA_CONFIG)
        self.assertNotIn("db-v2", LOCAL_VERIFY)
        spec = Path(__file__).resolve().parents[1].joinpath("docs/SPEC.md").read_text(encoding="utf-8")
        self.assertIn("https://gemma.ai/models/embeddinggemma/multimodal/", spec)
        self.assertIn(LOCAL_VERIFY, spec)
        self.assertIn("vectors", spec)
        with self.assertRaises(BoundaryError):
            embedder.embed(["synthetic"])
        self.assertNotIn("sentence_transformers", sys.modules)
        if importlib.util.find_spec("sentence_transformers") is None:
            with self.assertRaises(UsageError) as missing:
                invoke(
                    "embeddings",
                    lambda: embedder.embed(["synthetic"]),
                    {"embeddings": True},
                )
            self.assertIn("sentence-transformers[image]>=6.1", str(missing.exception))
            self.assertIn(LOCAL_VERIFY, str(missing.exception))
            self.assertNotIn("sentence_transformers", sys.modules)
            self.assertNotIn("torch", sys.modules)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "config.toml"
            buf = StringIO()
            with redirect_stdout(buf):
                init = main(
                    [
                        "--json",
                        "--home",
                        str(home),
                        "--config",
                        str(config),
                        "init",
                        "--account",
                        "personal",
                        "--email",
                        "ada@example.com",
                    ]
                )
            self.assertEqual(init, 0, buf.getvalue())
            buf = StringIO()
            with redirect_stdout(buf):
                code = main(
                    [
                        "--json",
                        "--home",
                        str(home),
                        "--config",
                        str(config),
                        "embed",
                        "--account",
                        "personal",
                        "--embedder",
                        "gemma",
                    ]
                )
            self.assertEqual(code, 0, buf.getvalue())
            self.assertIn('"embedded": 0', buf.getvalue())
            self.assertNotIn("sentence_transformers", sys.modules)
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
                "[inference]\nembedder = \"gemma\"\nembeddings = true\nsearch-embedding = false\n"
                "batch_size = 4\nmax_tokens = 2048\n\n",
                encoding="utf-8",
            )
            write_account(path, "personal", "ada@example.com")
            flags = inference_config(path)
            self.assertEqual(flags, {"embeddings": True, "search-embedding": False})
            self.assertEqual(embed_limits(path), (4, 2048))
            self.assertEqual(path.read_text(encoding="utf-8").count('embedder = "gemma"'), 1)
            self.assertIn("batch_size = 4", path.read_text(encoding="utf-8"))
            self.assertIn("max_tokens = 2048", path.read_text(encoding="utf-8"))
            write_account(path, "personal", "ada@example.com")
            self.assertEqual(inference_config(path), flags)
            self.assertEqual(embed_limits(path), (4, 2048))


class BoundTest(unittest.TestCase):
    def test_fake_batches_stay_inside_the_bound_and_long_text_is_cut(self) -> None:
        embedder = FakeEmbedder()
        embedder.batch_size = 4
        embedder.max_tokens = 8
        long = " ".join(f"w{i:02d}" for i in range(30))
        rows = invoke("embeddings", lambda: embedder.embed([long] * 9), {"embeddings": True})
        self.assertEqual([len(chunk) for chunk in embedder.batches], [4, 4, 1])
        self.assertEqual(embedder.batches[0][0].split(), [f"w{i:02d}" for i in range(8)])
        self.assertEqual(len(rows), 9)
        blob = "x" * 1000
        invoke("embeddings", lambda: embedder.embed([blob]), {"embeddings": True})
        self.assertEqual(embedder.batches, [["x" * 32]])
        images = invoke(
            "embeddings",
            lambda: embedder.embed_images([b"aa", b"bb", b"cc"]),
            {"embeddings": True},
        )
        self.assertEqual(embedder.image_batches, [b"aa", b"bb", b"cc"])
        self.assertEqual(len(images), 3)
        short = invoke("embeddings", lambda: embedder.embed(["hello  world"]), {"embeddings": True})
        self.assertEqual(embedder.batches, [["hello  world"]])
        self.assertEqual(len(short), 1)

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_embed_and_search_pass_bounded_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                _seed(conn)
                body = " ".join(f"w{i:02d}" for i in range(30))
                conn.execute("UPDATE messages SET body_text = ?", (body,))
                conn.commit()
                class Remember(FakeEmbedder):
                    def __init__(self) -> None:
                        self.all: list[list[str]] = []

                    def embed(self, texts: list[str]) -> list[list[float]]:
                        rows = super().embed(texts)
                        self.all.extend(self.batches)
                        return rows

                embedder = Remember()
                embedder.batch_size = 4
                embedder.max_tokens = 8
                self.assertEqual(embed_threads(conn, embedder, {"embeddings": True}), 5)
                self.assertEqual([len(chunk) for chunk in embedder.all], [2, 2, 2, 2, 2])
                for chunk in embedder.all:
                    for text in chunk:
                        self.assertLessEqual(len(text.split()), 8)
                        self.assertLessEqual(len(text), 32)
                query = " ".join(f"q{i:02d}" for i in range(30))
                search_messages(
                    conn,
                    query,
                    embedder=embedder,
                    config={"search-embedding": True},
                    fusion="rrf",
                    k=1,
                )
                seen = embedder.batches[-1][0]
                self.assertLessEqual(len(seen.split()), 8)
                self.assertLessEqual(len(seen), 32)
                self.assertNotIn("q29", seen)
                self.assertTrue(seen.startswith("task: search result"))
            finally:
                conn.close()

    def test_usage_detail_is_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = StringIO()
            err = StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(
                    ["--home", tmp, "search", "hello", "--account", "personal"]
                )
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "usage\n")
            self.assertEqual(err.getvalue(), "account is not initialized\n")

    def test_default_batch_encodes_one_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[inference]\nembeddings = true\n", encoding="utf-8")
            self.assertEqual(embed_limits(path), (1, 2048))
        embedder = FakeEmbedder()
        rows = invoke(
            "embeddings",
            lambda: embedder.embed(["one", "two", "three"]),
            {"embeddings": True},
        )
        self.assertEqual(embedder.batches, [["one"], ["two"], ["three"]])
        self.assertEqual(len(rows), 3)

    def test_bad_batch_size_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[inference]\nbatch_size = 0\n", encoding="utf-8")
            with self.assertRaises(UsageError) as caught:
                embed_limits(path)
            self.assertEqual(str(caught.exception), "batch_size needs a positive count")


class FlatEmbedTest(unittest.TestCase):
    def test_allocations_stay_flat_across_items(self) -> None:
        class AllocEmbedder(FakeEmbedder):
            def __init__(self) -> None:
                self.live = 0
                self.high = 0

            def embed(self, texts: list[str]) -> list[list[float]]:
                enter_model()
                parent = FakeEmbedder()

                def encode(chunk: list[str]) -> list[list[float]]:
                    self.live += len(chunk)
                    self.high = max(self.high, self.live)
                    return parent.embed(chunk)

                return bounded_text_rows(self, texts, encode)

            def release_batch(self) -> None:
                self.live = 0

        embedder = AllocEmbedder()
        embedder.batch_size = 1
        texts = [f"synthetic item {index}" for index in range(64)]
        rows = invoke("embeddings", lambda: embedder.embed(texts), {"embeddings": True})
        self.assertEqual(len(rows), 64)
        self.assertEqual(embedder.high, 1)
        self.assertEqual(embedder.live, 0)

    def test_gemma_encode_uses_inference_mode_and_empties_the_cache(self) -> None:
        events: list[str] = []
        torch = types.ModuleType("torch")
        torch.inference_mode = lambda: events.append("inference_mode") or __import__("contextlib").nullcontext()
        torch.backends = types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: True)
        )
        torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: events.append("cuda"))
        torch.mps = types.SimpleNamespace(empty_cache=lambda: events.append("empty_cache"))
        torch.bfloat16 = "bf16"
        torch.float32 = "f32"

        class Model:
            def encode(self, batch, **kwargs):
                events.append(f"encode {len(batch)}")
                return [[0.0] * NATIVE_DIM for _ in batch]

        previous = sys.modules.get("torch")
        sys.modules["torch"] = torch
        try:
            embedder = GemmaEmbedder()
            embedder.device = "mps"
            embedder._loaded = Model()
            rows = invoke(
                "embeddings",
                lambda: embedder.embed(["one", "two"]),
                {"embeddings": True},
            )
        finally:
            if previous is None:
                sys.modules.pop("torch", None)
            else:
                sys.modules["torch"] = previous
        self.assertEqual(len(rows), 2)
        self.assertEqual(events.count("inference_mode"), 2)
        self.assertEqual(events.count("empty_cache"), 4)
        self.assertEqual([item for item in events if item.startswith("encode")], ["encode 1", "encode 1"])

    def test_auto_device_prefers_mps_when_cuda_is_absent(self) -> None:
        class Torch:
            bfloat16 = "bf16"
            float32 = "f32"

            class cuda:
                @staticmethod
                def is_available() -> bool:
                    return False

            class backends:
                class mps:
                    @staticmethod
                    def is_available() -> bool:
                        return True

        seen: dict[str, object] = {}

        def factory(*_args, **kwargs):
            seen["kwargs"] = kwargs
            return "model"

        self.assertEqual(_load_gemma(factory, Torch(), "auto"), "model")
        self.assertEqual(seen["kwargs"]["device"], "mps")
        self.assertEqual(seen["kwargs"]["model_kwargs"], {"torch_dtype": "bf16"})
        self.assertEqual(_resolve_device(Torch(), "cpu"), "cpu")
        with self.assertRaises(UsageError) as caught:
            _resolve_device(Torch(), "cuda")
        self.assertEqual(str(caught.exception), "cuda is not available")

    def test_device_is_config_and_stays_out_of_the_boolean_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[inference]\nembeddings = true\ndevice = "cpu"\nbatch_size = 1\n',
                encoding="utf-8",
            )
            self.assertEqual(inference_device(path), "cpu")
            self.assertEqual(inference_config(path), {"embeddings": True})
            self.assertEqual(embed_limits(path), (1, 2048))
            write_account(path, "personal", "ada@example.com")
            self.assertIn('device = "cpu"', path.read_text(encoding="utf-8"))
            self.assertEqual(inference_device(path), "cpu")
            path.write_text('[inference]\ndevice = "metal"\n', encoding="utf-8")
            with self.assertRaises(UsageError) as caught:
                inference_config(path)
            self.assertEqual(str(caught.exception), "device is auto, mps, cuda, or cpu")
            spec = Path(__file__).resolve().parents[1].joinpath("docs/SPEC.md").read_text(encoding="utf-8")
            self.assertIn("M4", spec)
            self.assertIn(LOCAL_VERIFY, spec)
            self.assertIn('device` is `auto`', spec)

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_a_failed_item_is_skipped_and_a_rerun_keeps_committed_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                gif = _still_gif(1, 1)
                pixel = Path(tmp) / "pixel.gif"
                pixel.write_bytes(gif)
                wide = Path(tmp) / "scan.png"
                wide.write_bytes(_solid_png(32, 32))
                narrow = Path(tmp) / "narrow.png"
                narrow.write_bytes(_solid_png(16, 16))
                self.assertEqual(
                    image_gate("image/gif", 0, len(gif), gif),
                    (False, "tracking pixel 1x1"),
                )
                self.assertEqual(
                    image_gate("image/png", 0, wide.stat().st_size, wide.read_bytes()),
                    (True, None),
                )
                self.assertFalse(embeddable_image("image/gif", 0, len(gif), 1, 1))
                self.assertEqual(dimension_skip_reason(1, 1), "tracking pixel 1x1")
                self.assertEqual(dimension_skip_reason(16, 16), "side 16x16 below 32")
                for path, mime in ((pixel, "image/gif"), (wide, "image/png"), (narrow, "image/png")):
                    conn.execute(
                        """
                        INSERT INTO attachments(
                          msg_pk, mime_type, size_bytes, is_inline, stored_path, extract_status
                        ) VALUES (?, ?, ?, 0, ?, 'skipped')
                        """,
                        (ids["alpha-msg"], mime, path.stat().st_size, str(path)),
                    )
                conn.commit()

                class FailFar(FakeEmbedder):
                    def embed(self, texts: list[str]) -> list[list[float]]:
                        if any("completely different" in text for text in texts):
                            raise RuntimeError("channel dimension is ambiguous")
                        return super().embed(texts)

                    def embed_images(self, images: list[bytes]) -> list[list[float]]:
                        if images and images[0] == wide.read_bytes():
                            raise RuntimeError("too many values to unpack")
                        return super().embed_images(images)

                err = StringIO()
                with redirect_stderr(err):
                    written = embed_threads(conn, FailFar(), {"embeddings": True})
                self.assertEqual(written, 4)
                log = err.getvalue()
                self.assertIn("skipped image", log)
                self.assertIn("tracking pixel 1x1", log)
                self.assertIn("side 16x16 below 32", log)
                self.assertIn("too many values to unpack", log)
                self.assertIn("channel dimension is ambiguous", log)
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT embedding FROM vec_classification WHERE thread_pk = ?",
                        (ids["alpha"],),
                    ).fetchone()
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT embedding FROM vec_classification WHERE thread_pk = ?",
                        (ids["far"],),
                    ).fetchone()
                )
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_image").fetchone()[0], 0)
                self.assertEqual(embed_threads(conn, FakeEmbedder(), {"embeddings": True}), 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_classification").fetchone()[0], 5)
                self.assertEqual(embed_threads(conn, FakeEmbedder(), {"embeddings": True}), 0)
            finally:
                conn.close()

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_knn_replaces_rule_review_and_leaves_human_and_settled_rows(self) -> None:
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
                    ids["target"],
                    10,
                    lambda: DecisionDraft(
                        next_state="needs_review",
                        lane="imbox",
                        rule_id="else",
                        rules_version="rules-v4",
                        origin="rule",
                        source_detail="else",
                    ),
                )
                decide_once(
                    conn,
                    ids["far"],
                    10,
                    lambda: DecisionDraft(
                        next_state="needs_review",
                        lane="imbox",
                        rule_id="else",
                        rules_version="rules-v4",
                        origin="human",
                        source_detail="review",
                    ),
                )
                decide_once(
                    conn,
                    ids["vec"],
                    10,
                    lambda: DecisionDraft(
                        next_state="reference",
                        lane="paper_trail",
                        rule_id="3",
                        rules_version="rules-v4",
                        origin="rule",
                        source_detail="3",
                    ),
                )
                first = knn_decide(conn, ids["target"], embedder, flags, 20)
                second = knn_decide(conn, ids["target"], embedder, flags, 30)
                self.assertEqual(first["origin"], "knn")
                self.assertEqual(first["next_state"], "reference")
                self.assertEqual(int(first["decision_id"]), int(second["decision_id"]))
                rows = conn.execute(
                    "SELECT origin, superseded_by IS NULL FROM decisions WHERE thread_pk = ? ORDER BY decision_id",
                    (ids["target"],),
                ).fetchall()
                self.assertEqual([(row[0], int(row[1])) for row in rows], [("rule", 0), ("knn", 1)])
                human = knn_decide(conn, ids["far"], embedder, flags, 40)
                self.assertEqual(human["origin"], "human")
                self.assertEqual(human["next_state"], "needs_review")
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM decisions WHERE thread_pk = ?",
                        (ids["far"],),
                    ).fetchone()[0],
                    1,
                )
                settled = knn_decide(conn, ids["vec"], embedder, flags, 40)
                self.assertEqual((settled["origin"], settled["next_state"]), ("rule", "reference"))
                self.assertIsNone(knn_decide(conn, ids["alpha"], embedder, None, 50))
                eligible, decided, review = knn_account(conn, embedder, flags, 60)
                self.assertEqual(eligible, 1)
                self.assertEqual(decided + review, 1)
                self.assertEqual(
                    conn.execute(
                        "SELECT origin FROM decisions WHERE thread_pk = ? AND superseded_by IS NULL",
                        (ids["far"],),
                    ).fetchone()[0],
                    "human",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT origin FROM decisions WHERE thread_pk = ? AND superseded_by IS NULL",
                        (ids["alpha"],),
                    ).fetchone()[0],
                    "knn",
                )
            finally:
                conn.close()

    def test_oversized_and_unknown_images_are_skipped_before_decode(self) -> None:
        huge_png = _png_header(12000, 12000)
        huge_gif = b"GIF89a" + struct.pack("<HH", 12000, 12000)
        wide_png = _png_stub(5000, 6000)
        unknown = b"not-an-image"
        small = _solid_png(32, 32)
        self.assertEqual(len(huge_png), 33)
        self.assertEqual(
            image_gate("image/png", 0, len(huge_png), huge_png),
            (False, "image size is unknown"),
        )
        self.assertEqual(
            image_gate("image/gif", 0, len(huge_gif), huge_gif),
            (False, "image size is unknown"),
        )
        kept, reason = image_gate("image/png", 0, len(wide_png), wide_png)
        self.assertEqual(kept, False)
        self.assertIn("decompression bomb", reason)
        self.assertEqual(
            image_gate("image/webp", 0, len(unknown), unknown),
            (False, "image size is unknown"),
        )
        self.assertEqual(image_gate("image/png", 0, len(small), small), (True, None))
        self.assertEqual(
            dimension_skip_reason(5000, 6000),
            "pixels 5000x6000 above 25000000",
        )
        self.assertIsNone(dimension_skip_reason(8192, 3051))
        self.assertEqual(
            dimension_skip_reason(8192, 3052),
            "pixels 8192x3052 above 25000000",
        )
        fake = _chunk(b"fAKE", struct.pack(">II", 64, 64))
        lying_png = _png_stub(20000, 1000, extra=fake)
        self.assertEqual(
            image_gate("image/png", 0, len(lying_png), lying_png),
            (False, "side 20000x1000 above 8192"),
        )
        later_frame = _gif_later_frame((64, 64), (6000, 6000))
        kept, reason = image_gate("image/gif", 0, len(later_frame), later_frame)
        self.assertEqual(kept, False)
        self.assertIn("decompression bomb", reason)
        under_cap = _gif_later_frame((64, 64), (4000, 4000))
        self.assertEqual(
            image_gate("image/gif", 0, len(under_cap), under_cap),
            (False, "frame 4000x4000 above screen 64x64"),
        )
        many = _gif_frames(200, (5000, 4000))
        budget = _gif_frames(64, (5000, 4000))
        self.assertEqual(
            image_gate("image/gif", 0, len(many), many),
            (False, "frames 200 above 64"),
        )
        self.assertEqual(
            image_gate("image/gif", 0, len(budget), budget),
            (False, "frame work 1280000000 above 100000000"),
        )
        allowed = _gif_frames(64, (32, 32))
        self.assertEqual(image_gate("image/gif", 0, len(allowed), allowed), (True, None))
        self.assertEqual(image_gate("image/gif", 0, len(_still_gif(64, 64)), _still_gif(64, 64)), (True, None))
        loads: list[tuple[int, int]] = []
        real_open = __import__("PIL.Image", fromlist=["Image"]).open

        def open_spy(handle, *args, **kwargs):
            picture = real_open(handle, *args, **kwargs)
            real_load = picture.load

            def load_spy(*load_args, **load_kwargs):
                loads.append(
                    (int(picture.size[0]), int(picture.size[1]), int(picture.tell()))
                )
                return real_load(*load_args, **load_kwargs)

            picture.load = load_spy
            return picture

        with patch("PIL.Image.open", open_spy):
            self.assertEqual(
                image_gate("image/gif", 0, len(many), many),
                (False, "frames 200 above 64"),
            )
            self.assertEqual(
                image_gate("image/gif", 0, len(budget), budget),
                (False, "frame work 1280000000 above 100000000"),
            )
            self.assertEqual(loads, [])
            self.assertEqual(
                image_gate("image/png", 0, len(lying_png), lying_png),
                (False, "side 20000x1000 above 8192"),
            )
            self.assertEqual(
                image_gate("image/gif", 0, len(under_cap), under_cap),
                (False, "frame 4000x4000 above screen 64x64"),
            )
            bmp = _bmp_header(32, 32)
            bomb = _bmp_header(6000, 5000)
            self.assertEqual(image_gate("image/webp", 0, len(bmp), bmp), (True, None))
            kept, reason = image_gate("image/tiff", 0, len(bomb), bomb)
            picture = real_open(BytesIO(lying_png))
            try:
                self.assertEqual(opened_limit_reason(picture), "side 20000x1000 above 8192")
            finally:
                picture.close()
        self.assertEqual(kept, False)
        self.assertIn("decompression bomb", reason)
        self.assertEqual(loads, [(64, 64, 0)])

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_embed_skips_a_bomb_and_keeps_a_normal_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "personal.db")
            try:
                ids = _seed(conn)
                root = Path(tmp)
                files = {
                    "huge.png": (_png_header(12000, 12000), "image/png"),
                    "huge.gif": (b"GIF89a" + struct.pack("<HH", 12000, 12000), "image/gif"),
                    "wide.png": (_png_stub(5000, 6000), "image/png"),
                    "lying.png": (
                        _png_stub(20000, 1000, extra=_chunk(b"fAKE", struct.pack(">II", 64, 64))),
                        "image/png",
                    ),
                    "frame.gif": (_gif_later_frame((64, 64), (6000, 6000)), "image/gif"),
                    "tile.gif": (_gif_later_frame((64, 64), (4000, 4000)), "image/gif"),
                    "many.gif": (_gif_frames(200, (5000, 4000)), "image/gif"),
                    "budget.gif": (_gif_frames(64, (5000, 4000)), "image/gif"),
                    "unknown.webp": (b"not-an-image", "image/webp"),
                    "scan.png": (_solid_png(32, 32), "image/png"),
                }
                if importlib.util.find_spec("PIL") is not None:
                    files["scan.bmp"] = (_bmp_header(32, 32), "image/webp")
                    files["bomb.bmp"] = (_bmp_header(6000, 5000), "image/tiff")
                for name, (blob, mime) in files.items():
                    path = root / name
                    path.write_bytes(blob)
                    conn.execute(
                        """
                        INSERT INTO attachments(
                          msg_pk, mime_type, size_bytes, is_inline, stored_path, extract_status
                        ) VALUES (?, ?, ?, 0, ?, 'skipped')
                        """,
                        (ids["alpha-msg"], mime, path.stat().st_size, str(path)),
                    )
                conn.commit()

                class RecordImages(FakeEmbedder):
                    def __init__(self) -> None:
                        self.seen: list[bytes] = []

                    def embed_images(self, images: list[bytes]) -> list[list[float]]:
                        self.seen.extend(images)
                        return super().embed_images(images)

                embedder = RecordImages()
                err = StringIO()
                with redirect_stderr(err):
                    self.assertEqual(embed_threads(conn, embedder, {"embeddings": True}), 5)
                log = err.getvalue()
                self.assertIn("image size is unknown", log)
                self.assertIn("decompression bomb", log)
                self.assertIn("side 20000x1000 above 8192", log)
                self.assertIn("frame 4000x4000 above screen 64x64", log)
                self.assertIn("frames 200 above 64", log)
                self.assertIn("frame work 1280000000 above 100000000", log)
                self.assertIn(files["scan.png"][0], embedder.seen)
                self.assertNotIn(files["huge.png"][0], embedder.seen)
                self.assertNotIn(files["huge.gif"][0], embedder.seen)
                self.assertNotIn(files["wide.png"][0], embedder.seen)
                self.assertNotIn(files["unknown.webp"][0], embedder.seen)
                self.assertNotIn(files["lying.png"][0], embedder.seen)
                self.assertNotIn(files["frame.gif"][0], embedder.seen)
                self.assertNotIn(files["tile.gif"][0], embedder.seen)
                self.assertNotIn(files["many.gif"][0], embedder.seen)
                self.assertNotIn(files["budget.gif"][0], embedder.seen)
                if "bomb.bmp" in files:
                    self.assertIn("decompression bomb", log)
                    self.assertIn(files["scan.bmp"][0], embedder.seen)
                    self.assertNotIn(files["bomb.bmp"][0], embedder.seen)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM vec_image").fetchone()[0],
                    2 if "scan.bmp" in files else 1,
                )
            finally:
                conn.close()

    @unittest.skipUnless(_sqlite_vec_installed(), "sqlite-vec is not installed")
    def test_knn_leaves_a_model_row_and_a_knn_row(self) -> None:
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
                model = decide_once(
                    conn,
                    ids["target"],
                    10,
                    lambda: DecisionDraft(
                        next_state="needs_review",
                        lane="imbox",
                        rule_id="harness",
                        rules_version="harness",
                        origin="model",
                        source_detail="harness-fixture",
                    ),
                )
                knn = decide_once(
                    conn,
                    ids["far"],
                    10,
                    lambda: DecisionDraft(
                        next_state="reference",
                        lane="paper_trail",
                        rule_id="3",
                        rules_version="rules-v4",
                        origin="knn",
                        source_detail="fake-embedder",
                    ),
                )
                stayed_model = knn_decide(conn, ids["target"], embedder, flags, 20)
                stayed_knn = knn_decide(conn, ids["far"], embedder, flags, 20)
                eligible, decided, review = knn_account(conn, embedder, flags, 30)
                self.assertEqual(int(stayed_model["decision_id"]), int(model["decision_id"]))
                self.assertEqual(
                    (stayed_model["origin"], stayed_model["rule_id"], stayed_model["next_state"]),
                    ("model", "harness", "needs_review"),
                )
                self.assertEqual(int(stayed_knn["decision_id"]), int(knn["decision_id"]))
                self.assertEqual((stayed_knn["origin"], stayed_knn["rule_id"]), ("knn", "3"))
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM decisions WHERE thread_pk = ?",
                        (ids["target"],),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM decisions WHERE thread_pk = ?",
                        (ids["far"],),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(eligible, 2)
                self.assertEqual(decided + review, 2)
                model_row = conn.execute(
                    """
                    SELECT origin, rule_id FROM decisions
                    WHERE thread_pk = ? AND superseded_by IS NULL
                    """,
                    (ids["target"],),
                ).fetchone()
                knn_row = conn.execute(
                    """
                    SELECT origin, rule_id FROM decisions
                    WHERE thread_pk = ? AND superseded_by IS NULL
                    """,
                    (ids["far"],),
                ).fetchone()
                self.assertEqual((model_row[0], model_row[1]), ("model", "harness"))
                self.assertEqual((knn_row[0], knn_row[1]), ("knn", "3"))
            finally:
                conn.close()


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _png_header(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)


def _png_stub(width: int, height: int, extra: bytes = b"") -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + extra
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"\x00"))
        + _chunk(b"IEND", b"")
    )


def _gif_frames(count: int, screen: tuple[int, int]) -> bytes:
    image_mod = __import__("PIL.Image", fromlist=["Image"])
    palette = [0, 0, 0, 255, 0, 0] + [0] * (256 * 3 - 6)
    first = image_mod.new("P", (8, 8), 0)
    first.putpalette(palette)
    rest = []
    for index in range(1, count):
        frame = image_mod.new("P", (8, 8), index % 2)
        frame.putpalette(palette)
        rest.append(frame)
    buffer = BytesIO()
    first.save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=rest,
        duration=1,
        loop=0,
        disposal=2,
    )
    raw = bytearray(buffer.getvalue())
    raw[6:10] = struct.pack("<HH", screen[0], screen[1])
    return bytes(raw)


def _still_gif(width: int, height: int) -> bytes:
    image = __import__("PIL.Image", fromlist=["Image"]).new("P", (width, height))
    buffer = BytesIO()
    image.save(buffer, format="GIF")
    return buffer.getvalue()


def _gif_later_frame(screen: tuple[int, int], frame: tuple[int, int]) -> bytes:
    image_mod = __import__("PIL.Image", fromlist=["Image"])
    first = image_mod.new("P", screen, 0)
    second = image_mod.new("P", screen, 1)
    palette = [0, 0, 0, 255, 0, 0] + [0] * (256 * 3 - 6)
    first.putpalette(palette)
    second.putpalette(palette)
    buffer = BytesIO()
    first.save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=50,
        loop=0,
        disposal=2,
    )
    raw = bytearray(buffer.getvalue())
    positions: list[int] = []
    start = 0
    while True:
        found = raw.find(b"\x2c", start)
        if found < 0:
            break
        positions.append(found)
        start = found + 1
    raw[positions[1] + 5 : positions[1] + 9] = struct.pack("<HH", frame[0], frame[1])
    return bytes(raw)


def _bmp_header(width: int, height: int) -> bytes:
    row = ((min(width, 4) * 3 + 3) // 4) * 4
    pixels = b"\x00" * row
    offset = 54
    header = b"BM" + struct.pack("<IHHI", offset + len(pixels), 0, 0, offset)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 0, 0, 0, 0)
    return header + dib + pixels
