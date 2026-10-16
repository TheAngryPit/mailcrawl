"""Bench compares embedders without importing a model."""

import base64
import json
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

from mailcrawl.bench import run_bench
from mailcrawl.cli import main
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.inference import invoke
from mailcrawl.paths import UsageError
from mailcrawl.store import connect
from mailcrawl.vectors import MLX_LOCAL_VERIFY, MLX_PROCESSOR_ERROR, MlxEmbedder, open_embedder

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4z8AAAAMBAQAY3Y20AAAAAElFTkSuQmCC"
)


def _mbox() -> str:
    encoded = base64.b64encode(_PNG).decode("ascii")
    return f"""From 1768000000000000601@example.com Thu Jan 15 12:00:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Alpha note
Date: Thu, 15 Jan 2026 12:00:00 +0000
Message-ID: <bench-alpha@example.com>
X-GM-THRID: 1768000000000000601
X-GM-MSGID: 1768000000000000601
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

alpha bench

From 1768000000000000602@example.com Thu Jan 15 12:05:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Beta note
Date: Thu, 15 Jan 2026 12:05:00 +0000
Message-ID: <bench-beta@example.com>
X-GM-THRID: 1768000000000000602
X-GM-MSGID: 1768000000000000602
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

beta bench

From 1768000000000000603@example.com Thu Jan 15 12:10:00 +0000 2026
From: Bao Fixture <bao@example.com>
To: Ada Fixture <ada@example.com>
Subject: Scan note
Date: Thu, 15 Jan 2026 12:10:00 +0000
Message-ID: <bench-scan@example.com>
X-GM-THRID: 1768000000000000603
X-GM-MSGID: 1768000000000000603
X-Gmail-Labels: Inbox
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="benchbound"

--benchbound
Content-Type: text/plain; charset="utf-8"

scan bench

--benchbound
Content-Type: image/png; name="scan.png"
Content-Disposition: attachment; filename="scan.png"
Content-Transfer-Encoding: base64

{encoded}
--benchbound--

"""


class BenchTest(unittest.TestCase):
    def test_fake_pair_reports_agreement_and_leaves_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _archive(Path(tmp))
            before = db.read_bytes()
            report = run_bench(db, ["fake", "fake"], 4)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(report["sampled_texts"], 3)
            self.assertEqual(report["sampled_images"], 1)
            pair = report["pairs"][0]
            self.assertEqual(pair["left"], "fake")
            self.assertEqual(pair["right"], "fake")
            self.assertAlmostEqual(pair["cosine_min"], 1.0)
            self.assertAlmostEqual(pair["cosine_mean"], 1.0)
            self.assertAlmostEqual(pair["knn_top10_overlap"], 1.0)
            self.assertEqual(
                report["decisions"],
                [
                    {"accuracy": 1.0, "compared": 2, "embedder": "fake", "matched": 2},
                    {"accuracy": 1.0, "compared": 2, "embedder": "fake", "matched": 2},
                ],
            )
            self.assertEqual(report["embedders"][0]["load_s"], 0.0)
            self.assertGreater(report["embedders"][0]["texts_per_min"], 0)
            self.assertGreater(report["embedders"][0]["images_per_min"], 0)
            self.assertGreater(report["embedders"][0]["peak_rss_bytes"], 0)
            self.assertIn("| embedder |", report["markdown"])
            self.assertIn("| pair |", report["markdown"])
            vector_db = Path(str(report["vector_db"]))
            conn = sqlite3.connect(vector_db)
            try:
                stored = conn.execute("SELECT count(*) FROM bench_vectors").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(stored, 8)
            shutil.rmtree(vector_db.parent)
            self.assertNotIn("mlx", sys.modules)
            self.assertNotIn("mlx_vlm", sys.modules)
            self.assertNotIn("sentence_transformers", sys.modules)
            self.assertNotIn("torch", sys.modules)

    def test_cli_bench_is_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _archive(home)
            code, doc = _cli(
                [
                    "--json",
                    "--home",
                    str(home),
                    "bench",
                    "--account",
                    "personal",
                    "--embedders",
                    "fake,fake",
                    "--sample",
                    "4",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(doc["account"], "personal")
            self.assertTrue(doc["source_read_only"])
            self.assertEqual(doc["decisions"][0]["accuracy"], 1.0)
            self.assertIn("texts/min", doc["markdown"])
            shutil.rmtree(Path(doc["vector_db"]).parent)

    def test_mlx_image_path_stops_without_the_processor(self) -> None:
        embedder = open_embedder("mlx")
        self.assertIsInstance(embedder, MlxEmbedder)
        self.assertEqual(embedder.model_id, "mlx-community/embeddinggemma-2-8bit")
        self.assertEqual(embedder.dimension, 768)
        saved = sys.modules.get("transformers")
        sys.modules["transformers"] = types.ModuleType("transformers")
        try:
            with self.assertRaises(UsageError) as raised:
                invoke(
                    "embeddings",
                    lambda: embedder.embed_images([_PNG]),
                    {"embeddings": True},
                )
        finally:
            if saved is None:
                sys.modules.pop("transformers", None)
            else:
                sys.modules["transformers"] = saved
        self.assertIn("EmbeddingGemma2Processor", str(raised.exception))
        self.assertIn("text-only", str(raised.exception))
        self.assertEqual(str(raised.exception), MLX_PROCESSOR_ERROR)
        self.assertNotIn("mlx_vlm", sys.modules)
        spec = Path("docs/SPEC.md").read_text(encoding="utf-8")
        self.assertIn(MLX_LOCAL_VERIFY, spec)
        self.assertNotIn("db-v2", MLX_LOCAL_VERIFY)

    def test_db_v2_and_a_short_sample_are_refused(self) -> None:
        with self.assertRaises(UsageError) as raised:
            run_bench(Path("/tmp/db-v2/personal.db"), ["fake"], 1)
        self.assertIn("db-v2", str(raised.exception))
        with tempfile.TemporaryDirectory() as tmp:
            db = _archive(Path(tmp))
            with self.assertRaises(UsageError):
                run_bench(db, ["fake"], 0)
            with self.assertRaises(UsageError):
                run_bench(db, [], 1)


def _archive(home: Path) -> Path:
    config = home / "config.toml"
    mbox = home / "bench.mbox"
    mbox.write_text(_mbox(), encoding="utf-8")
    code, _doc = _cli(
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
    if code != 0:
        raise AssertionError(code)
    code, _doc = _cli(
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
    )
    if code != 0:
        raise AssertionError(code)
    db = home / "personal.db"
    conn = connect(db)
    try:
        rows = conn.execute("SELECT thread_pk FROM threads ORDER BY thread_pk").fetchall()
        for thread_pk in (int(rows[0][0]), int(rows[1][0])):
            decide_once(
                conn,
                thread_pk,
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
    finally:
        conn.close()
    return db


def _cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    from contextlib import redirect_stdout
    from io import StringIO

    buf = StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    text = buf.getvalue()
    if not text:
        return code, {}
    return code, json.loads(text)
