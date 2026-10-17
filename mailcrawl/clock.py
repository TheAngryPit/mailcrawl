"""The one clock decision code is allowed to read."""

from __future__ import annotations

import time


def as_of(explicit: int | None, configured: int | None = None) -> int:
    if explicit is not None:
        return int(explicit)
    if configured is not None:
        return int(configured)
    return int(time.time())
