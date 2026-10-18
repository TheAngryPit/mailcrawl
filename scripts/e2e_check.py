#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Snapshot one archive and diff it against the expected journey."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path


def snapshot(db: Path) -> dict[str, object]:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        threads = []
        rows = conn.execute(
            """
            SELECT threads.gmail_thread_id AS thread_id,
                   messages.rfc822_message_id AS message_id,
                   messages.is_from_me AS from_me,
                   messages.from_me_origin AS from_me_origin,
                   messages.bcc_hdr AS bcc,
                   decisions.next_state AS state,
                   decisions.lane AS lane,
                   decisions.origin AS origin,
                   decisions.rule_id AS rule_id,
                   decisions.evidence AS evidence
            FROM messages
            JOIN threads ON threads.thread_pk = messages.thread_pk
            LEFT JOIN decisions
              ON decisions.thread_pk = threads.thread_pk
             AND decisions.superseded_by IS NULL
            ORDER BY threads.gmail_thread_id, messages.rfc822_message_id
            """
        ).fetchall()
        labels = conn.execute(
            """
            SELECT threads.gmail_thread_id AS thread_id, message_labels.gmail_label_id AS label_id
            FROM message_labels
            JOIN messages ON messages.msg_pk = message_labels.msg_pk
            JOIN threads ON threads.thread_pk = messages.thread_pk
            ORDER BY threads.gmail_thread_id, message_labels.gmail_label_id
            """
        ).fetchall()
        files = conn.execute(
            """
            SELECT threads.gmail_thread_id AS thread_id, attachments.filename AS filename
            FROM attachments
            JOIN messages ON messages.msg_pk = attachments.msg_pk
            JOIN threads ON threads.thread_pk = messages.thread_pk
            ORDER BY threads.gmail_thread_id, attachments.filename
            """
        ).fetchall()
    finally:
        conn.close()
    by_thread: dict[str, dict[str, object]] = {}
    for row in rows:
        thread_id = str(row["thread_id"])
        evidence = row["evidence"]
        kind = ""
        if isinstance(evidence, str) and evidence:
            try:
                parsed = json.loads(evidence)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("type") == "revive":
                        found = item.get("kind")
                        if isinstance(found, str):
                            kind = found
        by_thread[thread_id] = {
            "bcc": row["bcc"],
            "from_me": int(row["from_me"] or 0),
            "from_me_origin": row["from_me_origin"],
            "kind": kind,
            "labels": [],
            "lane": row["lane"],
            "message_id": row["message_id"],
            "origin": row["origin"],
            "rule_id": row["rule_id"],
            "state": row["state"],
            "thread_id": thread_id,
        }
    for row in labels:
        by_thread[str(row["thread_id"])]["labels"].append(str(row["label_id"]))
    for row in files:
        item = by_thread[str(row["thread_id"])]
        names = item.setdefault("attachments", [])
        if isinstance(names, list):
            names.append(None if row["filename"] is None else str(row["filename"]))
    threads = [by_thread[key] for key in sorted(by_thread)]
    return {"threads": threads}


def queue_ids(path: Path) -> list[str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    items = document.get("items")
    if not isinstance(items, list):
        raise SystemExit(f"{path} has no items")
    found = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("thread_id"), str):
            raise SystemExit(f"{path} item has no thread_id")
        found.append(item["thread_id"])
    return sorted(found)


