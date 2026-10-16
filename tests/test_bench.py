"""Bench compares embedders without importing a model."""

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import mailcrawl.bench as bench_module
from mailcrawl.bench import (
    LABEL_TRUTH_VERIFY,
    ThreadRow,
    label_scores,
    ru_maxrss_mb,
    run_bench,
    select_sample,
)
from mailcrawl.cli import main
from mailcrawl.decisions import DecisionDraft, decide_once
from mailcrawl.inference import invoke
from mailcrawl.paths import UsageError
from mailcrawl.store import connect
from mailcrawl.taxonomy import load_taxonomy
from mailcrawl.vectors import (
    MLX_LOCAL_VERIFY,
    MLX_PROCESSOR_ERROR,
    FakeEmbedder,
    MlxEmbedder,
    open_embedder,
)

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
            self.assertEqual(report["n_human"], 2)
            self.assertEqual(report["truth"], "decisions")
            self.assertNotIn("n_labelled", report)
            self.assertIn("human_accuracy", report["markdown"])
            self.assertNotIn("label_overlap@1", report["markdown"])
            self.assertEqual(report["n_with_images"], 1)
            self.assertEqual(report["n_image_only"], 1)
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
            self.assertNotIn("peak_rss_bytes", report["embedders"][0])
            self.assertGreater(report["embedders"][0]["peak_rss_mb"], 1)
            self.assertLess(report["embedders"][0]["peak_rss_mb"], 4096)
            self.assertGreater(report["embedders"][1]["peak_rss_mb"], 1)
            self.assertIn("1.000 (n=2)", report["markdown"])
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
            self.assertIn("peak_rss_mb", doc["markdown"])
            self.assertIn("(n=2)", doc["markdown"])
            self.assertEqual(doc["n_human"], 2)
            self.assertEqual(doc["n_with_images"], 1)
            shutil.rmtree(Path(doc["vector_db"]).parent)

    def test_sample_spreads_human_labels_and_keeps_an_image(self) -> None:
        rows = [
            ThreadRow(1, "a", "a", False, False, "reference"),
            ThreadRow(2, "b", "b", False, False, "reference"),
            ThreadRow(3, "c", "c", False, False, "reference"),
            ThreadRow(4, "d", "d", False, False, "noise"),
            ThreadRow(5, "e", "e", False, False, "reply"),
            ThreadRow(9, "scan", "scan", True, True, None),
        ]
        chosen = [row.thread_pk for row in select_sample(rows, 4)]
        self.assertEqual(chosen, [9, 4, 1, 5])

    def test_ru_maxrss_is_megabytes(self) -> None:
        self.assertEqual(ru_maxrss_mb(1024, "linux"), 1.0)
        self.assertEqual(ru_maxrss_mb(1024 * 1024, "darwin"), 1.0)

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

    def test_embedders_exit_before_the_next_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _archive(Path(tmp))
            seen: list[subprocess.Popen[str]] = []
            real = subprocess.Popen

            def popen(argv, **kwargs):
                if seen:
                    previous = seen[-1]
                    self.assertIsNotNone(previous.returncode)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(previous.pid, 0)
                proc = real(argv, **kwargs)
                seen.append(proc)
                return proc

            bench_module.subprocess.Popen = popen
            try:
                report = run_bench(db, ["fake", "fake"], 1)
            finally:
                bench_module.subprocess.Popen = real
            self.assertEqual([proc.returncode for proc in seen], [0, 0])
            self.assertNotEqual(seen[0].pid, seen[1].pid)
            self.assertEqual(
                [row["name"] for row in report["embedders"]],
                ["fake", "fake"],
            )


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


class BenchChildTest(unittest.TestCase):
    def test_child_json_file_uses_the_batch_bound(self) -> None:
        text = " ".join(f"w{i:02d}" for i in range(40))
        embedder = FakeEmbedder()
        embedder.batch_size = 2
        embedder.max_tokens = 8
        expected = invoke(
            "embeddings",
            lambda: embedder.embed([text] * 5),
            {"embeddings": True},
        )
        self.assertEqual([len(chunk) for chunk in embedder.batches], [2, 2, 1])
        self.assertLessEqual(len(embedder.batches[0][0]), 32)
        descriptor, out_path = tempfile.mkstemp(suffix=".json")
        os.close(descriptor)
        payload = {
            "batch_size": 2,
            "image_only": [],
            "images": [],
            "max_tokens": 8,
            "name": "fake",
            "texts": [[1, text]] * 5,
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "mailcrawl.bench", out_path],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "")
            raw = json.loads(Path(out_path).read_text(encoding="utf-8"))
        finally:
            Path(out_path).unlink(missing_ok=True)
        self.assertEqual(raw["text_vectors"], expected)


def _category_row(thread_pk: int, categories: set[str], *, image_only: bool = False) -> ThreadRow:
    return ThreadRow(thread_pk, "s", "b", image_only, image_only, None, frozenset(categories))


