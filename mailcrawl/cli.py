"""JSON CLI for init, import, search, rules, stats, dry-run, from-me, backfill, embed, knn, jev, bench, review, and decide."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

from mailcrawl.advise import StubAdvisor
from mailcrawl.bench import run_bench
from mailcrawl.clock import as_of
from mailcrawl.configfile import (
    account_email,
    account_identity,
    configured_as_of,
    embed_limits,
    embedder_name,
    allow_override,
    inference_config,
    llm_review,
    owner_addresses,
    inference_device,
    local_llm,
    rules_path,
    takeout_label_map,
    time_policy,
    write_account,
)
from mailcrawl.contract import ExportRejected, ManifestMismatch
from mailcrawl.gognative import ExportInvalid
from mailcrawl.ingest import ImportResult, import_gog_export, import_mbox
from mailcrawl.owner import recompute_from_me
from mailcrawl.schema import USER_VERSION
from mailcrawl.inference import enabled_points
from mailcrawl.jev import jev_decide, open_client, score_grey_band
from mailcrawl.paths import UsageError, canonical_alias, config_path, data_dir
from mailcrawl.reevaluate import TIME_DETAIL, reevaluate_time
from mailcrawl.report import dry_run, stats
from mailcrawl.review import (
    apply_agent_decision,
    apply_jsonl,
    decision_document,
    open_readonly,
    parse_agent_decision,
    parse_scope,
    review_queue,
    review_run,
    undo_decisions,
    undo_document,
)
from mailcrawl.rules import apply_rules, load_rules
from mailcrawl.search import search_messages
from mailcrawl.store import UnsupportedSchema, connect, init_account
from mailcrawl.takeout_repair import takeout_status
from mailcrawl.vectors import embed_threads, knn_account, knn_decide, open_embedder
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
        if args.command == "takeout":
            return _takeout(args)
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
        if args.command == "embed":
            return _embed(args)
        if args.command == "knn":
            return _knn(args)
        if args.command == "jev":
            return _jev(args)
        if args.command == "bench":
            return _bench(args)
        if args.command == "review":
            return _review(args)
        if args.command == "decide":
            return _decide(args)
        if args.command == "from-me":
            return _from_me(args)
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
    common.add_argument("--home", "--db", dest="home")
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

    takeout = sub.add_parser("takeout")
    takeout_sub = takeout.add_subparsers(dest="takeout_command", required=True)
    status = takeout_sub.add_parser("status")
    status.add_argument("--account", required=True)

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--account", required=True)
    search.add_argument("--sender")
    search.add_argument("--label")
    search.add_argument("--mime")
    search.add_argument("--image-only", action="store_true")
    search.add_argument("--since", type=int)
    search.add_argument("--until", type=int)
    search.add_argument("--semantic", action="store_true")
    search.add_argument("--vec-weight", type=float, default=1.0)
    search.add_argument("--keyword-weight", type=float, default=1.0)
    search.add_argument("--k", type=int, default=10)
    search.add_argument("--embedder")
    search.add_argument("--jev-client", default="fake")

    rules = sub.add_parser("rules")
    rules.add_argument("--account", required=True)
    rules.add_argument("--rules")
    rules.add_argument("--as-of", "--now", dest="now", type=int)
    rules.add_argument("--reevaluate", action="store_true")
    rules.add_argument("--apply", action="store_true")
    rules.add_argument("--csv")

    counts = sub.add_parser("stats")
    counts.add_argument("--account", required=True)
    counts.add_argument("--taxonomy")

    mine = sub.add_parser("from-me")
    mine.add_argument("--account", required=True)
    mine.add_argument("--apply", action="store_true")

    preview = sub.add_parser("dry-run")
    preview.add_argument("--account", required=True)
    preview.add_argument("--taxonomy")
    preview.add_argument("--rules")
    preview.add_argument("--as-of", "--now", dest="now", type=int)
    preview.add_argument("--export-decisions")
    preview.add_argument("--sample", type=int)
    preview.add_argument("--seed", type=int)
    preview.add_argument("--advise", action="store_true")

    fill = sub.add_parser("backfill")
    fill.add_argument("--account", required=True)

    embed = sub.add_parser("embed")
    embed.add_argument("--account", required=True)
    embed.add_argument("--embedder")

    knn = sub.add_parser("knn")
    knn.add_argument("--account", required=True)
    knn.add_argument("--thread")
    knn.add_argument("--embedder")
    knn.add_argument("--as-of", "--now", dest="now", type=int)

    jev = sub.add_parser("jev")
    jev.add_argument("--account", required=True)
    jev.add_argument("--thread")
    jev.add_argument("--client", default="fake")
    jev.add_argument("--as-of", "--now", dest="now", type=int)

    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    queue = review_sub.add_parser("queue")
    queue.add_argument("--account", required=True)
    queue.add_argument("--as-of", "--now", dest="now", type=int)
    _scope_args(queue)
    run = review_sub.add_parser("run")
    run.add_argument("--account", required=True)
    run.add_argument("--override", action="store_true")
    run.add_argument("--as-of", "--now", dest="now", type=int)
    _scope_args(run)
    undo = review_sub.add_parser("undo")
    undo.add_argument("--account", required=True)
    undo.add_argument("--thread")
    undo.add_argument("--source-detail")
    undo.add_argument("--as-of", "--now", dest="now", type=int)
    decide = sub.add_parser("decide")
    decide.add_argument("--account", required=True)
    decide.add_argument("--thread")
    decide.add_argument("--state")
    decide.add_argument("--lane")
    decide.add_argument("--source-detail")
    decide.add_argument("--reason")
    decide.add_argument("--file")
    decide.add_argument("--override", action="store_true")
    decide.add_argument("--as-of", "--now", dest="now", type=int)
    bench = sub.add_parser("bench")
    bench.add_argument("--account", required=True)
    bench.add_argument("--embedders", required=True)
    bench.add_argument("--sample", type=int, required=True)
    bench.add_argument("--truth", choices=("decisions", "labels"), default="decisions")
    bench.add_argument("--taxonomy")
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
            aliases = owner_addresses(cfg, alias)
            result = import_gog_export(
                conn,
                alias=alias,
                export_dir=Path(args.dir),
                taxonomy_path=catalogue,
                account_email=email,
                aliases=aliases,
            )
        elif args.import_command == "mbox":
            aliases = owner_addresses(cfg, alias)
            result = import_mbox(
                conn,
                alias=alias,
                mbox_path=Path(args.file),
                label_map=takeout_label_map(cfg, alias),
                labels_path=Path(args.labels) if args.labels else None,
                taxonomy_path=catalogue,
                account_email=email,
                aliases=aliases,
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
            "takeout_unmapped": list(result.takeout_unmapped),
            "taxonomy_warnings": len(absent),
        },
    )
    return 0


def _takeout(args: argparse.Namespace) -> int:
    if args.takeout_command != "status":
        raise UsageError("unknown takeout command")
    alias = canonical_alias(args.account)
    cfg = config_path(args.config)
    conn = _account_db(args)
    try:
        report = takeout_status(conn, takeout_label_map(cfg, alias))
    finally:
        conn.close()
    _emit(
        args,
        {
            "ok": True,
            "takeout_unmapped": list(report.unmapped),
        },
        text=", ".join(report.unmapped) or "none",
    )
    return 0


def _search(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    db_path = data_dir(args.home) / f"{alias}.db"
    if not db_path.is_file():
        raise UsageError("account is not initialized")
    if args.k < 1:
        raise UsageError("k needs a positive count")
    flags = inference_config(config_path(args.config))
    embedder = None
    if "search-embedding" in enabled_points(flags):
        embedder = _open_embedder(args)
    jev = None
    if "search-reranker" in enabled_points(flags):
        jev = open_client(args.jev_client)
    conn = connect(db_path)
    try:
        hits = search_messages(
            conn,
            args.query,
            sender=args.sender,
            label=args.label,
            mime=args.mime,
            image_only=1 if args.image_only else None,
            since=args.since,
            until=args.until,
            embedder=embedder,
            jev=jev,
            config=flags,
            fusion="rrf" if args.semantic else "keyword",
            keyword_weight=args.keyword_weight,
            vec_weight=args.vec_weight,
            k=args.k,
        )
    finally:
        conn.close()
    _emit(args, {"hits": hits, "ok": True, "query": args.query})
    return 0


def _rules(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    db_path = data_dir(args.home) / f"{alias}.db"
    if not db_path.is_file():
        raise UsageError("account is not initialized")
    if args.apply and not args.reevaluate:
        raise UsageError("apply needs reevaluate")
    rules_file = _rules_file(args)
    config = load_rules(rules_file)
    now = _as_of(args)
    owners, domains, security_senders, test_recipients = _identity(args)
    conn = connect(db_path)
    try:
        if args.reevaluate:
            if not args.csv:
                raise UsageError("reevaluate needs a csv path")
            rows = reevaluate_time(
                conn,
                now=now,
                policy=time_policy(config_path(args.config)),
                apply=bool(args.apply),
                review=llm_review(config_path(args.config)),
            )
            _write_reevaluate(Path(args.csv), rows)
            changed = sum(1 for row in rows if row["changed"] == "1")
            _emit(
                args,
                {
                    "applied": bool(args.apply),
                    "changed": changed,
                    "csv": str(args.csv),
                    "ok": True,
                    "skipped_human": sum(1 for row in rows if row["skipped"] == "human"),
                    "skipped_review": sum(1 for row in rows if row["skipped"] == "needs_review"),
                    "source_detail": TIME_DETAIL,
                    "unchanged": sum(1 for row in rows if row["changed"] == "0" and not row["skipped"]),
                },
            )
            return 0
        report = apply_rules(
            conn,
            now=now,
            config=config,
            owners=owners,
            domains=domains,
            security_senders=security_senders,
            test_recipients=test_recipients,
            policy=time_policy(config_path(args.config)),
            review=llm_review(config_path(args.config)),
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


def _from_me(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    cfg = config_path(args.config)
    aliases = owner_addresses(cfg, alias)
    if not args.apply:
        conn = _open_dry(data_dir(args.home) / f"{alias}.db")
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < USER_VERSION:
                _emit(args, {"account": alias, "error": "needs migration", "ok": False})
                return 2
            if version > USER_VERSION:
                _emit(args, {"account": alias, "error": "unsupported_schema", "ok": False})
                return 3
            report = recompute_from_me(conn, aliases, apply=False)
        finally:
            conn.close()
        _emit(args, {"account": alias, **report.public()})
        return 0
    conn = _account_db(args)
    try:
        report = recompute_from_me(conn, aliases, apply=True)
    finally:
        conn.close()
    _emit(args, {"account": alias, **report.public()})
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    now = _as_of(args)
    rules_file = _rules_file(args)
    config = load_rules(rules_file)
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
            advisor=StubAdvisor() if args.advise else None,
            policy=time_policy(config_path(args.config)),
            review=llm_review(config_path(args.config)),
        )

    _emit(args, {**_counted(args, report), "ok": True})
    return 0


def _as_of(args: argparse.Namespace) -> int:
    return as_of(args.now, configured_as_of(config_path(args.config)))


def _time_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "policy": time_policy(config_path(args.config)),
        "review": llm_review(config_path(args.config)),
    }


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


def _bench(args: argparse.Namespace) -> int:
    home = data_dir(args.home)
    alias = canonical_alias(args.account)
    names = [part.strip() for part in str(args.embedders).split(",") if part.strip()]
    if args.sample is None or args.sample < 1:
        raise UsageError("sample needs a positive count")
    batch_size, max_tokens = embed_limits(config_path(args.config))
    label_categories = None
    if args.truth == "labels":
        catalogue = resolve_taxonomy(args.taxonomy, config_path(args.config))
        if catalogue is None:
            raise UsageError("labels truth needs a taxonomy")
        loaded = load_taxonomy(catalogue)
        label_categories = {
            name: frozenset(categories)
            for name, categories in loaded.names_for(alias).items()
        }
    report = run_bench(
        home / f"{alias}.db",
        names,
        args.sample,
        batch_size=batch_size,
        max_tokens=max_tokens,
        truth=args.truth,
        label_categories=label_categories,
    )
    report["account"] = alias
    _emit(args, report, text=str(report["markdown"]))
    return 0


def _embedder(args: argparse.Namespace) -> str:
    if args.embedder:
        return args.embedder
    return embedder_name(config_path(args.config))


def _rules_file(args: argparse.Namespace) -> Path:
    if args.rules:
        chosen = Path(args.rules).expanduser()
        if not chosen.is_file():
            raise UsageError("rules file from --rules is missing")
        return chosen
    configured = rules_path(config_path(args.config))
    if configured is None:
        raise UsageError("rules needs --rules or a rules path in config")
    if not configured.is_file():
        raise UsageError("rules file from config is missing")
    return configured


def _open_embedder(args: argparse.Namespace):
    embedder = open_embedder(_embedder(args))
    batch_size, max_tokens = embed_limits(config_path(args.config))
    embedder.batch_size = batch_size
    embedder.max_tokens = max_tokens
    embedder.device = inference_device(config_path(args.config))
    return embedder


def _embed(args: argparse.Namespace) -> int:
    data_dir(args.home)
    embedder = _open_embedder(args)
    flags = inference_config(config_path(args.config))
    alias = canonical_alias(args.account)
    conn = _account_db(args)
    try:
        written = embed_threads(conn, embedder, flags)
    finally:
        conn.close()
    _emit(args, {"account": alias, "embedded": written, "ok": True})
    return 0


def _knn(args: argparse.Namespace) -> int:
    data_dir(args.home)
    flags = inference_config(config_path(args.config))
    embedder = None
    if "embeddings" in enabled_points(flags):
        embedder = _open_embedder(args)
    now = _as_of(args)
    conn = _account_db(args)
    decision = None
    counted = None
    try:
        if args.thread:
            row = conn.execute(
                "SELECT thread_pk FROM threads WHERE gmail_thread_id = ?",
                (args.thread,),
            ).fetchone()
            if row is None:
                raise UsageError("unknown thread")
            decision = knn_decide(conn, int(row["thread_pk"]), embedder, flags, now, **_time_args(args))
        else:
            counted = knn_account(conn, embedder, flags, now, **_time_args(args))
    finally:
        conn.close()
    if args.thread:
        _emit(
            args,
            {
                "next_state": None if decision is None else decision["next_state"],
                "ok": True,
                "origin": None if decision is None else decision["origin"],
                "source_detail": None if decision is None else decision["source_detail"],
                "thread": args.thread,
            },
        )
        return 0
    eligible, decided, review = counted
    _emit(
        args,
        {
            "account": canonical_alias(args.account),
            "decided": decided,
            "eligible": eligible,
            "ok": True,
            "review": review,
        },
    )
    return 0


def _jev(args: argparse.Namespace) -> int:
    data_dir(args.home)
    flags = inference_config(config_path(args.config))
    now = _as_of(args)
    conn = _account_db(args)
    decision = None
    passed = None
    try:
        client = open_client(args.client)
        if args.thread:
            row = conn.execute(
                "SELECT thread_pk FROM threads WHERE gmail_thread_id = ?",
                (args.thread,),
            ).fetchone()
            if row is None:
                raise UsageError("unknown thread")
            decision = jev_decide(
                conn, int(row["thread_pk"]), client, flags, now, **_time_args(args)
            )
        else:
            passed = score_grey_band(conn, client, flags, now, **_time_args(args))
    finally:
        conn.close()
    if args.thread:
        _emit(
            args,
            {
                "decision_id": None if decision is None else decision["decision_id"],
                "next_state": "needs_review" if decision is None else decision["next_state"],
                "ok": True,
                "origin": None if decision is None else decision["origin"],
                "source_detail": None if decision is None else decision["source_detail"],
                "thread": args.thread,
            },
        )
        return 0
    _emit(
        args,
        {
            "account": canonical_alias(args.account),
            "decided": passed.decided,
            "eligible": passed.eligible,
            "ok": True,
            "review": passed.review,
        },
    )
    return 0


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


def _open_dry(path: Path) -> sqlite3.Connection:
    # A read-only WAL connection still creates -wal and -shm. immutable does not.
    if not path.is_file():
        raise UsageError("account is not initialized")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _account_db(args: argparse.Namespace) -> sqlite3.Connection:
    alias = canonical_alias(args.account)
    db_path = data_dir(args.home) / f"{alias}.db"
    if not db_path.is_file():
        raise UsageError("account is not initialized")
    return connect(db_path)


def _write_reevaluate(path: Path, rows: list[dict[str, str]]) -> None:
    fields = (
        "gmail_thread_id",
        "origin",
        "state",
        "lane",
        "next_state",
        "next_lane",
        "changed",
        "skipped",
        "reason",
        "kind",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _init_aliases(args: argparse.Namespace) -> list[str]:
    found: list[str] = []
    if args.accounts:
        found.extend(part.strip() for part in args.accounts.split(",") if part.strip())
    if args.account_list:
        found.extend(args.account_list)
    return found


def _scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--origin")
    parser.add_argument("--rule")
    parser.add_argument("--label")
    parser.add_argument("--state")
    parser.add_argument("--since", type=int)
    parser.add_argument("--limit", type=int)


def _review_scope(args: argparse.Namespace):
    return parse_scope(
        everything=bool(args.all),
        origin=args.origin,
        rule=args.rule,
        label=args.label,
        state=args.state,
        since=args.since,
        limit=args.limit,
        time_closures=llm_review(config_path(args.config)),
    )


def _review(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    config = config_path(args.config)
    _max = embed_limits(config)[1]
    if args.review_command == "queue":
        conn = open_readonly(data_dir(args.home) / f"{alias}.db")
        try:
            items = review_queue(
                conn,
                alias,
                _review_scope(args),
                max_tokens=_max,
                as_of=_as_of(args),
            )
        finally:
            conn.close()
        documents = [item.document() for item in items]
        _emit(
            args,
            {"account": alias, "items": documents, "ok": True},
            text=f"queue {len(documents)}",
        )
        return 0
    conn = _account_db(args)
    try:
        now = _as_of(args)
        if args.review_command == "undo":
            restored = undo_decisions(
                conn,
                thread_id=args.thread,
                source_detail=args.source_detail,
                now=now,
            )
            documents = [
                undo_document(thread_id, row, changed) for thread_id, row, changed in restored
            ]
        else:
            override = bool(args.override) or allow_override(config)
            rows = review_run(
                conn,
                alias,
                local_llm(config),
                _review_scope(args),
                inference_config(config),
                max_tokens=_max,
                now=now,
                override=override,
            )
            documents = [
                decision_document(row, _thread_id(conn, int(row["thread_pk"])), changed)
                for row, changed in rows
            ]
    finally:
        conn.close()
    text = f"undone {len(documents)}" if args.review_command == "undo" else f"decided {len(documents)}"
    _emit(args, {"decisions": documents, "ok": True}, text=text)
    return 0


def _decide(args: argparse.Namespace) -> int:
    alias = canonical_alias(args.account)
    now = _as_of(args)
    override = bool(args.override) or allow_override(config_path(args.config))
    conn = _account_db(args)
    try:
        if args.file is not None:
            if args.thread or args.state or args.lane or args.source_detail or args.reason:
                raise UsageError("decide takes flags or a file")
            lines = _decision_lines(args.file)
            rows = apply_jsonl(conn, lines, now, override=override)
        else:
            decision = parse_agent_decision(
                {
                    "lane": args.lane,
                    "reason": args.reason,
                    "source_detail": args.source_detail,
                    "state": args.state,
                    "thread_id": args.thread,
                }
            )
            rows = [apply_agent_decision(conn, decision, now, override=override)]
        ids = {int(row["thread_pk"]): _thread_id(conn, int(row["thread_pk"])) for row, _changed in rows}
    finally:
        conn.close()
    documents = [
        decision_document(row, ids[int(row["thread_pk"])], changed) for row, changed in rows
    ]
    _emit(args, {"decisions": documents, "ok": True}, text=f"decided {len(documents)}")
    return 0


def _decision_lines(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    file_path = Path(path)
    if not file_path.is_file():
        raise UsageError("decision file is missing")
    return file_path.read_text(encoding="utf-8").splitlines()


def _thread_id(conn: sqlite3.Connection, thread_pk: int) -> str:
    row = conn.execute(
        "SELECT gmail_thread_id FROM threads WHERE thread_pk = ?",
        (thread_pk,),
    ).fetchone()
    if row is None:
        raise UsageError("thread is not in the archive")
    return str(row["gmail_thread_id"])


def _emit(args: argparse.Namespace, doc: dict[str, object], text: str | None = None) -> None:
    if args.json:
        json.dump(doc, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return
    if text:
        print(text)
        return
    detail = doc.get("detail")
    if isinstance(detail, str) and detail:
        print(detail, file=sys.stderr)
    error = doc.get("error")
    if isinstance(error, str):
        print(error)
        return
    print("ok")
