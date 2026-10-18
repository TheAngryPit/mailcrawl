#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Headless checks for the wheel install. Cwd stays empty. No network."""

import calendar
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import mailcrawl
import mailcrawl.jev as jev
from mailcrawl.cli import main as mailcrawl_main
from mailcrawl.inference import invoke
from mailcrawl.jev import BATTERY, HttpJev
from mailcrawl.paths import UsageError
from mailcrawl.rules import bundled_rules_path
from mailcrawl.store import connect

AS_OF = calendar.timegm((2026, 10, 17, 12, 0, 0))
DAY = 86400


def main() -> None:
    name = sys.argv[1]
    location = mailcrawl.__file__
    if "site-packages" not in location.replace("\\", "/"):
        raise SystemExit(f"not a wheel install: {location}")
    if list(Path.cwd().iterdir()):
        raise SystemExit("cwd is not empty")
    scenarios[name]()
    if list(Path.cwd().iterdir()):
        raise SystemExit("scenario wrote into the empty cwd")
    print(f"proof-type command-transcript feature={name}")
    print(f"wheel {location}")


def cli(home: Path, config: Path, *args: str) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mailcrawl_main(["--json", "--home", str(home), "--config", str(config), *args])
    text = buf.getvalue()
    doc = json.loads(text) if text else {}
    if code != 0 or doc.get("ok") is False:
        raise SystemExit(f"{args} failed {code} {doc}")
    return doc


def home_for(account: str) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="mailcrawl-wheel-"))
    config = root / "config.toml"
    config.write_text(
        f'[accounts.{account}]\nemail = "ada@example.com"\n',
        encoding="utf-8",
    )
    cli(root, config, "init", "--account", account, "--email", "ada@example.com")
    return root, config


def db_path(home: Path, account: str) -> Path:
    return home / f"{account}.db"


