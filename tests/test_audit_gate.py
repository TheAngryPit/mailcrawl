import unittest
from pathlib import Path

from mailcrawl.audit import AuditGateError, check

ROOT = Path(__file__).resolve().parents[1]


class AuditGateTest(unittest.TestCase):
    def test_under_two_percent_passes(self) -> None:
        rate = check(ROOT / "testdata/audit/accepted-under-2.json")
        self.assertEqual(rate, 1 / 60)
        self.assertLess(rate, 0.02)

    def test_exact_two_percent_fails(self) -> None:
        with self.assertRaises(AuditGateError) as caught:
            check(ROOT / "testdata/audit/accepted-exact-2.json")
        self.assertEqual(caught.exception.rate, 0.02)

    def test_over_two_percent_fails(self) -> None:
        with self.assertRaises(AuditGateError) as caught:
            check(ROOT / "testdata/audit/accepted-over-2.json")
        self.assertEqual(caught.exception.rate, 0.04)

    def test_empty_accepted_set_fails_closed(self) -> None:
        with self.assertRaises(AuditGateError) as caught:
            check(ROOT / "testdata/audit/accepted-empty.json")
        self.assertIsNone(caught.exception.rate)
