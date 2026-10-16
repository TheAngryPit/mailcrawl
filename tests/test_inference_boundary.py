import importlib.util
import sys
import unittest
from pathlib import Path

from mailcrawl.inference import (
    DECLARED_POINTS,
    BoundaryError,
    enabled_points,
    enter_model,
    invoke,
    scan_package,
    scan_source,
)
from mailcrawl.vectors import LOCAL_VERIFY, FakeEmbedder

ROOT = Path(__file__).resolve().parents[1]


class Probe:
    def __init__(self) -> None:
        self.calls = 0

    def predict(self, text: str) -> str:
        enter_model()
        self.calls += 1
        return text


class InferenceBoundaryTest(unittest.TestCase):
    def test_package_has_no_model_call_outside_a_point(self) -> None:
        self.assertEqual(scan_package(ROOT / "mailcrawl"), [])

    def test_planted_violation_is_caught(self) -> None:
        path = ROOT / "testdata/inference/planted_violation.py"
        problems = scan_source(path.read_text(encoding="utf-8"), str(path))
        self.assertEqual(len(problems), 1)
        probe = Probe()
        spec = importlib.util.spec_from_file_location("planted_violation", path)
        if spec is None or spec.loader is None:
            self.fail("planted module did not load")
        module = importlib.util.module_from_spec(spec)
        write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = write_bytecode
        with self.assertRaises(BoundaryError):
            module.classify(probe)
        self.assertEqual(probe.calls, 0)

    def test_model_client_import_is_caught(self) -> None:
        problems = scan_source("import openai\n", "planted_import.py")
        self.assertEqual(problems, ["planted_import.py:1: model client import openai"])
        hidden = scan_source(
            "def load():\n    from sentence_transformers import SentenceTransformer\n",
            "planted_lazy.py",
        )
        self.assertEqual(
            hidden,
            ["planted_lazy.py:2: model client import sentence_transformers"],
        )

    def test_declared_points_are_off_by_default(self) -> None:
        self.assertEqual(enabled_points(None), frozenset())
        probe = Probe()
        result = invoke("embeddings", lambda: probe.predict("synthetic"))
        self.assertIsNone(result)
        self.assertEqual(probe.calls, 0)
        for point in DECLARED_POINTS:
            self.assertIsNone(invoke(point, lambda: probe.predict("synthetic")))
        self.assertEqual(probe.calls, 0)

    def test_enabled_declared_point_runs_once(self) -> None:
        probe = Probe()
        result = invoke(
            "embeddings",
            lambda: probe.predict("synthetic"),
            {"embeddings": True},
        )
        self.assertEqual(result, "synthetic")
        self.assertEqual(probe.calls, 1)

    def test_vectors_module_stays_inside_the_fence(self) -> None:
        path = ROOT / "mailcrawl" / "vectors.py"
        self.assertEqual(scan_source(path.read_text(encoding="utf-8"), str(path)), [])
        embedder = FakeEmbedder()
        with self.assertRaises(BoundaryError):
            embedder.embed(["synthetic"])
        stored = invoke(
            "embeddings",
            lambda: embedder.embed(["synthetic"]),
            {"embeddings": True},
        )
        again = invoke(
            "embeddings",
            lambda: embedder.embed(["synthetic"]),
            {"embeddings": True},
        )
        other = invoke(
            "embeddings",
            lambda: embedder.embed(["other synthetic"]),
            {"embeddings": True},
        )
        self.assertEqual(stored, again)
        self.assertNotEqual(stored, other)
        self.assertEqual(len(stored[0]), 768)
        self.assertNotIn("db-v2", LOCAL_VERIFY)

    def test_unknown_point_fails(self) -> None:
        probe = Probe()
        with self.assertRaises(BoundaryError):
            invoke("not-a-point", lambda: probe.predict("synthetic"), {})
        self.assertEqual(probe.calls, 0)