def seed_old_owner_thread(home: Path, account: str, thread_id: str) -> None:
    conn = connect(db_path(home, account))
    try:
        account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        when = AS_OF - 100 * DAY
        conn.execute(
            "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (?, ?, 2, 0)",
            (account_id, thread_id),
        )
        thread_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        body = " ".join(["word"] * 40)
        people = (
            (0, "Bao Fixture <bao@example.com>"),
            (1, "Ada Fixture <ada@example.com>"),
        )
        for index, (mine, header) in enumerate(people):
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, from_hdr,
                  subject, body_text, is_from_me, date_trusted, raw_sha256, provenance_hash, imported_at
                ) VALUES (?, ?, ?, ?, ?, 'synthetic subject', ?, ?, 1, ?, 'prov', 1)
                """,
                (
                    account_id,
                    f"m-{thread_id}-{index}",
                    thread_pk,
                    when + index,
                    header,
                    body,
                    mine,
                    f"sha-{thread_id}-{index}",
                ),
            )
        conn.execute(
            """
            INSERT INTO decisions(
              thread_pk, next_state, lane, source, origin, source_detail,
              rule_id, rules_version, evidence, created_at
            ) VALUES (?, 'reply', 'imbox', 'rule', 'rule', 'rule-5', '5', 'rules-v4', '[]', 1)
            """,
            (thread_pk,),
        )
        conn.commit()
    finally:
        conn.close()


def bundled_rules() -> str:
    path = bundled_rules_path()
    if "site-packages" not in str(path):
        raise SystemExit(f"bundled rules are not in the wheel: {path}")
    return str(path)


def reevaluate(home: Path, config: Path, account: str) -> dict:
    csv_path = home / "plan.csv"
    return cli(
        home,
        config,
        "rules",
        "--account",
        account,
        "--rules",
        bundled_rules(),
        "--now",
        str(AS_OF),
        "--reevaluate",
        "--apply",
        "--csv",
        str(csv_path),
    )


def time_closure_review() -> None:
    home, config = home_for("personal")
    config.write_text(
        config.read_text(encoding="utf-8") + "[review]\nllm_review = true\n",
        encoding="utf-8",
    )
    seed_old_owner_thread(home, "personal", "old-thread")
    reevaluate(home, config, "personal")
    queued = cli(home, config, "review", "queue", "--account", "personal", "--now", str(AS_OF))
    item = {row["thread_id"]: row for row in queued["items"]}["old-thread"]
    closure = item["time_closure"]
    if closure["state"] != "resolved" or closure["lane"] != "paper_trail":
        raise SystemExit(closure)
    kept = decide_keep(home, config, "old-thread")
    if kept["decisions"][0]["changed"] is not True:
        raise SystemExit(kept)
    print("fixture-id old-thread")
    print("test-name test_an_enabled_step_stores_the_closure_and_keeps_it_in_scope")
    print("time-closure resolved paper_trail keep-accepted")


def closure_stays_queued() -> None:
    home, config = home_for("personal")
    config.write_text(
        config.read_text(encoding="utf-8") + "[review]\nllm_review = true\n",
        encoding="utf-8",
    )
    seed_old_owner_thread(home, "personal", "old-thread")
    reevaluate(home, config, "personal")
    config.write_text(
        '[accounts.personal]\nemail = "ada@example.com"\n',
        encoding="utf-8",
    )
    queued = cli(home, config, "review", "queue", "--account", "personal", "--now", str(AS_OF))
    item = {row["thread_id"]: row for row in queued["items"]}["old-thread"]
    if item["state"] != "needs_review":
        raise SystemExit(item["state"])
    closure = item["time_closure"]
    if closure["state"] != "resolved" or closure["lane"] != "paper_trail":
        raise SystemExit(closure)
    kept = decide_keep(home, config, "old-thread")
    if kept["decisions"][0]["changed"] is not True:
        raise SystemExit(kept)
    print("fixture-id old-thread")
    print("test-name test_a_stored_closure_stays_in_the_default_queue_when_llm_review_is_off")
    print("stored-closure stays-queued needs_review")


def decide_keep(home: Path, config: Path, thread_id: str) -> dict:
    path = home / "keep.jsonl"
    path.write_text(
        json.dumps({"action": "keep", "source_detail": "fixture-model", "thread_id": thread_id}) + "\n",
        encoding="utf-8",
    )
    return cli(home, config, "decide", "--account", "personal", "--file", str(path), "--now", str(AS_OF))


def owner_alias_from_me() -> None:
    home, config = home_for("vcl")
    rules = bundled_rules()
    config.write_text(
        "\n".join(
            [
                "[accounts.vcl]",
                'email = "ada@example.com"',
                'owner_addresses = ["Other@Example.com"]',
                "",
                "[rules]",
                f'path = "{rules}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    mbox = home / "alias-pass.mbox"
    mbox.write_text(_alias_mbox(), encoding="utf-8")
    imported = cli(home, config, "import", "mbox", str(mbox), "--alias", "vcl")
    if imported.get("n_new") != 1:
        raise SystemExit(imported)
    report = cli(home, config, "from-me", "--account", "vcl")
    if report.get("alias") != 1 or report.get("applied") is not False:
        raise SystemExit(report)
    decided = cli(home, config, "rules", "--account", "vcl", "--now", "1700000000")
    if decided.get("ok") is not True:
        raise SystemExit(decided)
    print("fixture-id alias-pass.mbox")
    print("test-name test_import_trusts_gmail_pass_and_rules_see_the_alias")
    print("from-me alias=1 dry-run")


def wheel_rules_jev() -> None:
    rules = bundled_rules()
    home, config = home_for("personal")
    config.write_text(
        config.read_text(encoding="utf-8") + f'[rules]\npath = "{rules}"\n',
        encoding="utf-8",
    )
    seed_old_owner_thread(home, "personal", "young-rules")
    counted = cli(home, config, "rules", "--account", "personal", "--now", str(AS_OF))
    if counted.get("rules_version") != "rules-v4":
        raise SystemExit(counted)
    _refuse_jev_redirect()
    print("fixture-id bundled-rules-v0")
    print("test-name test_redirect_drops_the_key_before_the_second_host")
    print("bundled-rules rules-v4 jev-redirect-refused")


def revive_hold() -> None:
    home, config = home_for("personal")
    seed_old_owner_thread(home, "personal", "stale-thread")
    conn = connect(db_path(home, "personal"))
    try:
        conn.execute(
            "DELETE FROM messages WHERE gmail_message_id = 'm-stale-thread-1'"
        )
        conn.execute("UPDATE threads SET msg_count = 1 WHERE gmail_thread_id = 'stale-thread'")
        conn.commit()
    finally:
        conn.close()
    held = reevaluate(home, config, "personal")
    if held.get("changed") != 1:
        raise SystemExit(held)
    path = home / "revive.jsonl"
    path.write_text(
        json.dumps(
            {
                "action": "revive",
                "kind": "forgotten",
                "reason": "still owed",
                "source_detail": "fixture-model",
                "state": "reply",
                "thread_id": "stale-thread",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    revived = cli(home, config, "decide", "--account", "personal", "--file", str(path), "--now", str(AS_OF))
    if revived["decisions"][0]["changed"] is not True:
        raise SystemExit(revived)
    again = reevaluate(home, config, "personal")
    if again.get("changed") != 0:
        raise SystemExit(again)
    _untrusted_after_revive(home)
    opened = reevaluate(home, config, "personal")
    if opened.get("changed") != 1:
        raise SystemExit(opened)
    print("fixture-id stale-thread")
    print("test-name test_an_untrusted_date_imported_after_the_revive_is_queued")
    print("revive-hold then untrusted-import-opens")


def _untrusted_after_revive(home: Path) -> None:
    conn = connect(db_path(home, "personal"))
    try:
        account = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        thread_pk = int(
            conn.execute(
                "SELECT thread_pk FROM threads WHERE gmail_thread_id = 'stale-thread'"
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, from_hdr,
              subject, body_text, is_from_me, date_trusted, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, 'm-untrusted', ?, ?, 'Bao Fixture <bao@example.com>', 'synthetic subject', 'hello again', 0, 0, 'sha-untrusted', 'prov', ?)
            """,
            (account, thread_pk, calendar.timegm((1999, 1, 1, 0, 0, 0)), AS_OF + 60),
        )
        conn.execute(
            "INSERT INTO senders(account_id, address_norm, domain) VALUES (?, 'bao@example.com', 'example.com')",
            (account,),
        )
        sender_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "UPDATE messages SET sender_id = ? WHERE thread_pk = ?",
            (sender_id, thread_pk),
        )
        conn.commit()
    finally:
        conn.close()


