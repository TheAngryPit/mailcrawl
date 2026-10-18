#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Write decide JSONL from the queue and the fixture table.

The untrusted text in a queue item is never copied into a decision.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def decisions(queue: dict[str, object], table: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    items = queue.get("items")
    if not isinstance(items, list):
        raise SystemExit("queue json has no items")
    rows: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise SystemExit("queue item is not an object")
        thread_id = item.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise SystemExit("queue item has no thread_id")
        act = table.get(thread_id)
        if act is None:
            raise SystemExit(f"no fixture decision for {thread_id}")
        if "untrusted" in json.dumps(act):
            raise SystemExit("fixture decision copied queue text")
        row: dict[str, str] = {
            "action": act["action"],
            "source_detail": act["source_detail"],
            "thread_id": thread_id,
        }
        if act["action"] == "revive":
            row["kind"] = act["kind"]
            row["reason"] = act["reason"]
            row["state"] = act["state"]
        elif act["action"] == "set":
            row["lane"] = act["lane"]
            row["reason"] = act["reason"]
            row["state"] = act["state"]
        else:
            raise SystemExit(f"unsupported fixture action {act['action']}")
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    queue = json.loads(Path(args.queue).read_text(encoding="utf-8"))
    table = json.loads(Path(args.table).read_text(encoding="utf-8"))
    rows = decisions(queue, table)
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
