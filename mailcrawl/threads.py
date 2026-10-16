"""Thread flags derived from stored messages and attachments."""

from __future__ import annotations

import sqlite3

TEXT_LIMIT = 200
INLINE_IMAGE_FLOOR = 20 * 1024

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


def mark_image_only(conn: sqlite3.Connection, account_id: int | None = None) -> None:
    sql = _MARK_SQL
    params: list[int] = [TEXT_LIMIT, INLINE_IMAGE_FLOOR]
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params.append(account_id)
    conn.execute(sql, params)