def _refuse_jev_redirect() -> None:
    for code in (301, 302, 303, 307, 308):
        _refuse_redirect(code)
    print("redirects 301 302 303 307 308")


def _refuse_redirect(code: int) -> None:
    second_hits: list[str | None] = []

    class Second(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            second_hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, fmt: str, *args: object) -> None:
            return

    class First(BaseHTTPRequestHandler):
        location = ""
        status = 302

        def do_POST(self) -> None:
            self.send_response(First.status)
            self.send_header("Location", First.location)
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            return

    second = ThreadingHTTPServer(("127.0.0.1", 0), Second)
    first = ThreadingHTTPServer(("127.0.0.1", 0), First)
    First.status = code
    First.location = f"http://127.0.0.1:{second.server_address[1]}/v1/systemone"
    Thread(target=second.serve_forever, daemon=True).start()
    Thread(target=first.serve_forever, daemon=True).start()
    os.environ["TYPESAFE_API_KEY"] = "loopback-secret"
    jev._ENDPOINT = f"http://127.0.0.1:{first.server_address[1]}/v1/systemone"
    try:
        try:
            invoke("jev-battery", lambda: HttpJev().ask("synthetic", BATTERY[:1]), {"jev-battery": True})
        except UsageError as exc:
            if "redirect" not in str(exc):
                raise SystemExit(f"{code} {exc}") from exc
        else:
            raise SystemExit(f"jev followed redirect {code}")
        if second_hits:
            raise SystemExit(f"redirect {code} reached the second host")
    finally:
        first.shutdown()
        second.shutdown()
        first.server_close()
        second.server_close()


def _alias_mbox() -> str:
    auth = (
        "Authentication-Results: mx.google.com;\n"
        "       dkim=pass header.d=example.com;\n"
        "       dmarc=pass header.from=example.com"
    )
    lines = [
        "From 1700000000000000101@xxx Tue Nov 14 00:00:00 +0000 2023",
        "X-GM-THRID: 1700000000000000101",
        "X-GM-MSGID: 1700000000000000101",
        "X-Gmail-Labels: Inbox",
        auth,
        "From: Other <Other@Example.com>",
        "To: Ada <ada@example.com>",
        "Subject: note",
        "Date: Tue, 14 Nov 2023 22:13:20 +0000",
        "Message-ID: <owner-101@example.com>",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
        "",
        "Can you confirm?",
        "",
    ]
    return "\n".join(lines)


scenarios = {
    "time-closure-review": time_closure_review,
    "owner-alias-from-me": owner_alias_from_me,
    "wheel-rules-jev": wheel_rules_jev,
    "revive-hold": revive_hold,
    "closure-stays-queued": closure_stays_queued,
}


if __name__ == "__main__":
    main()
