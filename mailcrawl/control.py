# SPDX-License-Identifier: MIT
"""Read-only health and CrawlBar metadata for the local archive."""

from __future__ import annotations

import datetime as dt
import sqlite3
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailcrawl.clock import as_of
from mailcrawl.paths import UsageError, canonical_alias
from mailcrawl.schema import TABLES, USER_VERSION

CONTROL_SCHEMA = "crawlkit.control.v1"
APP_ID = "mailcrawl"
_ALIASES = ("thehive", "vcl", "personal")
_COUNT_QUERIES = (
    ("messages", "Messages", "SELECT COUNT(*) FROM messages"),
    ("threads", "Threads", "SELECT COUNT(*) FROM threads"),
    ("senders", "Senders", "SELECT COUNT(*) FROM senders"),
    ("labels", "Labels", "SELECT COUNT(*) FROM labels"),
    ("attachments", "Attachments", "SELECT COUNT(*) FROM attachments"),
    ("imports", "Imports", "SELECT COUNT(*) FROM import_runs"),
)
_REQUIRED_COLUMNS = {
    "accounts": {"account_id", "alias"},
    "import_runs": {
        "run_id",
        "account_id",
        "finished_at",
        "n_seen",
        "n_new",
        "n_dup",
        "n_error",
    },
}
_SEARCH_REQUIRED_COLUMNS = {
    "accounts": {"account_id", "alias"},
    "messages": {
        "msg_pk", "gmail_message_id", "thread_pk", "internal_date", "from_hdr",
        "subject", "body_text",
    },
    "threads": {"thread_pk", "image_only"},
    "labels": {"account_id", "gmail_label_id", "name"},
    "message_labels": {"account_id", "gmail_label_id", "msg_pk"},
    "attachments": {"msg_pk", "mime_type"},
    "msg_fts": {"subject", "body_text", "from_hdr", "attachment_text"},
    "msg_trigram": {"subject", "body_text", "from_hdr", "attachment_text"},
}
_CHECK_PROGRESS_INTERVAL = 10_000
_CHECK_PROGRESS_CALL_LIMIT = 100


@dataclass(frozen=True)
class _FileStamp:
    exists: bool
    device: int = 0
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0
    ctime_ns: int = 0
    mode: int = 0

    @classmethod
    def read(cls, path: Path) -> _FileStamp:
        try:
            info = path.stat()
        except FileNotFoundError:
            return cls(False)
        return cls(
            True,
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode & 0o777,
        )


@dataclass
class _DatabaseProbe:
    alias: str
    state: str
    row: dict[str, Any]
    warnings: list[str]
    integrity: str
    counts: dict[str, int] | None = None
    freshness: dict[str, Any] | None = None


def metadata() -> dict[str, Any]:
    """Return the static, secret-free CrawlBar manifest."""
    commands = {
        "metadata": _command(["mailcrawl", "metadata", "--json"]),
        "status": _command(
            ["mailcrawl", "status", "--account", "{config:account}", "--json"]
        ),
        "doctor": _command(
            ["mailcrawl", "doctor", "--account", "{config:account}", "--json"]
        ),
        "sync": _command(
            ["mailcrawl", "sync", "--account", "{config:account}", "--json"],
            mutates=True,
        ),
        "search": _command(
            ["mailcrawl", "--json", "search", "--account", "{config:account}", "--read-only"]
        ),
    }
    return {
        "schema_version": CONTROL_SCHEMA,
        "id": APP_ID,
        "display_name": "Mailcrawl",
        "description": "A local archive for private email.",
        "binary": {"name": "mailcrawl"},
        "paths": {
            "default_config": "~/.config/mailcrawl/config.toml",
        },
        "commands": commands,
        "capabilities": ["status", "doctor", "sync", "search"],
        "config_options": [
            {
                "id": "account",
                "label": "Mail account",
                "kind": "choice",
                "default_value": "personal",
                "choices": ["personal", "thehive", "vcl"],
            }
        ],
        "privacy": {
            "contains_private_messages": True,
            "exports_secrets": False,
            "local_only_scopes": ["email archive", "local search"],
        },
    }