class LabelTruthTest(unittest.TestCase):
    def test_sample_takes_the_rarest_category_and_skips_unmapped(self) -> None:
        rows = [
            _category_row(1, {"common"}),
            _category_row(2, {"common"}),
            _category_row(3, {"common"}),
            _category_row(4, {"rare"}),
            _category_row(5, {"rare"}),
            _category_row(8, set()),
            _category_row(9, {"common"}, image_only=True),
        ]
        chosen = [row.thread_pk for row in select_sample(rows, 4, truth="labels")]
        self.assertEqual(chosen, [9, 4, 5, 1])
        self.assertNotIn(8, chosen)

    def test_scores_cover_exact_partial_and_no_overlap(self) -> None:
        same = label_scores(
            [1, 2, 3, 4],
            [(1.0, 0.0), (1.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
            {
                1: frozenset({"alpha", "beta"}),
                2: frozenset({"alpha", "beta"}),
                3: frozenset({"alpha", "beta"}),
                4: frozenset({"gamma"}),
            },
        )
        self.assertEqual(same["compared"], 4)
        self.assertEqual(same["overlap_at_1"], 0.75)
        self.assertEqual(same["jaccard_at_1"], 0.75)
        self.assertEqual(same["vote_f1_at_10"], 0.8)
        self.assertEqual(same["recall"], {"alpha": 1.0, "beta": 1.0, "gamma": 0.0})
        partial = label_scores(
            [1, 2],
            [(1.0, 0.0), (1.0, 0.0)],
            {1: frozenset({"alpha", "beta"}), 2: frozenset({"alpha"})},
        )
        self.assertEqual(partial["overlap_at_1"], 1.0)
        self.assertEqual(partial["jaccard_at_1"], 0.5)
        self.assertEqual(partial["vote_f1_at_10"], 4 / 6)
        none = label_scores(
            [1, 2],
            [(1.0, 0.0), (1.0, 0.0)],
            {1: frozenset({"alpha"}), 2: frozenset({"gamma"})},
        )
        self.assertEqual(none["overlap_at_1"], 0.0)
        self.assertEqual(none["jaccard_at_1"], 0.0)
        self.assertEqual(none["vote_f1_at_10"], 0.0)
        self.assertEqual(none["recall"], {"alpha": 0.0, "gamma": 0.0})

    def test_label_bench_keeps_every_category_and_skips_unmapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            taxonomy = home / "taxonomy.json"
            taxonomy.write_text(
                json.dumps(
                    {
                        "default": {
                            "alpha": ["Bill"],
                            "beta": ["Bill", "Receipt"],
                            "gamma": ["News"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            mapping = {
                name: frozenset(categories)
                for name, categories in load_taxonomy(taxonomy).names_for("personal").items()
            }
            self.assertEqual(mapping["Bill"], frozenset({"alpha", "beta"}))
            db = home / "personal.db"
            _seed_labels(db)
            before = db.read_bytes()
            report = run_bench(db, ["fake"], 10, truth="labels", label_categories=mapping)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(report["truth"], "labels")
            self.assertEqual(report["n_labelled"], 4)
            self.assertEqual(report["sampled_texts"], 4)
            self.assertEqual(report["per_category"], {"alpha": 2, "beta": 3, "gamma": 1})
            self.assertNotIn("n_human", report)
            self.assertIn("label_overlap@1", report["markdown"])
            self.assertIn("jaccard@1", report["markdown"])
            self.assertIn("vote_f1@10", report["markdown"])
            self.assertEqual(report["labels"][0]["embedder"], "fake")
            spec = Path("docs/SPEC.md").read_text(encoding="utf-8")
            self.assertIn(LABEL_TRUTH_VERIFY, spec)
            shutil.rmtree(Path(str(report["vector_db"])).parent)

    def test_cli_label_truth_and_a_missing_taxonomy(self) -> None:
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
                    "fake",
                    "--sample",
                    "2",
                    "--truth",
                    "labels",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(doc["error"], "usage")
            self.assertEqual(doc["detail"], "labels truth needs a taxonomy")


def _seed_labels(db: Path) -> None:
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
        )
        account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        labels = (
            ("Label_bill", "Bill"),
            ("Label_receipt", "Receipt"),
            ("Label_news", "News"),
            ("Label_other", "Other"),
        )
        for label_id, name in labels:
            conn.execute(
                """
                INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
                VALUES (?, ?, ?, 'user', 1)
                """,
                (account_id, label_id, name),
            )
        threads = (
            (1, "Bill"),
            (2, "Bill"),
            (3, "News"),
            (4, "Receipt"),
            (5, "Other"),
        )
        for offset, (thread_pk, name) in enumerate(threads, start=1):
            conn.execute(
                """
                INSERT INTO threads(thread_pk, account_id, gmail_thread_id, msg_count)
                VALUES (?, ?, ?, 1)
                """,
                (thread_pk, account_id, f"t-{thread_pk}"),
            )
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, subject,
                  body_text, raw_sha256, provenance_hash, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    f"m-{thread_pk}",
                    thread_pk,
                    offset,
                    name,
                    f"body {name} {thread_pk}",
                    f"sha-{thread_pk}",
                    f"prov-{thread_pk}",
                    1,
                ),
            )
            msg_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            label_id = {
                "Bill": "Label_bill",
                "News": "Label_news",
                "Receipt": "Label_receipt",
                "Other": "Label_other",
            }[name]
            conn.execute(
                """
                INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                VALUES (?, ?, ?, 1)
                """,
                (msg_pk, account_id, label_id),
            )
        conn.commit()
    finally:
        conn.close()

