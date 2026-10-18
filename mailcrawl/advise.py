# SPDX-License-Identifier: MIT
"""Advisory suggestions for else threads. Callers pass an advisor. The default is none."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

MODEL_NAME = "por definir"


@dataclass(frozen=True)
class AdviceCase:
    thread_pk: int
    rule_id: str
    lane: str


@dataclass(frozen=True)
class Advice:
    rule_id: str
    lane: str
    confidence: str


class Advisor(Protocol):
    def advise(self, case: AdviceCase) -> Advice: ...


class StubAdvisor:
    """Deterministic stand-in. It does not load a model and it does not use the network."""

    def advise(self, case: AdviceCase) -> Advice:
        if case.rule_id != "else":
            raise ValueError("advisor sees else threads only")
        return Advice("5", "imbox", "0.00")
