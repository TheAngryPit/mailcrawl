# SPDX-License-Identifier: MIT
"""The one clock decision code is allowed to read."""

from __future__ import annotations

import calendar
import time

from mailcrawl.paths import UsageError

AS_OF_MIN = calendar.timegm((2000, 1, 1, 0, 0, 0))
AS_OF_MAX = calendar.timegm((2100, 1, 1, 0, 0, 0))


def as_of(explicit: int | None, configured: int | None = None) -> int:
    if explicit is not None:
        return require_as_of(explicit)
    if configured is not None:
        return require_as_of(configured)
    return require_as_of(time.time())


def require_as_of(value: int) -> int:
    stamp = int(value)
    if stamp < AS_OF_MIN or stamp > AS_OF_MAX:
        raise UsageError("as_of is out of range")
    return stamp


def utc_parts(stamp: int) -> tuple[int, int, int]:
    year, month, day = time.gmtime(int(stamp))[:3]
    return int(year), int(month), int(day)
