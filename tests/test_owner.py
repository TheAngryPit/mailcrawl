"""Owner addresses count only with Gmail's own authentication result."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mailcrawl.cli import main
from mailcrawl.schema import USER_VERSION
from mailcrawl.store import connect, init_account

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "testdata" / "rules" / "rules-v0.json"
NOW = 1_700_000_000
PASS = (
    "Authentication-Results: mx.google.com;\n"
    "       dmarc=pass (p=NONE; sp=NONE) header.from=example.com"
)


class OwnerAliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.config = self.home / "config.toml"
        self.config.write_text(
            "\n".join(
                [
                    "[rules]",
                    f'path = "{RULES}"',
                    "",
                    "[accounts.vcl]",
                    'email = "ada@example.com"',
                    'owner_addresses = ["Other@Example.com", "joa\u0303o@example.com"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_import_trusts_gmail_pass_and_rules_see_the_alias(self) -> None:
        self._init()
        mbox = self.home / "takeout.mbox"
        mbox.write_text(_mbox(), encoding="utf-8")
        imported = self._json(
            ["import", "mbox", str(mbox), "--alias", "vcl"]
        )
        self.assertEqual(imported["ok"], True)
        stored = self._rows()
        self.assertEqual(stored["Can you confirm?"], (1, "alias"))
        self.assertEqual(stored["Can you confirm the spoof?"], (0, None))
        self.assertEqual(stored["Can you confirm the later pass?"], (0, None))
        self.assertEqual(stored["Can you confirm the dkim?"], (1, "alias"))
        self.assertEqual(stored["Can you confirm the later fail?"], (1, "alias"))
        self.assertEqual(stored["Thanks, all set."], (1, "sent"))
        self.assertEqual(stored["hello from a colleague"], (0, None))
        self.assertEqual(stored["Can you confirm the subdomain?"], (0, None))
        report = self._json(["from-me", "--account", "vcl"])
        self.assertEqual(
            report,
            {
                "account": "vcl",
                "alias": 3,
                "applied": False,
                "changed": 0,
                "ok": True,
                "sent": 1,
                "unchanged": 8,
                "untrusted": 3,
            },
        )
        decided = self._json(["rules", "--account", "vcl", "--now", str(NOW)])
        self.assertEqual(decided["ok"], True)
        self.assertEqual(self._decisions()["Can you confirm?"], ("6", "waiting"))
        self.assertEqual(self._decisions()["Can you confirm the spoof?"], ("5", "reply"))
        self.assertEqual(self._decisions()["Can you confirm the later pass?"], ("5", "reply"))
        self.assertEqual(self._decisions()["Can you confirm the dkim?"], ("6", "waiting"))
        self.assertEqual(self._decisions()["Thanks, all set."], ("8", "resolved"))
        self.assertEqual(self._decisions()["hello from a colleague"], ("5", "reply"))
        self.assertEqual(self._decisions()["Can you confirm the subdomain?"], ("5", "reply"))

    def test_from_me_is_dry_until_apply_and_a_repeat_changes_nothing(self) -> None:
        db = init_account(self.home, "vcl", "ada@example.com")
        conn = connect(db)
        try:
            account = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
            thread = conn.execute(
                """
                INSERT INTO threads(account_id, gmail_thread_id)
                VALUES (?, 'thread-alias')
                RETURNING thread_pk
                """,
                (account,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date,
                  from_hdr, auth_results, body_text, is_from_me,
                  raw_sha256, provenance_hash, imported_at
                ) VALUES (?, 'msg-alias', ?, ?, ?, ?, 'Can you confirm?', 0, 'raw', 'prov', ?)
                """,
                (
                    account,
                    thread,
                    NOW,
                    "João مرحبا <jo\u00e3o@example.com>",
                    "mx.google.com; dmarc=pass header.from=example.com",
                    NOW,
                ),
            )
            conn.execute(
                """
                INSERT INTO cursors(account_id, stream, value, updated_at)
                VALUES (?, 'takeout_label_map', 'already-applied', ?)
                """,
                (account, NOW),
            )
            conn.commit()
        finally:
            conn.close()
        preview = self._json(["from-me", "--account", "vcl"])
        self.assertEqual(preview["applied"], False)
        self.assertEqual(preview["changed"], 1)
        self.assertEqual(preview["alias"], 1)
        self.assertEqual(preview["untrusted"], 0)
        self.assertEqual(self._flag(db), (0, None))
        self.assertEqual(self._cursor(db), "already-applied")
        applied = self._json(["from-me", "--account", "vcl", "--apply"])
        self.assertEqual(applied["applied"], True)
        self.assertEqual(applied["changed"], 1)
        self.assertEqual(self._flag(db), (1, "alias"))
        self.assertEqual(self._cursor(db), "already-applied")
        again = self._json(["from-me", "--account", "vcl", "--apply"])
        self.assertEqual(again["changed"], 0)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(self._flag(db), (1, "alias"))
        waiting = self._json(["rules", "--account", "vcl", "--now", str(NOW)])
        self.assertEqual(waiting["by_rule"], {"6": 1})
        self.assertEqual(waiting["by_state"], {"waiting": 1})

    def test_user_version_10_gains_the_origin_column(self) -> None:
        db = init_account(self.home, "vcl", "ada@example.com")
        conn = sqlite3.connect(db)
        try:
            conn.execute("ALTER TABLE messages DROP COLUMN from_me_origin")
            conn.execute("PRAGMA user_version = 10")
            conn.commit()
        finally:
            conn.close()
        conn = connect(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, USER_VERSION)
        self.assertIn("from_me_origin", columns)
        self.assertEqual(USER_VERSION, 11)

    def _init(self) -> None:
        self._json(["init", "--account", "vcl", "--email", "ada@example.com"])

    def _json(self, args: list[str]) -> dict[str, object]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(
                ["--json", "--home", str(self.home), "--config", str(self.config), *args]
            )
        self.assertEqual(code, 0, buf.getvalue())
        return json.loads(buf.getvalue())

    def _db(self) -> Path:
        return self.home / "vcl.db"

    def _rows(self) -> dict[str, tuple[int, str | None]]:
        conn = connect(self._db())
        try:
            found = conn.execute(
                "SELECT body_text, is_from_me, from_me_origin FROM messages"
            ).fetchall()
        finally:
            conn.close()
        return {str(row[0]): (int(row[1]), row[2]) for row in found}

    def _decisions(self) -> dict[str, tuple[str, str]]:
        conn = connect(self._db())
        try:
            found = conn.execute(
                """
                SELECT messages.body_text, decisions.rule_id, decisions.next_state
                FROM messages
                JOIN decisions ON decisions.thread_pk = messages.thread_pk
                WHERE decisions.superseded_by IS NULL
                """
            ).fetchall()
        finally:
            conn.close()
        return {str(row[0]): (str(row[1]), str(row[2])) for row in found}

    def _flag(self, db: Path) -> tuple[int, str | None]:
        conn = connect(db)
        try:
            row = conn.execute("SELECT is_from_me, from_me_origin FROM messages").fetchone()
        finally:
            conn.close()
        return int(row[0]), row[1]

    def _cursor(self, db: Path) -> str:
        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT value FROM cursors WHERE stream = 'takeout_label_map'"
            ).fetchone()
        finally:
            conn.close()
        return str(row[0])