def export_ids(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return sorted(str(row["gmail_thread_id"]) for row in rows)


def canon(document: object) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


_THREAD_SECTIONS = frozenset({"final", "revived"})
_ID_LISTS = frozenset(
    {
        "export_ids",
        "queue_off",
        "queue_off_after_undo",
        "queue_on",
        "queue_on_after_undo",
    }
)


def _show(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _thread_map(section: object) -> dict[str, dict[str, object]] | None:
    if not isinstance(section, dict):
        return None
    rows = section.get("threads")
    if not isinstance(rows, list):
        return None
    found: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("thread_id"), str):
            return None
        found[row["thread_id"]] = row
    return found


def _id_list(value: object) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def _counts(ids: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for thread_id in ids:
        found[thread_id] = found.get(thread_id, 0) + 1
    return found


def _thread_rows(section: object) -> list[dict[str, object]] | None:
    if not isinstance(section, dict):
        return None
    rows = section.get("threads")
    if not isinstance(rows, list):
        return None
    found: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("thread_id"), str):
            return None
        found.append(row)
    return found


def _membership(section: str, thread_id: str, expected_count: int, actual_count: int) -> str:
    if expected_count > 1 or actual_count > 1:
        return (
            f"thread {thread_id}: {section} "
            f"expected count {expected_count} actual {actual_count}"
        )
    if expected_count == 1:
        return f"thread {thread_id}: {section} expected present actual absent"
    return f"thread {thread_id}: {section} expected absent actual present"


def _order_line(section: str, actual_ids: list[str], expected_ids: list[str]) -> str | None:
    if _counts(actual_ids) != _counts(expected_ids) or actual_ids == expected_ids:
        return None
    for index, (left, right) in enumerate(zip(actual_ids, expected_ids)):
        if left != right:
            return f"{section}: order differs at index {index}"
    return None


def _field_lines(
    section: str,
    thread_id: str,
    actual_row: dict[str, object],
    expected_row: dict[str, object],
) -> list[str]:
    lines: list[str] = []
    for field in sorted((set(actual_row) | set(expected_row)) - {"thread_id"}):
        if actual_row.get(field) != expected_row.get(field):
            lines.append(
                f"thread {thread_id}: {section}.{field} "
                f"expected {_show(expected_row.get(field))} actual {_show(actual_row.get(field))}"
            )
    return lines


def _finish(section: str, actual: object, expected: object, lines: list[str]) -> list[str]:
    if actual != expected and not lines:
        lines.append(f"{section}: differs")
    return lines


def _threads(section: str, actual: object, expected: object) -> list[str]:
    actual_rows = _thread_rows(actual)
    expected_rows = _thread_rows(expected)
    if actual_rows is None or expected_rows is None:
        if actual == expected:
            return []
        return [f"{section} expected {_show(expected)} actual {_show(actual)}"]
    actual_ids = [str(row["thread_id"]) for row in actual_rows]
    expected_ids = [str(row["thread_id"]) for row in expected_rows]
    actual_counts = _counts(actual_ids)
    expected_counts = _counts(expected_ids)
    actual_grouped: dict[str, list[dict[str, object]]] = {}
    expected_grouped: dict[str, list[dict[str, object]]] = {}
    for row in actual_rows:
        actual_grouped.setdefault(str(row["thread_id"]), []).append(row)
    for row in expected_rows:
        expected_grouped.setdefault(str(row["thread_id"]), []).append(row)
    lines: list[str] = []
    for thread_id in sorted(set(actual_counts) | set(expected_counts)):
        actual_count = actual_counts.get(thread_id, 0)
        expected_count = expected_counts.get(thread_id, 0)
        shared = min(actual_count, expected_count)
        for index in range(shared):
            lines.extend(
                _field_lines(
                    section,
                    thread_id,
                    actual_grouped[thread_id][index],
                    expected_grouped[thread_id][index],
                )
            )
        if actual_count != expected_count:
            lines.append(_membership(section, thread_id, expected_count, actual_count))
    order = _order_line(section, actual_ids, expected_ids)
    if order:
        lines.append(order)
    return _finish(section, actual, expected, lines)


def _ids(name: str, actual: object, expected: object) -> list[str]:
    actual_ids = _id_list(actual)
    expected_ids = _id_list(expected)
    if actual_ids is None or expected_ids is None:
        if actual == expected:
            return []
        return [f"{name} expected {_show(expected)} actual {_show(actual)}"]
    actual_counts = _counts(actual_ids)
    expected_counts = _counts(expected_ids)
    lines: list[str] = []
    for thread_id in sorted(set(actual_counts) | set(expected_counts)):
        actual_count = actual_counts.get(thread_id, 0)
        expected_count = expected_counts.get(thread_id, 0)
        if actual_count != expected_count:
            lines.append(_membership(name, thread_id, expected_count, actual_count))
    order = _order_line(name, actual_ids, expected_ids)
    if order:
        lines.append(order)
    return _finish(name, actual, expected, lines)


def _walk(path: str, actual: object, expected: object) -> list[str]:
    if actual == expected:
        return []
    if path in _THREAD_SECTIONS:
        return _threads(path, actual, expected)
    if path in _ID_LISTS:
        return _ids(path, actual, expected)
    if isinstance(actual, dict) and isinstance(expected, dict):
        lines: list[str] = []
        for key in sorted(set(actual) | set(expected)):
            child = f"{path}.{key}" if path else key
            lines.extend(_walk(child, actual.get(key), expected.get(key)))
        return lines
    label = path or "document"
    return [f"{label} expected {_show(expected)} actual {_show(actual)}"]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _unreported(actual: object, expected: object) -> list[str]:
    if isinstance(actual, dict) and isinstance(expected, dict):
        lines = [
            f"{key}: differs"
            for key in sorted(set(actual) | set(expected))
            if _canonical(actual.get(key)) != _canonical(expected.get(key))
        ]
        if lines:
            return lines
    return ["document: differs"]


def compare(actual: object, expected: object) -> str:
    lines = _walk("", actual, expected)
    if _canonical(actual) != _canonical(expected) and not lines:
        lines = _unreported(actual, expected)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


_INJECT_ID = "<e2e-header-injection@example.com>"
_TRAVERSAL_ID = "<e2e-traversal@example.com>"
_UNDO_THREAD = "e2e-en-old-ask"
_ESCAPE_NAMES = (
    "../../tmp/mailcrawl-e2e-escape.png",
    "/tmp/mailcrawl-e2e-absolute.png",
)


def journey_guards(actual: object) -> list[str]:
    if not isinstance(actual, dict):
        return ["document expected an object"]
    lines: list[str] = []
    for section in ("final", "revived"):
        mapped = _thread_map(actual.get(section))
        if mapped is None:
            lines.append(f"{section} expected threads actual {_show(actual.get(section))}")
            continue
        injection = [
            row for row in mapped.values() if row.get("message_id") == _INJECT_ID
        ]
        if not injection:
            lines.append(f"thread e2e-header-injection: {section} expected present actual absent")
        for row in injection:
            if row.get("bcc") is not None:
                lines.append(
                    f"thread {row['thread_id']}: {section}.bcc "
                    f"expected null actual {_show(row.get('bcc'))}"
                )
        traversal = [
            row for row in mapped.values() if row.get("message_id") == _TRAVERSAL_ID
        ]
        if not traversal:
            lines.append(f"thread e2e-traversal: {section} expected present actual absent")
        for row in traversal:
            names = row.get("attachments")
            for wanted in _ESCAPE_NAMES:
                if not isinstance(names, list) or wanted not in names:
                    lines.append(
                        f"thread {row['thread_id']}: {section}.attachments "
                        f"expected {_show(wanted)} actual {_show(names)}"
                    )
    for key in ("queue_off_after_undo", "queue_on_after_undo"):
        found = _id_list(actual.get(key))
        if found is None or _UNDO_THREAD not in found:
            lines.append(f"thread {_UNDO_THREAD}: {key} expected present actual absent")
    return lines


def assemble(
    db: Path,
    queue_off: Path,
    queue_on: Path,
    revived_db: Path,
    export_path: Path,
    native_import: Path,
    mbox_import: Path,
    queue_off_after_undo: Path,
    queue_on_after_undo: Path,
) -> dict[str, object]:
    native = json.loads(native_import.read_text(encoding="utf-8"))
    mbox = json.loads(mbox_import.read_text(encoding="utf-8"))
    revived = snapshot(revived_db)
    return {
        "export_ids": export_ids(export_path),
        "final": snapshot(db),
        "imports": {
            "mbox": {"n_new": mbox.get("n_new"), "n_refused": mbox.get("n_refused")},
            "native": {"n_new": native.get("n_new"), "n_refused": native.get("n_refused")},
        },
        "queue_off": queue_ids(queue_off),
        "queue_off_after_undo": queue_ids(queue_off_after_undo),
        "queue_on": queue_ids(queue_on),
        "queue_on_after_undo": queue_ids(queue_on_after_undo),
        "revived": revived,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--revived-db", required=True)
    parser.add_argument("--queue-off", required=True)
    parser.add_argument("--queue-on", required=True)
    parser.add_argument("--queue-off-after-undo", required=True)
    parser.add_argument("--queue-on-after-undo", required=True)
    parser.add_argument("--export", required=True)
    parser.add_argument("--native-import", required=True)
    parser.add_argument("--mbox-import", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    actual = assemble(
        Path(args.db),
        Path(args.queue_off),
        Path(args.queue_on),
        Path(args.revived_db),
        Path(args.export),
        Path(args.native_import),
        Path(args.mbox_import),
        Path(args.queue_off_after_undo),
        Path(args.queue_on_after_undo),
    )
    guarded = journey_guards(actual)
    if guarded:
        print("\n".join(guarded))
        raise SystemExit(1)
    expected_path = Path(args.expected)
    if args.record:
        expected_path.write_text(canon(actual), encoding="utf-8")
        return
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    diff = compare(actual, expected)
    if diff:
        print(diff, end="")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