def status(home: Path, config: Path, account: str | None = None) -> dict[str, Any]:
    """Return fast local status without creating, migrating, or chmodding files."""
    return _health(Path(home), Path(config), account, run_doctor=False)


def doctor(home: Path, config: Path, account: str | None = None) -> dict[str, Any]:
    """Validate local config and database integrity without repairing either."""
    return _health(Path(home), Path(config), account, run_doctor=True)


@contextmanager
def readonly_archive(path: Path, *, account: str | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a current-schema archive through a stable immutable SQLite view."""
    database = Path(path).expanduser()
    if not database.is_file():
        raise UsageError("account is not initialized")
    try:
        database = database.resolve(strict=True)
        before = _file_stamps(_archive_paths(database))
    except OSError:
        raise UsageError("database is unavailable") from None

    if before[1].size > 0:
        raise UsageError("database WAL requires maintenance")
    if before[3].size > 0:
        raise UsageError("database journal requires maintenance")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != USER_VERSION:
            raise UsageError("database schema is incompatible")
        names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not set(TABLES) <= names:
            raise UsageError("database schema is incomplete")
        for table, required in _SEARCH_REQUIRED_COLUMNS.items():
            columns = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if not required <= columns:
                raise UsageError("database schema is incomplete")
        if account is not None and conn.execute(
            "SELECT 1 FROM accounts WHERE alias = ?", (account,)
        ).fetchone() is None:
            raise UsageError("account database does not match selection")
        yield conn
    finally:
        if conn is not None:
            conn.close()
        try:
            after = _file_stamps(_archive_paths(database))
        except OSError:
            after = None
        if after != before:
            raise UsageError("database changed during read")


def _archive_paths(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    )


def _health(home: Path, config: Path, account: str | None, *, run_doctor: bool) -> dict[str, Any]:
    now = as_of(None)
    try:
        selected = canonical_alias(account) if account is not None else None
    except UsageError:
        return _document(
            state="error",
            warnings=["invalid_account"],
            summary="The selected account is not supported.",
            checks={"config": "not_checked", "databases": []} if run_doctor else None,
        )

    config_state, sources = _inspect_config(config, selected)
    warnings: list[str] = []
    if config_state == "missing":
        warnings.append("config_missing")
    elif config_state == "invalid":
        warnings.append("config_invalid")
    elif config_state == "incomplete":
        warnings.append("config_account_uninitialized")

    aliases = (selected,) if selected else _ALIASES
    probes = [
        _inspect_database(
            home.expanduser() / f"{alias}.db",
            alias,
            sources=sources,
            now=now,
            run_doctor=run_doctor,
        )
        for alias in aliases
    ]
    for probe in probes:
        warnings.extend(probe.warnings)

    ready = [probe for probe in probes if probe.state == "ready"]
    source_rows: list[dict[str, Any]] = []
    for probe in ready:
        if probe.freshness:
            source_rows.extend(probe.freshness.get("sources", []))
    source_warning_codes = {
        "failed": "source_refresh_failed",
        "invalid_receipt": "source_receipt_invalid",
        "stale": "source_refresh_stale",
        "never_imported": "source_never_imported",
    }
    source_health_degraded = False
    for source_row in source_rows:
        if not source_row.get("enabled", True):
            continue
        code = source_warning_codes.get(source_row.get("state"))
        if code:
            warnings.append(code)
            source_health_degraded = True

    if any(probe.state == "needs_maintenance" for probe in probes):
        state = "needs_maintenance"
    elif not ready:
        state = "needs_setup"
    elif selected and probes[0].state == "missing":
        state = "needs_setup"
    elif config_state in ("missing", "incomplete"):
        state = "needs_setup"
    elif config_state == "invalid" or source_health_degraded:
        state = "degraded"
    else:
        state = "ready"

    if selected and probes and probes[0].state == "missing":
        warnings.append("database_missing")
    elif not ready and all(probe.state == "missing" for probe in probes):
        warnings.append("no_initialized_accounts")

    counts_complete = bool(ready) and all(probe.state == "ready" for probe in probes)
    aggregate_counts = _sum_counts(ready) if ready else {}
    latest_import = max(
        (stamp for probe in ready if probe.freshness
         if (stamp := probe.freshness.get("last_import_at")) is not None),
        default=None,
    )
    checks = None
    if run_doctor:
        checks = {
            "config": config_state,
            "databases": [
                {"id": probe.alias, "integrity": probe.integrity}
                for probe in probes
            ],
        }

    return _document(
        state=state,
        warnings=warnings,
        summary=_summary(state),
        counts=_count_rows(aggregate_counts),
        counts_complete=counts_complete,
        databases=[probe.row for probe in probes],
        freshness={
            "last_import_at": latest_import,
            "sources": source_rows,
            "complete": all(probe.state == "ready" for probe in probes),
        },
        checks=checks,
    )


def _inspect_config(config: Path, selected: str | None) -> tuple[str, tuple[Any, ...]]:
    try:
        if not config.is_file():
            return "missing", ()
        document = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return "invalid", ()
    if not isinstance(document, dict):
        return "invalid", ()

    accounts = document.get("accounts", {})
    if not isinstance(accounts, dict):
        return "invalid", ()
    incomplete = not bool(accounts)
    try:
        for alias, row in accounts.items():
            if not isinstance(alias, str):
                return "invalid", ()
            canonical_alias(alias)
            if not isinstance(row, dict):
                return "invalid", ()
            email = row.get("email")
            if email is not None and not isinstance(email, str):
                return "invalid", ()
            if not isinstance(email, str) or not email.strip():
                incomplete = True
        if selected is not None:
            row = accounts.get(selected)
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("email"), str)
                or not row["email"].strip()
            ):
                return "incomplete", ()
        if "sources" not in document:
            return ("incomplete" if incomplete else "ok"), ()
        from mailcrawl.sources import configured_sources

        sources = configured_sources(config)
        for source in sources:
            row = accounts.get(source.account)
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("email"), str)
                or not row["email"].strip()
            ):
                return "incomplete", ()
        return ("incomplete" if incomplete else "ok"), sources
    except (UsageError, OSError, UnicodeError, ValueError):
        return "invalid", ()


def _inspect_database(
    path: Path,
    alias: str,
    *,
    sources: tuple[Any, ...],
    now: int,
    run_doctor: bool,
) -> _DatabaseProbe:
    wal_path = Path(str(path) + "-wal")
    shm_path = Path(str(path) + "-shm")
    journal_path = Path(str(path) + "-journal")
    try:
        before = _file_stamps((path, wal_path, shm_path, journal_path))
    except OSError:
        return _probe(alias, "needs_maintenance", "database_unavailable", before=None)

    if not before[0].exists:
        if before[1].exists or before[2].exists:
            return _probe(alias, "needs_maintenance", "orphaned_database_sidecar", before=before)
        return _probe(alias, "missing", None, before=before)

    if before[1].size > 0:
        return _probe(alias, "needs_maintenance", "sqlite_wal_requires_maintenance", before=before)
    if before[3].size > 0:
        return _probe(
            alias,
            "needs_maintenance",
            "sqlite_journal_requires_maintenance",
            before=before,
        )

    conn: sqlite3.Connection | None = None
    warnings: list[str] = []
    integrity = "not_run"
    counts: dict[str, int] | None = None
    freshness: dict[str, Any] | None = None
    state = "ready"
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version != USER_VERSION:
            code = (
                "database_schema_outdated"
                if user_version < USER_VERSION
                else "database_schema_newer"
            )
            return _probe(
                alias,
                "needs_maintenance",
                code,
                before=before,
                schema_version=user_version,
            )

        names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not set(TABLES) <= names:
            return _probe(
                alias,
                "needs_maintenance",
                "database_schema_incomplete",
                before=before,
                schema_version=user_version,
            )
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                return _probe(
                    alias,
                    "needs_maintenance",
                    "database_schema_incomplete",
                    before=before,
                    schema_version=user_version,
                )
        account_row = conn.execute("SELECT 1 FROM accounts WHERE alias = ?", (alias,)).fetchone()
        if account_row is None:
            return _probe(
                alias,
                "needs_maintenance",
                "database_account_mismatch",
                before=before,
                schema_version=user_version,
            )

        counts = {
            key: int(conn.execute(query).fetchone()[0])
            for key, _label, query in _COUNT_QUERIES
        }
        latest = conn.execute(
            "SELECT finished_at, n_seen, n_new, n_dup, n_error FROM import_runs "
            "WHERE finished_at IS NOT NULL "
            "ORDER BY finished_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
        last_import_at = _epoch(latest["finished_at"]) if latest is not None else None
        total_errors = int(
            conn.execute(
                "SELECT COALESCE(SUM(COALESCE(n_error, 0)), 0) FROM import_runs "
                "WHERE finished_at IS NOT NULL"
            ).fetchone()[0]
        )
        latest_run = None
        if latest is not None:
            latest_run = {
                "n_seen": _integer(latest["n_seen"]),
                "n_new": _integer(latest["n_new"]),
                "n_dup": _integer(latest["n_dup"]),
                "n_error": _integer(latest["n_error"]),
            }

        source_rows = []
        if sources:
            from mailcrawl.sources import source_status

            for source in sources:
                if source.account == alias:
                    source_rows.append(source_status(conn, source, now))
        receipt_times = [
            stamp
            for source_row in source_rows
            if (stamp := _epoch(source_row.get("last_import_at"))) is not None
        ]
        if receipt_times:
            last_import_at = max([last_import_at, *receipt_times], key=lambda value: value or 0)
        freshness = {
            "last_import_at": last_import_at,
            "latest_run": latest_run,
            "import_errors_total": total_errors,
            "sources": source_rows,
        }

        if run_doctor:
            progress_calls = 0

            def interrupt_after_budget() -> int:
                nonlocal progress_calls
                progress_calls += 1
                return int(progress_calls > _CHECK_PROGRESS_CALL_LIMIT)

            conn.set_progress_handler(interrupt_after_budget, _CHECK_PROGRESS_INTERVAL)
            try:
                result = conn.execute("PRAGMA quick_check(1)").fetchone()
            except sqlite3.OperationalError as exc:
                if (
                    progress_calls <= _CHECK_PROGRESS_CALL_LIMIT
                    and "interrupt" not in str(exc).casefold()
                ):
                    raise
                result = None
                integrity = "incomplete"
                state = "needs_maintenance"
                warnings.append("database_integrity_check_incomplete")
            finally:
                conn.set_progress_handler(None, 0)
            if integrity != "incomplete":
                integrity = "ok" if result is not None and result[0] == "ok" else "failed"
            if integrity == "failed":
                state = "needs_maintenance"
                warnings.append("database_integrity_check_failed")
    except sqlite3.DatabaseError as exc:
        state = "needs_maintenance"
        warnings.append(_sqlite_error_code(exc))
        integrity = "failed" if run_doctor else "not_run"
        counts = None
        freshness = None
    except (OSError, RuntimeError, ValueError, OverflowError):
        state = "needs_maintenance"
        warnings.append("database_unavailable")
        integrity = "failed" if run_doctor else "not_run"
        counts = None
        freshness = None
    finally:
        if conn is not None:
            conn.close()

    try:
        after = _file_stamps((path, wal_path, shm_path, journal_path))
    except OSError:
        after = None
    if after != before:
        state = "needs_maintenance"
        warnings.append("database_changed_during_read")
        integrity = "incomplete" if run_doctor else integrity
        counts = None
        freshness = None

    if state == "needs_maintenance" and not warnings:
        warnings.append("database_unavailable")
    row = _database_row(alias, before, state, counts, freshness)
    if after is None:
        row["state"] = "needs_maintenance"
    return _DatabaseProbe(alias, state, row, warnings, integrity, counts, freshness)


def _probe(
    alias: str,
    state: str,
    warning: str | None,
    *,
    before: tuple[_FileStamp, ...] | None,
    schema_version: int | None = None,
) -> _DatabaseProbe:
    stamps = before or (
        _FileStamp(False),
        _FileStamp(False),
        _FileStamp(False),
        _FileStamp(False),
    )
    warnings = [warning] if warning else []
    row = _database_row(alias, stamps, state, None, None)
    if schema_version is not None:
        row["user_version"] = schema_version
    return _DatabaseProbe(alias, state, row, warnings, "skipped")


def _database_row(
    alias: str,
    stamps: tuple[_FileStamp, ...],
    state: str,
    counts: dict[str, int] | None,
    freshness: dict[str, Any] | None,
) -> dict[str, Any]:
    db, wal, shm, journal = stamps
    row: dict[str, Any] = {
        "id": alias,
        "label": alias.capitalize(),
        "kind": "sqlite",
        "role": "account",
        "is_primary": True,
        "bytes": db.size if db.exists else 0,
        "wal_bytes": wal.size if wal.exists else 0,
        "shm_bytes": shm.size if shm.exists else 0,
        "journal_bytes": journal.size if journal.exists else 0,
        "modified_at": _iso_timestamp(db.mtime_ns // 1_000_000_000) if db.exists else None,
        "state": state,
    }
    if counts is not None:
        row["counts"] = _count_rows(counts)
    if freshness is not None:
        row["freshness"] = freshness
    return row


def _file_stamps(paths: tuple[Path, ...]) -> tuple[_FileStamp, ...]:
    return tuple(_FileStamp.read(path) for path in paths)


def _sum_counts(probes: list[_DatabaseProbe]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for probe in probes:
        if probe.counts is None:
            continue
        for key, value in probe.counts.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _count_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    labels = {key: label for key, label, _query in _COUNT_QUERIES}
    return [
        {"id": key, "label": labels[key], "value": value}
        for key, value in counts.items()
        if key in labels
    ]


def _document(
    *,
    state: str,
    warnings: list[str],
    summary: str,
    counts: list[dict[str, Any]] | None = None,
    counts_complete: bool | None = None,
    databases: list[dict[str, Any]] | None = None,
    freshness: dict[str, Any] | None = None,
    checks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": CONTROL_SCHEMA,
        "app_id": APP_ID,
        "generated_at": _iso_timestamp(as_of(None)),
        "state": state,
        "summary": summary,
        "counts": counts or [],
        "databases": databases or [],
        "freshness": freshness or {"last_import_at": None, "sources": [], "complete": False},
        "warnings": [{"code": code} for code in sorted(set(warnings))],
        "ok": state == "ready",
    }
    if counts_complete is not None:
        result["counts_complete"] = counts_complete
    if checks is not None:
        result["checks"] = checks
    return result


def _summary(state: str) -> str:
    return {
        "ready": "Local archive status is ready.",
        "needs_setup": "Set up a local account before importing.",
        "needs_maintenance": (
            "A local database needs attention before its status can be read safely."
        ),
        "degraded": "Configuration or source refresh needs attention.",
        "error": "The requested account could not be checked.",
    }.get(state, "Local archive status is unavailable.")


def _command(argv: list[str], *, mutates: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"argv": argv, "json": True}
    if mutates:
        result["mutates"] = True
    return result


def _epoch(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    try:
        dt.datetime.fromtimestamp(value, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return value


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _iso_timestamp(value: int) -> str | None:
    try:
        return (
            dt.datetime.fromtimestamp(value, dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _sqlite_error_code(exc: sqlite3.DatabaseError) -> str:
    message = str(exc).casefold()
    if "malformed" in message or "not a database" in message or "disk image" in message:
        return "database_corrupt"
    return "database_unavailable"
