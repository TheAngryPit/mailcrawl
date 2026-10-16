"""The synthetic audited set fails CI when accepted-case error exceeds 2%."""

import unittest
from pathlib import Path

from mailcrawl.audit import AuditGateError, check

ROOT = Path(__file__).resolve().parents[1]


class AuditGateTest(unittest.TestCase):
    def test_under_two_percent_passes(self) -> None:
        rate = check(ROOT / "testdata/audit/accepted-under-2.json")
        self.assertEqual(rate, 0.02)

    def test_over_two_percent_fails(self) -> None:
        with self.assertRaises(AuditGateError) as caught:
            check(ROOT / "testdata/audit/accepted-over-2.json")
        self.assertEqual(caught.exception.rate, 0.04)