def _mbox() -> str:
    messages = [
        _message(
            "101",
            "Inbox",
            PASS,
            "João مرحبا <Other@Example.com>",
            "Can you confirm?",
        ),
        _message(
            "102",
            "Inbox",
            "Authentication-Results: evil.example.com; dmarc=pass header.from=example.com",
            "Other@Example.com",
            "Can you confirm the spoof?",
        ),
        _message(
            "103",
            "Inbox",
            "\n".join(
                [
                    "Authentication-Results: mx.google.com; dmarc=fail header.from=example.com",
                    "Authentication-Results: mx.google.com; dmarc=pass header.from=example.com",
                ]
            ),
            "Other@Example.com",
            "Can you confirm the later pass?",
        ),
        _message(
            "104",
            "Inbox",
            "Authentication-Results: mx.google.com; dmarc=fail header.from=example.com; dkim=pass header.d=example.com",
            "Other@Example.com",
            "Can you confirm the dkim?",
        ),
        _message(
            "105",
            "Inbox",
            "\n".join(
                [
                    "Authentication-Results: mx.google.com; dmarc=pass header.from=example.com",
                    "Authentication-Results: mx.google.com; dmarc=fail header.from=example.com",
                ]
            ),
            "Other@Example.com",
            "Can you confirm the later fail?",
        ),
        _message("106", "Sent", None, "stranger@example.com", "Thanks, all set."),
        _message(
            "107",
            "Inbox",
            PASS,
            "Bao <bao@example.com>",
            "hello from a colleague",
        ),
        _message(
            "108",
            "Inbox",
            "Authentication-Results: mx.google.com; dmarc=pass header.from=mail.example.com",
            "Other@Example.com",
            "Can you confirm the subdomain?",
        ),
    ]
    return "\n".join(messages) + "\n"


def _message(number: str, labels: str, auth: str | None, sender: str, body: str) -> str:
    ident = f"1700000000000000{number}"
    lines = [
        f"From {ident}@xxx Tue Nov 14 00:00:00 +0000 2023",
        f"X-GM-THRID: {ident}",
        f"X-GM-MSGID: {ident}",
        f"X-Gmail-Labels: {labels}",
    ]
    if auth:
        lines.append(auth)
    lines.extend(
        [
            f"From: {sender}",
            "To: Ada <ada@example.com>",
            "Subject: note",
            "Date: Tue, 14 Nov 2023 22:13:20 +0000",
            f"Message-ID: <owner-{number}@example.com>",
            "MIME-Version: 1.0",
            'Content-Type: text/plain; charset="utf-8"',
            "",
            body,
            "",
        ]
    )
    return "\n".join(lines)
