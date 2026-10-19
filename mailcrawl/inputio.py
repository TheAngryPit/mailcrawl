# SPDX-License-Identifier: MIT
"""Bounded reads of regular files beneath an operator-selected directory."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import BinaryIO

MAX_MANIFEST_BYTES = 16 << 20
MAX_INDEX_BYTES = 32 << 20
MAX_LABEL_BYTES = 2 << 20
MAX_RECORD_BYTES = 1 << 20
MAX_MESSAGE_BYTES = 64 << 20
MAX_HEADER_BYTES = 64 << 10
MAX_LINE_BYTES = 1 << 20
MAX_MIME_PARTS = 256
MAX_MIME_DEPTH = 32
MAX_DECODED_BYTES = 32 << 20
MAX_TEXT_CHARS = 2 << 20
MAX_IMPORT_BYTES = 512 << 20
MAX_IMPORT_RECORDS = 100_000
MAX_IMAGE_BYTES = 8 << 20
MAX_IMPORT_IMAGE_BYTES = 256 << 20
MAX_EXTRACT_BYTES = 4 << 20
MAX_EXTRACT_STDERR_BYTES = 64 << 10
MAX_IMPORT_EXTRACT_BYTES = 64 << 20


class InputLimit(ValueError):
    """An input exceeds the bounded import policy."""


class ImportExhausted(InputLimit):
    """The aggregate import budget is exhausted; the transaction must abort."""


class UnsafeInput(ValueError):
    """An input path is not a regular file beneath its pinned root."""


def bounded_read(handle: BinaryIO, max_bytes: int) -> bytes:
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeInput("input is not a regular file")
    if info.st_size > max_bytes:
        raise InputLimit("input byte limit exceeded")
    data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise InputLimit("input byte limit exceeded")
    return data


class SafeRoot:
    def __init__(self, root: Path) -> None:
        self.path = root.absolute()
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def __enter__(self) -> SafeRoot:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __del__(self) -> None:
        if hasattr(self, "fd"):
            self.close()

    def open(self, relative: str) -> BinaryIO:
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or any(p in ("", ".", "..") for p in relative.split("/")) or "\\" in relative or "\x00" in relative or ":" in relative:
            raise UnsafeInput("unsafe input path")
        current = os.dup(self.fd)
        try:
            for part in pure.parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
                os.close(current)
                current = following
            fd = os.open(pure.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise UnsafeInput("input is not a regular file")
                return os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise UnsafeInput("input path is unsafe or unavailable") from exc
        finally:
            os.close(current)

    def directories(self, relative: str) -> list[str]:
        if not relative or relative in (".", "..") or any(char in relative for char in ("/", "\\", "\x00", ":")):
            raise UnsafeInput("unsafe directory path")
        try:
            fd = os.open(relative, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise UnsafeInput("unsafe directory path") from exc
        try:
            names = []
            with os.scandir(fd) as entries:
                for count, entry in enumerate(entries, start=1):
                    if count > MAX_IMPORT_RECORDS:
                        raise InputLimit("directory entry limit exceeded")
                    if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                        names.append(entry.name)
            return sorted(names)
        finally:
            os.close(fd)

    def read(self, relative: str, max_bytes: int) -> bytes:
        with self.open(relative) as handle:
            return bounded_read(handle, max_bytes)


def read_file(path: Path, max_bytes: int) -> bytes:
    with SafeRoot(path.parent) as root:
        return root.read(path.name, max_bytes)


@dataclass
class ImportBudget:
    image_bytes: int = 0
    extract_bytes: int = 0
    input_bytes: int = 0
    created_images: list[Path] = field(default_factory=list)


import_budget: ContextVar[ImportBudget | None] = ContextVar("mailcrawl_import_budget", default=None)


def read_import(root: SafeRoot, relative: str, max_bytes: int, budget: ImportBudget | None = None) -> bytes:
    account = budget if budget is not None else import_budget.get()
    with root.open(relative) as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > max_bytes:
            raise InputLimit("message byte limit exceeded")
        if account is None:
            return bounded_read(handle, max_bytes)
        remaining = MAX_IMPORT_BYTES - account.input_bytes
        if size > remaining:
            raise ImportExhausted("import byte limit exceeded")
        account.input_bytes += size
        try:
            raw = bounded_read(handle, min(max_bytes, remaining))
        except InputLimit as exc:
            if remaining <= max_bytes:
                raise ImportExhausted("import byte limit exceeded") from exc
            raise
        account.input_bytes += max(0, len(raw) - size)
        return raw


def charge_extraction(size: int) -> None:
    budget = import_budget.get()
    if budget is not None:
        budget.extract_bytes += size
        if budget.extract_bytes > MAX_IMPORT_EXTRACT_BYTES:
            raise ImportExhausted("import extraction byte limit exceeded")


@dataclass(frozen=True)
class Refused:
    offset: int
    reason: str
