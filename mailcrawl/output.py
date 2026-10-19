"""Encode external text at human-facing output boundaries."""

from __future__ import annotations


def terminal_text(value: object) -> str:
    return "".join(
        f"\\x{ord(char):02x}" if ord(char) < 32 or 127 <= ord(char) <= 159 else char
        for char in str(value)
    )


def spreadsheet_cell(value: object) -> object:
    if not isinstance(value, str):
        return value
    leading = value.lstrip()
    if leading.startswith(("=", "+", "-", "@")) or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    ):
        return "'" + value
    return value
