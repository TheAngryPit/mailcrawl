# SPDX-License-Identifier: MIT
"""Accepted-case error on a synthetic audited set. Grey rows are not accepted."""

from __future__ import annotations

import json
import sys
from pathlib import Path

LIMIT = 0.02


class AuditGateError(Exception):
    def __init__(self, rate: float | None) -> None:
        self.rate = rate
        if rate is None:
            super().__init__("accepted set is empty")
            return
        super().__init__(f"accepted-case error {rate:.4f} is not under {LIMIT:.2f}")


def accepted_case_error(cases: list[dict[str, object]]) -> float:
    accepted = [case for case in cases if case.get("accepted") is True]
    if not accepted:
        raise AuditGateError(None)
    wrong = 0
    for case in accepted:
        if case.get("rule") != case.get("correct_rule"):
            wrong += 1
    return wrong / len(accepted)


def load_cases(path: Path) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("synthetic") is not True:
        raise AuditGateError(1.0)
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise AuditGateError(1.0)
    return cases


def check(path: Path) -> float:
    rate = accepted_case_error(load_cases(path))
    if rate >= LIMIT:
        raise AuditGateError(rate)
    return rate


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 -m mailcrawl.audit <fixture.json>", file=sys.stderr)
        return 2
    try:
        rate = check(Path(argv[1]))
    except AuditGateError as exc:
        print(str(exc))
        return 1
    print(f"accepted-case error {rate:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
