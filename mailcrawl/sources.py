"""Configured local export sources and transactional refresh receipts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from mailcrawl.clock import as_of
from mailcrawl import inputio
from mailcrawl.configfile import account_email, load_config, owner_addresses, takeout_label_map
from mailcrawl.ingest import ImportResult, import_gog_export, import_mbox
from mailcrawl.paths import UsageError, canonical_alias
from mailcrawl.store import connect
from mailcrawl.taxonomy import resolve_taxonomy

_SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_COUNT_FIELDS = ("n_seen", "n_new", "n_dup", "n_error", "n_refused", "n_reconciled")


@dataclass(frozen=True)
class SourceSpec:
    id: str
    account: str
    kind: str
    path: Path
    stale_after_seconds: int = 86400
    enabled: bool = True
    limits: inputio.ImportLimits | None = None


def configured_sources(config: Path) -> tuple[SourceSpec, ...]:
    block = load_config(config).get("sources", {})
    if not isinstance(block, dict):
        raise UsageError("sources needs a table")
    result = []
    for name, row in block.items():
        if not isinstance(name, str) or not _SOURCE_ID.fullmatch(name) or not isinstance(row, dict):
            raise UsageError("source needs a stable lowercase identifier and table")
        if set(row) - {"account", "kind", "path", "stale_after_seconds", "enabled", "limits"}:
            raise UsageError("source contains unsupported settings")
        if not isinstance(row.get("account"), str):
            raise UsageError("source needs an account")
        account = canonical_alias(row["account"])
        if row.get("kind") not in ("gog-export", "mbox"):
            raise UsageError("source kind must be gog-export or mbox")
        if not isinstance(row.get("path"), str) or not row["path"].strip():
            raise UsageError("source needs a local path")
        if "\x00" in row["path"]:
            raise UsageError("source path is invalid")
        path = Path(row["path"]).expanduser()
        if not path.is_absolute():
            path = config.parent / path
        stale = row.get("stale_after_seconds", 86400)
        enabled = row.get("enabled", True)
        if type(stale) is not int or stale <= 0 or type(enabled) is not bool:
            raise UsageError("source freshness must be positive seconds and enabled must be boolean")
        limits = row.get("limits", {})
        allowed_limits = {"max_total_bytes", "max_records", "max_image_bytes", "max_extract_bytes"}
        if not isinstance(limits, dict) or set(limits) - allowed_limits:
            raise UsageError("source limits contains unsupported settings")
        if any(type(value) is not int or value <= 0 for value in limits.values()):
            raise UsageError("source limits must be positive integers")
        base = (inputio.DEFAULT_GOG_IMPORT_LIMITS if row["kind"] == "gog-export"
                else inputio.DEFAULT_MBOX_IMPORT_LIMITS)
        result.append(SourceSpec(name, account, row["kind"], path.absolute(), stale, enabled,
                                 replace(base, **limits)))
    return tuple(sorted(result, key=lambda source: source.id))


def _receipt(conn: sqlite3.Connection, account: str, stream: str, document: dict, now: int) -> None:
    row = conn.execute("SELECT account_id FROM accounts WHERE alias = ?", (account,)).fetchone()
    if row is None:
        raise UsageError("source account is not initialized")
    conn.execute(
        "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(account_id, stream) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (row[0], stream, json.dumps(document, sort_keys=True), now),
    )


def _counts(result: ImportResult) -> dict[str, int]:
    return {key: getattr(result, key) for key in _COUNT_FIELDS}


def source_digest(source: SourceSpec) -> str:
    return hashlib.sha256(json.dumps({"id": source.id, "account": source.account,
        "kind": source.kind, "path": str(source.path),
        "limits": asdict(source.limits) if source.limits else None}, sort_keys=True).encode()).hexdigest()


def source_status(conn: sqlite3.Connection, source: SourceSpec, now: int) -> dict[str, object]:
    result = {"id": source.id, "account": source.account, "state": "never_imported",
              "local_only": True, "enabled": source.enabled}
    for prefix in ("local-export:", "local-export-attempt:"):
        row = conn.execute(
            "SELECT value FROM cursors WHERE account_id = (SELECT account_id FROM accounts "
            "WHERE alias = ?) AND stream = ?", (source.account, prefix + source.id),
        ).fetchone()
        if row is None:
            continue
        try:
            document = json.loads(row[0])
            if not isinstance(document, dict):
                raise ValueError("invalid receipt")
            if prefix == "local-export:":
                stamp = document.get("last_import_at")
                if (document.get("schema_version") != "mailcrawl.refresh.v1" or
                    document.get("status") != "success" or type(stamp) is not int or stamp > now):
                    raise ValueError("invalid receipt")
                result["last_import_at"] = stamp
                counts = document.get("counts", {})
                if isinstance(counts, dict):
                    result["counts"] = {key: counts[key] for key in _COUNT_FIELDS
                                        if type(counts.get(key)) is int and counts[key] >= 0}
                result["state"] = "stale" if (now - stamp > source.stale_after_seconds or
                    document.get("source_spec_sha256") != source_digest(source)) else "current_local"
            else:
                stamp = document.get("last_attempt_at")
                if type(stamp) is not int or stamp > now or document.get("status") not in ("failed", "success"):
                    raise ValueError("invalid attempt")
                result["last_attempt_at"] = stamp
                if document["status"] == "failed":
                    result["state"] = "failed"
        except (ValueError, TypeError):
            result["state"] = "invalid_receipt"
    if not source.enabled:
        result["state"] = "disabled"
    return result


class IncompleteSource(ValueError):
    pass


def sync_sources(
    home: Path,
    config: Path,
    *,
    source_id: str | None = None,
    account: str | None = None,
    expected_source: SourceSpec | None = None,
) -> dict[str, object]:
    sources = configured_sources(config)
    if expected_source is not None:
        configured = next(
            (source for source in sources if source.id == expected_source.id), None
        )
        if configured != expected_source:
            raise UsageError("configured source changed before sync")
        if source_id is not None and source_id != expected_source.id:
            raise UsageError("expected source does not match selection")
        if account is not None and account != expected_source.account:
            raise UsageError("expected source does not match selection")
    if source_id is not None and not any(source.id == source_id for source in sources):
        raise UsageError("unknown configured source")
    selected = [source for source in sources if source.enabled and
                (source_id is None or source.id == source_id) and
                (account is None or source.account == account)]
    if not selected:
        raise UsageError("no enabled local sources selected")
    reports = []
    for source in selected:
        db = home / f"{source.account}.db"
        now = as_of(None)
        report = {"id": source.id, "account": source.account, "last_attempt_at": now}
        conn: sqlite3.Connection | None = None
        attempted_counts: dict[str, int] | None = None

        def completed(result: ImportResult) -> None:
            nonlocal attempted_counts
            attempted_counts = _counts(result)
            if result.n_error or result.n_refused:
                raise IncompleteSource("source contains refused or conflicting records")
            last = conn.execute(
                "SELECT source_manifest_sha256 FROM import_runs WHERE account_id = "
                "(SELECT account_id FROM accounts WHERE alias = ?) ORDER BY run_id DESC LIMIT 1",
                (source.account,),
            ).fetchone()
            finished = as_of(None)
            document = {"schema_version": "mailcrawl.refresh.v1", "status": "success",
                        "last_import_at": finished, "kind": source.kind, "source_spec_sha256": source_digest(source),
                        "index_manifest_sha256": last[0] if last else None, "counts": _counts(result)}
            _receipt(conn, source.account, "local-export:" + source.id, document, finished)
            _receipt(conn, source.account, "local-export-attempt:" + source.id,
                     {"status": "success", "last_attempt_at": now}, now)
            report["last_import_at"] = finished

        try:
            if not db.is_file():
                report.update(
                    outcome="failed",
                    error="not_initialized",
                    receipt_persisted=False,
                )
                reports.append(report)
                continue
            email = account_email(config, source.account)
            taxonomy = resolve_taxonomy(None, config)
            aliases = owner_addresses(config, source.account)
            label_map = takeout_label_map(config, source.account) if source.kind == "mbox" else None
            conn = connect(db)
            if expected_source is not None:
                latest = next(
                    (
                        item
                        for item in configured_sources(config)
                        if item.id == expected_source.id
                    ),
                    None,
                )
                if latest != expected_source:
                    raise UsageError("configured source changed before sync")
            common = dict(alias=source.account, taxonomy_path=taxonomy,
                          account_email=email, aliases=aliases, on_success=completed, limits=source.limits)
            if source.kind == "gog-export":
                import_gog_export(conn, export_dir=source.path, **common)
            else:
                import_mbox(conn, mbox_path=source.path, labels_path=None,
                            label_map=label_map, **common)
            report.update(_counts_from_report(attempted_counts))
            report.update(outcome="success", receipt_persisted=True)
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            rollback_ok = True
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    rollback_ok = False
            report.pop("last_import_at", None)
            report.update(outcome="failed", error="import_failed")
            if attempted_counts:
                report["attempted_counts"] = attempted_counts
            report["receipt_persisted"] = (
                _persist_failed_attempt(conn, db, source, now) if rollback_ok else False
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        reports.append(report)
    return {"app_id": "mailcrawl", "schema_version": "mailcrawl.refresh.v1",
            "ok": all(row["outcome"] == "success" for row in reports), "sources": reports}


def _counts_from_report(counts: dict[str, int] | None) -> dict[str, int]:
    if counts is None:
        raise RuntimeError("completed import did not report counts")
    return counts


def _persist_failed_attempt(
    conn: sqlite3.Connection | None,
    db: Path,
    source: SourceSpec,
    now: int,
) -> bool:
    owned = conn is None
    target = conn
    try:
        if target is None:
            if not db.is_file():
                return False
            target = sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=rw", uri=True)
        _receipt(
            target,
            source.account,
            "local-export-attempt:" + source.id,
            {"status": "failed", "last_attempt_at": now, "error": "import_failed"},
            now,
        )
        target.commit()
        return True
    except Exception:
        if target is not None:
            try:
                target.rollback()
            except Exception:
                pass
        return False
    finally:
        if target is not None and owned:
            try:
                target.close()
            except Exception:
                pass
