"""Phase 3 vectors stay off unless config turns the point on."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from mailcrawl.cli import main
from mailcrawl.configfile import inference_config, write_account
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.inference import BoundaryError, invoke
from mailcrawl.paths import UsageError
from mailcrawl.store import connect, index_message, init_account
from mailcrawl.ingest import import_mbox
from mailcrawl.vectors import (
    LOCAL_VERIFY,
    CLASSIFICATION_PREFIX,
    GEMMA_CONFIG,
    NATIVE_DIM,
    FakeEmbedder,
    GemmaEmbedder,
    ScriptedEmbedder,
    embed_threads,
    knn_decide,
    open_embedder,
    require_vec,
    truncate,
)
from mailcrawl.search import search_messages


def _sqlite_vec_installed() -> bool:
    return importlib.util.find_spec("sqlite_vec") is not None


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
                self.assertEqual(embed_threads(conn, embedder, flags), 5)
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
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082"
        )
        encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4z8AAAAMBAQAY3Y20AAAAAElFTkSuQmCC"
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
                self.assertEqual(embed_threads(conn, FakeEmbedder(), flags_on), 4)
                self.assertEqual(conn.execute("SELECT count(*) FROM vec_image").fetchone()[0], 2)
            finally:
                conn.close()


class VectorCliTest(unittest.TestCase):
    def test_omitted_embedder_resolves_to_gemma(self) -> None:
        mbox_text = """From 1768000000000000501@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Text note
Date: Thu, 15 Jan 2026 12:00:00 +0000
Message-ID: <default-embedder@example.com>
X-GM-THRID: 1768000000000000501
X-GM-MSGID: 1768000000000000501
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

alpha keyword

"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "config.toml"
            mbox = home / "one.mbox"
            mbox.write_text(mbox_text, encoding="utf-8")
            self.assertEqual(
                main(
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
                ),
                0,
            )
            config.write_text(
                config.read_text(encoding="utf-8") + "\n[inference]\nembeddings = true\n",
                encoding="utf-8",
            )
            self.assertEqual(
                main(
                    [
                        "--json",
                        "--home",
                        str(home),
                        "--config",
                        str(config),
                        "import",
                        "mbox",
                        str(mbox),
                        "--account",
                        "personal",
                    ]
                ),
                0,
            )
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
                    ]
                )
            self.assertEqual(code, 2, buf.getvalue())
            self.assertIn("sentence-transformers>=6.1", buf.getvalue())
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
            self.assertIn("sentence-transformers>=6.1", str(missing.exception))
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
                "[inference]\nembeddings = true\nsearch-embedding = false\n\n",
                encoding="utf-8",
            )
            write_account(path, "personal", "ada@example.com")
            flags = inference_config(path)
            self.assertEqual(flags, {"embeddings": True, "search-embedding": False})
            write_account(path, "personal", "ada@example.com")
            self.assertEqual(inference_config(path), flags)
