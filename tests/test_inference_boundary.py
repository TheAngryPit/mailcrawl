"""Model calls stay inside declared points, and those points default to off."""

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
        namespace: dict[str, object] = {}
        exec(path.read_text(encoding="utf-8"), namespace)  # noqa: S102
        classify = namespace["classify"]
        assert callable(classify)
        with self.assertRaises(BoundaryError):
            classify(probe)
        self.assertEqual(probe.calls, 0)

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

    def test_unknown_point_fails(self) -> None:
        probe = Probe()
        with self.assertRaises(BoundaryError):
            invoke("not-a-point", lambda: probe.predict("synthetic"), {})
        self.assertEqual(probe.calls, 0)
