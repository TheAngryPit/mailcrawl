"""JSON CLI for init, import, search, rules, stats, and dry-run."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from mailcrawl.configfile import account_email, taxonomy_path, write_account
from mailcrawl.contract import ExportRejected, ManifestMismatch
from mailcrawl.gognative import ExportInvalid
from mailcrawl.ingest import import_gog_export
from mailcrawl.paths import UsageError, canonical_alias, config_path, data_dir
from mailcrawl.report import dry_run, stats
from mailcrawl.rules import apply_rules, load_rules
from mailcrawl.search import search_messages
from mailcrawl.store import UnsupportedSchema, connect, init_account


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
    gog.add_argument("--account", required=True)

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--account", required=True)

    rules = sub.add_parser("rules")
    rules.add_argument("--account", required=True)
    rules.add_argument("--rules", required=True)
    rules.add_argument("--now", type=int)

    counts = sub.add_parser("stats")
    counts.add_argument("--account", required=True)

    preview = sub.add_parser("dry-run")
    preview.add_argument("--account", required=True)
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
    if args.import_command != "gog-export":
        raise UsageError("unknown import command")
    alias = canonical_alias(args.account)
    cfg = config_path(args.config)
    email = account_email(cfg, alias)
    catalogue = Path(args.taxonomy) if args.taxonomy else taxonomy_path(cfg)
    db_path = data_dir(args.home) / f"{alias}.db"
    conn = connect(db_path)
    try:
        result = import_gog_export(
            conn,
            alias=alias,
            export_dir=Path(args.dir),
            taxonomy_path=catalogue,
            account_email=email,
        )
    finally:
        conn.close()
    _emit(
        args,
        {
            "account": alias,
            "database": str(db_path),
            "n_dup": result.n_dup,
            "n_error": result.n_error,
            "n_new": result.n_new,
            "n_seen": result.n_seen,
            "ok": True,
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
    conn = connect(db_path)
    try:
        report = apply_rules(conn, now=now, config=config)
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
    conn = _account_db(args)
    try:
        counted = stats(conn)
    finally:
        conn.close()
    _emit(args, {**counted, "ok": True})
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    conn = _account_db(args)
    try:
        counted = dry_run(conn)
    finally:
        conn.close()
    _emit(args, {**counted, "ok": True})
    return 0


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
