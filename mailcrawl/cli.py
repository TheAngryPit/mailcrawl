"""JSON CLI for init, import, search, rules, stats, dry-run, and backfill."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from mailcrawl.configfile import account_email, account_identity, write_account
from mailcrawl.contract import ExportRejected, ManifestMismatch
from mailcrawl.gognative import ExportInvalid
from mailcrawl.ingest import ImportResult, import_gog_export, import_mbox
from mailcrawl.paths import UsageError, canonical_alias, config_path, data_dir
from mailcrawl.report import dry_run, stats
from mailcrawl.rules import apply_rules, load_rules
from mailcrawl.search import search_messages
from mailcrawl.store import UnsupportedSchema, connect, init_account
from mailcrawl.taxonomy import (
    active_categories,
    backfill_categories,
    load_taxonomy,
    missing_labels,
    present_label_names,
    resolve_taxonomy,
)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else int(code)
    try:
        if args.command == "init":
            return _init(args)
        if args.command == "import":
            return _import(args)
        if args.command == "search":
            return _search(args)
        if args.command == "rules":
            return _rules(args)
        if args.command == "stats":
            return _stats(args)
        if args.command == "dry-run":
            return _dry_run(args)
        if args.command == "backfill":
            return _backfill(args)
    except ExportRejected:
        _emit(args, {"error": "unsupported_schema", "ok": False})
        return 3
    except ExportInvalid as exc:
        _emit(args, {"detail": str(exc), "error": exc.error, "ok": False})
        return exc.exit_code
    except UnsupportedSchema:
        _emit(args, {"error": "unsupported_schema", "ok": False})
        return 3
    except ManifestMismatch as exc:
        _emit(args, {"detail": str(exc), "error": "manifest_mismatch", "ok": False})
        return 1
    except ValueError as exc:
        _emit(args, {"detail": str(exc), "error": "usage", "ok": False})
        return 2
    except UsageError as exc:
        _emit(args, {"detail": str(exc), "error": "usage", "ok": False})
        return 2
    _emit(args, {"error": "usage", "ok": False})
    return 2


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true")
    common.add_argument("--home")
    common.add_argument("--config")
    parser = argparse.ArgumentParser(prog="mailcrawl", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--account", action="append", dest="account_list")
    init.add_argument("--accounts")
    init.add_argument("--email")

    imp = sub.add_parser("import")
    imp_sub = imp.add_subparsers(dest="import_command", required=True)
    gog = imp_sub.add_parser("gog-export")
    gog.add_argument("--dir", required=True)
    gog.add_argument("--taxonomy")
    gog.add_argument("--account", "--alias", dest="account", required=True)
    mbox = imp_sub.add_parser("mbox")
    mbox.add_argument("file")
    mbox.add_argument("--account", "--alias", dest="account", required=True)
    mbox.add_argument("--labels")
    mbox.add_argument("--taxonomy")

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--account", required=True)

    rules = sub.add_parser("rules")
    rules.add_argument("--account", required=True)
    rules.add_argument("--rules", required=True)
    rules.add_argument("--now", type=int)

    counts = sub.add_parser("stats")
    counts.add_argument("--account", required=True)
    counts.add_argument("--taxonomy")

    preview = sub.add_parser("dry-run")
    preview.add_argument("--account", required=True)
    preview.add_argument("--taxonomy")
    preview.add_argument("--rules")
    preview.add_argument("--now", type=int)
    preview.add_argument("--export-decisions")
    preview.add_argument("--sample", type=int)
    preview.add_argument("--seed", type=int)

    fill = sub.add_parser("backfill")
    fill.add_argument("--account", required=True)
    return parser


def _init(args: argparse.Namespace) -> int:
    names = _init_aliases(args)
    if not names:
        raise UsageError("init needs --account or --accounts")
    if not args.email:
        raise UsageError("init needs --email")
    canonical = []
    for name in names:
        alias = canonical_alias(name)
        if alias not in canonical:
            canonical.append(alias)
    if len(canonical) != 1:
        raise UsageError("init one account per --email")
    alias = canonical[0]
    dest = data_dir(args.home)
    path = init_account(dest, alias, args.email)
    write_account(config_path(args.config), alias, args.email.strip())
    _emit(
        args,
        {"account": alias, "database": str(path), "ok": True},
        f"initialized {alias} {path}",
    )
    return 0


def _import(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    cfg = config_path(args.config)
    email = account_email(cfg, alias)
    catalogue = resolve_taxonomy(args.taxonomy, cfg)
    if catalogue is not None:
        load_taxonomy(catalogue)
    db_path = data_dir(args.home) / f"{alias}.db"
    conn = connect(db_path)
    try:
        result: ImportResult
        if args.import_command == "gog-export":
            result = import_gog_export(
                conn,
                alias=alias,
                export_dir=Path(args.dir),
                taxonomy_path=catalogue,
                account_email=email,
            )
        elif args.import_command == "mbox":
            result = import_mbox(
                conn,
                alias=alias,
                mbox_path=Path(args.file),
                labels_path=Path(args.labels) if args.labels else None,
                taxonomy_path=catalogue,
                account_email=email,
            )
        else:
            raise UsageError("unknown import command")
        absent = _taxonomy_warnings(conn, alias, catalogue)
    finally:
        conn.close()
    _emit(
        args,
        {
            "account": alias,
            "database": str(db_path),
            "missing_labels": absent,
            "n_dup": result.n_dup,
            "n_error": result.n_error,
            "n_new": result.n_new,
            "n_reconciled": result.n_reconciled,
            "n_refused": result.n_refused,
            "n_seen": result.n_seen,
            "ok": True,
            "taxonomy_warnings": len(absent),
        },
    )
    return 0


def _search(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    db_path = data_dir(args.home) / f"{alias}.db"
    if not db_path.is_file():
        raise UsageError("account is not initialized")
    conn = connect(db_path)
    try:
        hits = search_messages(conn, args.query)
    finally:
        conn.close()
    _emit(args, {"hits": hits, "ok": True, "query": args.query})
    return 0


def _rules(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    db_path = data_dir(args.home) / f"{alias}.db"
    if not db_path.is_file():
        raise UsageError("account is not initialized")
    config = load_rules(Path(args.rules))
    now = int(time.time()) if args.now is None else args.now
    owners, domains, security_senders, test_recipients = _identity(args)
    conn = connect(db_path)
    try:
        report = apply_rules(
            conn,
            now=now,
            config=config,
            owners=owners,
            domains=domains,
            security_senders=security_senders,
            test_recipients=test_recipients,
        )
    finally:
        conn.close()
    _emit(
        args,
        {
            "by_rule": report.by_rule,
            "by_state": report.by_state,
            "covered": report.covered,
            "ok": True,
            "rules_version": config.rules_version,
            "threads": report.threads,
        },
    )
    return 0


def _stats(args: argparse.Namespace) -> int:
    owners, domains, security_senders, test_recipients = _identity(args)

    def report(
        conn: sqlite3.Connection,
        categories: tuple[str, ...],
        names: dict[str, tuple[str, ...]] | None,
    ) -> dict[str, object]:
        return stats(
            conn,
            categories=categories,
            label_keys=names,
            owners=owners,
            domains=domains,
            security_senders=security_senders,
            test_recipients=test_recipients,
        )

    _emit(args, {**_counted(args, report), "ok": True})
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    now = int(time.time()) if args.now is None else args.now
    config = None if not args.rules else load_rules(Path(args.rules))
    export_path, sample, seed = _export_args(args)
    owners, domains, security_senders, test_recipients = _identity(args)

    def report(
        conn: sqlite3.Connection,
        categories: tuple[str, ...],
        names: dict[str, tuple[str, ...]] | None,
    ) -> dict[str, object]:
        return dry_run(
            conn,
            categories=categories,
            now=now,
            config=config,
            label_keys=names,
            export_path=export_path,
            sample=sample,
            seed=seed,
            owners=owners,
            domains=domains,
            security_senders=security_senders,
            test_recipients=test_recipients,
        )

    _emit(args, {**_counted(args, report), "ok": True})
    return 0


def _identity(
    args: argparse.Namespace,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    return account_identity(config_path(args.config), canonical_alias(args.account))


def _export_args(args: argparse.Namespace) -> tuple[Path | None, int | None, int]:
    raw = args.export_decisions
    sample = args.sample
    seed = args.seed
    if sample is not None and raw is None:
        raise UsageError("sample needs --export-decisions")
    if seed is not None and sample is None:
        raise UsageError("seed needs --sample")
    if sample is not None and sample < 1:
        raise UsageError("sample needs a positive count")
    if raw is None:
        return None, None, 0
    dest = Path(raw)
    if dest.exists() and dest.is_dir():
        raise UsageError("export path is a directory")
    if not dest.parent.is_dir():
        raise UsageError("export directory is missing")
    return dest, sample, 0 if seed is None else seed


def _backfill(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    conn = _account_db(args)
    try:
        updated = backfill_categories(conn)
    finally:
        conn.close()
    _emit(args, {"account": alias, "ok": True, "updated": updated})
    return 0


def _counted(args: argparse.Namespace, report) -> dict[str, object]:
    alias = canonical_alias(args.account)
    catalogue = resolve_taxonomy(args.taxonomy, config_path(args.config))
    loaded = None if catalogue is None else load_taxonomy(catalogue)
    conn = _account_db(args)
    try:
        names = None if loaded is None else loaded.names_for(alias)
        counted = report(conn, active_categories(loaded, alias), names)
        absent = [] if loaded is None else missing_labels(loaded, alias, present_label_names(conn))
    finally:
        conn.close()
    counted["missing_labels"] = absent
    counted["taxonomy_warnings"] = len(absent)
    return counted


def _taxonomy_warnings(conn, alias: str, catalogue: Path | None) -> list[str]:
    if catalogue is None:
        return []
    return missing_labels(load_taxonomy(catalogue), alias, present_label_names(conn))


def _account_db(args: argparse.Namespace) -> sqlite3.Connection:
    alias = canonical_alias(args.account)
    db_path = data_dir(args.home) / f"{alias}.db"
    if not db_path.is_file():
        raise UsageError("account is not initialized")
    return connect(db_path)


def _init_aliases(args: argparse.Namespace) -> list[str]:
    found: list[str] = []
    if args.accounts:
        found.extend(part.strip() for part in args.accounts.split(",") if part.strip())
    if args.account_list:
        found.extend(args.account_list)
    return found


def _emit(args: argparse.Namespace, doc: dict[str, object], text: str | None = None) -> None:
    if args.json:
        json.dump(doc, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return
    if text:
        print(text)
        return
    error = doc.get("error")
    if isinstance(error, str):
        print(error)
        return
    print("ok")
