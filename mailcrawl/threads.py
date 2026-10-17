"""Thread flags derived from stored messages and attachments."""

from __future__ import annotations

import io
import sqlite3
import struct
import warnings
from contextlib import contextmanager

TEXT_LIMIT = 200
INLINE_IMAGE_FLOOR = 20 * 1024
MIN_IMAGE_SIDE = 32
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 25_000_000
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
    width, height, probe = _image_dimensions(blob)
    if not embeddable_image(mime_type, is_inline, size_bytes, width, height):
        return False, dimension_skip_reason(width, height)
    if width is None or height is None:
        return False, probe or "image size is unknown"
    return True, None


def _image_dimensions(blob: bytes) -> tuple[int | None, int | None, str | None]:
    parsed = image_size(blob)
    if parsed is not None:
        return parsed[0], parsed[1], None
    size, reason = _pil_header_size(blob)
    if size is None:
        return None, None, reason or "image size is unknown"
    return size[0], size[1], None


def _pil_header_size(blob: bytes) -> tuple[tuple[int, int] | None, str | None]:
    with pillow_limits() as image_mod:
        if image_mod is None:
            return None, "image size is unknown"
        try:
            with image_mod.open(io.BytesIO(blob)) as picture:
                return (int(picture.size[0]), int(picture.size[1])), None
        except (image_mod.DecompressionBombError, image_mod.DecompressionBombWarning) as exc:
            text = str(exc).splitlines()[0][:200] if str(exc) else "decompression bomb"
            return None, text or "decompression bomb"
        except Exception:
            return None, "image size is unknown"


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


def image_size(blob: bytes) -> tuple[int, int] | None:
    if len(blob) >= 10 and blob[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack_from("<HH", blob, 6)
    if len(blob) >= 24 and blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack_from(">II", blob, 16)
    if blob.startswith(b"\xff\xd8"):
        return _jpeg_size(blob)
    return None


def _jpeg_size(blob: bytes) -> tuple[int, int] | None:
    index = 2
    length = len(blob)
    while index + 8 < length:
        if blob[index] != 0xFF:
            return None
        while index < length and blob[index] == 0xFF:
            index += 1
        if index >= length:
            return None
        marker = blob[index]
        index += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > length:
            return None
        segment = int.from_bytes(blob[index : index + 2], "big")
        if segment < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2) and index + 7 <= length:
            height = int.from_bytes(blob[index + 3 : index + 5], "big")
            width = int.from_bytes(blob[index + 5 : index + 7], "big")
            return width, height
        index += segment
    return None


def mark_image_only(conn: sqlite3.Connection, account_id: int | None = None) -> None:
    sql = _MARK_SQL
    params: list[int] = [TEXT_LIMIT, INLINE_IMAGE_FLOOR]
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params.append(account_id)
    conn.execute(sql, params)
