# SPDX-License-Identifier: MIT
"""Thread flags derived from stored messages and attachments."""

from __future__ import annotations

import io
import sqlite3
import warnings
from contextlib import contextmanager

from mailcrawl.runtime_limits import MAX_IMAGE_BYTES

TEXT_LIMIT = 200
INLINE_IMAGE_FLOOR = 20 * 1024
MIN_IMAGE_SIDE = 32
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 25_000_000
MAX_FRAMES = 64
MAX_FRAME_WORK = MAX_IMAGE_PIXELS
TRACKING_PIXELS = frozenset({(1, 1)})
RASTER_IMAGES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/tiff",
    }
)

_MARK_SQL = """
UPDATE threads SET image_only = CASE WHEN (
  COALESCE((
    SELECT SUM(LENGTH(COALESCE(body_text, '')))
    FROM messages
    WHERE messages.thread_pk = threads.thread_pk
  ), 0) < ?
  AND EXISTS (
    SELECT 1
    FROM attachments
    JOIN messages ON messages.msg_pk = attachments.msg_pk
    WHERE messages.thread_pk = threads.thread_pk
      AND (
        lower(COALESCE(attachments.mime_type, '')) LIKE 'application/pdf%'
        OR (
          lower(COALESCE(attachments.mime_type, '')) LIKE 'image/%'
          AND NOT (
            attachments.is_inline = 1
            AND COALESCE(attachments.size_bytes, 0) < ?
          )
        )
      )
  )
) THEN 1 ELSE 0 END
"""


def embeddable_image(
    mime_type: str | None,
    is_inline: int,
    size_bytes: int | None,
    width: int | None = None,
    height: int | None = None,
) -> bool:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime not in RASTER_IMAGES:
        return False
    if int(is_inline) == 1 and int(size_bytes or 0) < INLINE_IMAGE_FLOOR:
        return False
    if dimension_skip_reason(width, height):
        return False
    return True


def dimension_skip_reason(width: int | None, height: int | None) -> str | None:
    if width is None or height is None:
        return None
    if (width, height) in TRACKING_PIXELS:
        return f"tracking pixel {width}x{height}"
    if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
        return f"side {width}x{height} below {MIN_IMAGE_SIDE}"
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        return f"side {width}x{height} above {MAX_IMAGE_SIDE}"
    if width * height > MAX_IMAGE_PIXELS:
        return f"pixels {width}x{height} above {MAX_IMAGE_PIXELS}"
    return None


def image_gate(
    mime_type: str | None,
    is_inline: int,
    size_bytes: int | None,
    blob: bytes,
) -> tuple[bool, str | None]:
    if not embeddable_image(mime_type, is_inline, size_bytes, None, None):
        return False, None
    if len(blob) > MAX_IMAGE_BYTES:
        return False, "encoded image exceeds byte budget"
    measured = _measure(blob)
    if isinstance(measured, str):
        return False, measured
    reason = dimension_skip_reason(measured[0], measured[1])
    if reason:
        return False, reason
    return True, None


def opened_limit_reason(picture) -> str | None:
    frames = int(getattr(picture, "n_frames", 1) or 1)
    canvas = (int(picture.size[0]), int(picture.size[1]))
    if frames > MAX_FRAMES:
        return f"frames {frames} above {MAX_FRAMES}"
    work = frames * canvas[0] * canvas[1]
    if work > MAX_FRAME_WORK:
        return f"frame work {work} above {MAX_FRAME_WORK}"
    widest, tallest = canvas
    passed = False
    try:
        for index in range(frames):
            if index:
                picture.seek(index)
            current = (int(picture.size[0]), int(picture.size[1]))
            boxes = [current]
            for tile in picture.tile or ():
                box = tile[1]
                if len(box) != 4:
                    continue
                boxes.append((int(box[2] - box[0]), int(box[3] - box[1])))
            for width, height in boxes:
                if width > canvas[0] or height > canvas[1]:
                    return (
                        f"frame {width}x{height} above screen {canvas[0]}x{canvas[1]}"
                    )
                widest = max(widest, width)
                tallest = max(tallest, height)
                reason = dimension_skip_reason(widest, tallest)
                if reason:
                    return reason
        passed = True
        return dimension_skip_reason(widest, tallest)
    finally:
        if passed and frames > 1:
            picture.seek(0)


def _measure(blob: bytes) -> tuple[int, int] | str:
    with pillow_limits() as image_mod:
        if image_mod is None:
            return "image size is unknown"
        try:
            with image_mod.open(io.BytesIO(blob)) as picture:
                reason = opened_limit_reason(picture)
                if reason:
                    return reason
                return (int(picture.size[0]), int(picture.size[1]))
        except Exception as exc:
            return _open_failure(exc)


def _open_failure(exc: BaseException) -> str:
    text = str(exc).splitlines()[0][:200] if str(exc) else ""
    if type(exc).__name__ in ("DecompressionBombError", "DecompressionBombWarning"):
        return text or "decompression bomb"
    if "decompression bomb" in text.lower():
        return text
    return "image size is unknown"


@contextmanager
def pillow_limits():
    try:
        from PIL import Image
    except ImportError:
        yield None
        return
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        yield Image


def mark_image_only(conn: sqlite3.Connection, account_id: int | None = None) -> None:
    sql = _MARK_SQL
    params: list[int] = [TEXT_LIMIT, INLINE_IMAGE_FLOOR]
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params.append(account_id)
    conn.execute(sql, params)
