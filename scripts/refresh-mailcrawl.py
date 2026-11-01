#!/usr/bin/env python3
import argparse
import json
import os
import signal
import sqlite3
from pathlib import Path
import subprocess
import sys

from mailcrawl.paths import UsageError, config_path, data_dir
from mailcrawl.sources import configured_sources, sync_sources


def refresh(home: Path, config: Path, source_id: str, producer: Path,
            timeout: int = 3600) -> dict:
    matches = [source for source in configured_sources(config) if source.id == source_id]
    if not matches or not matches[0].enabled or matches[0].kind != "gog-export":
        raise UsageError("producer handoff requires an enabled configured gog-export source")
    if not producer.is_absolute() or not producer.is_file() or not os.access(producer, os.X_OK):
        raise UsageError("producer must be an explicitly selected executable absolute path")
    if type(timeout) is not int or timeout <= 0:
        raise UsageError("producer timeout must be positive seconds")
    if os.name != "posix":
        raise UsageError("the optional producer wrapper requires POSIX process-group cleanup")
    process = None
    try:
        process = subprocess.Popen([str(producer), "--out", str(matches[0].path)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        code = process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "source": source_id, "stage": "export", "error": "producer_failed"}
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    if code:
        return {"ok": False, "source": source_id, "stage": "export", "error": "producer_failed"}
    try:
        return sync_sources(home, config, source_id=source_id, expected_source=matches[0])
    except UsageError:
        return {"ok": False, "source": source_id, "stage": "handoff", "error": "source_changed"}


def _terminate(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser(
        description="Run an operator-selected export producer, then import its completed local output."
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--producer", required=True, type=Path)
    parser.add_argument("--home")
    parser.add_argument("--config")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        result = refresh(data_dir(args.home), config_path(args.config), args.source,
                         args.producer, args.timeout)
    except (UsageError, ValueError, OSError, sqlite3.Error):
        result = {"ok": False, "stage": "configuration", "error": "invalid_configuration"}
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
