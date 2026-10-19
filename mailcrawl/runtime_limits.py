"""Byte budgets for optional inference and stored archive consumers."""

from __future__ import annotations

import time
import socket

from mailcrawl.paths import UsageError

MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_REASON_CHARS = 8192
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BATCH_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BATCH_COUNT = 128
MAX_BENCH_THREADS = 100_000


def response_bytes(stream, limit: int = MAX_HTTP_BYTES, *, timeout: float = 30) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    reader = getattr(stream, "read1", stream.read)
    connection = stream
    sock = None
    for _ in range(4):
        raw = getattr(connection, "raw", None)
        candidate = getattr(raw, "_sock", None)
        if isinstance(candidate, socket.socket):
            sock = candidate
            break
        connection = getattr(connection, "fp", None)
        if connection is None:
            break
    while size <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UsageError("inference response exceeds time budget")
        if sock is not None and sock.fileno() >= 0:
            sock.settimeout(remaining)
        chunk = reader(min(65536, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise UsageError("inference response exceeds byte budget")
        chunks.append(chunk)
    raise UsageError("inference response exceeds byte budget")


def request_bytes(payload: bytes) -> bytes:
    if len(payload) > MAX_HTTP_BYTES:
        raise UsageError("inference request exceeds byte budget")
    return payload


def context_parts(parts, limit: int = MAX_CONTEXT_BYTES, *, transform=None) -> str:
    kept: list[str] = []
    size = 0
    for part in parts:
        text = str(part or "")
        size += len(text.encode("utf-8")) + bool(kept)
        if size > limit:
            raise UsageError("thread text exceeds byte budget")
        kept.append(text if transform is None else transform(text))
    return "\n".join(kept)
