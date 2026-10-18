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
    "       dkim=pass header.d=example.com;\n"
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
        self.assertEqual(stored["Can you confirm the dkim?"], (0, None))
        self.assertEqual(stored["Can you confirm the later fail?"], (1, "alias"))
        self.assertEqual(stored["Thanks, all set."], (1, "sent"))
        self.assertEqual(stored["hello from a colleague"], (0, None))
        self.assertEqual(stored["Can you confirm the subdomain?"], (0, None))
        report = self._json(["from-me", "--account", "vcl"])
        self.assertEqual(
            report,
            {
                "account": "vcl",
                "alias": 2,
                "applied": False,
                "changed": 0,
                "ok": True,
                "sent": 1,
                "unchanged": 8,
                "untrusted": 4,
            },
        )
        decided = self._json(["rules", "--account", "vcl", "--now", str(NOW)])
        self.assertEqual(decided["ok"], True)
        self.assertEqual(self._decisions()["Can you confirm?"], ("6", "waiting"))
        self.assertEqual(self._decisions()["Can you confirm the spoof?"], ("5", "reply"))
        self.assertEqual(self._decisions()["Can you confirm the later pass?"], ("5", "reply"))
        self.assertEqual(self._decisions()["Can you confirm the dkim?"], ("5", "reply"))
        self.assertEqual(self._decisions()["Can you confirm the later fail?"], ("6", "waiting"))
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
                    "mx.google.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com",
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

    def test_forged_authentication_results_stay_untrusted(self) -> None:
        db = init_account(self.home, "vcl", "ada@example.com")
        aligned = "dkim=pass header.d=example.com; dmarc=pass header.from=example.com"
        cases = {
            "aligned": (f"mx.google.com; {aligned}", 1, "alias"),
            "quoted-local": (
                'mx.google.com; spf=pass (google.com: domain of "x)"@example.com designates sender) '
                'smtp.mailfrom="x)"@example.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com',
                1,
                "alias",
            ),
            "trailing-dot": (
                "mx.google.com; dkim=pass header.d=example.com.; dmarc=none header.from=example.com",
                1,
                "alias",
            ),
            "second-dkim": (
                "mx.google.com; dkim=pass header.d=evil.example; "
                "dkim=pass header.d=example.com; dmarc=pass header.from=example.com",
                1,
                "alias",
            ),
            "comment-escape": (
                r'mx.google.com; spf=pass (google.com: domain of "x\);dmarc=pass header.from=example.com; '
                r'dkim=pass header.d=example.com;\(y dmarc=fail header.from=evil.example"@evil.example)',
                0,
                None,
            ),
            "unescaped-paren": (
                'mx.google.com; spf=pass (google.com: domain of "x);dmarc=pass header.from=example.com; '
                'dkim=pass header.d=example.com;(y dmarc=fail header.from=evil.example)"@evil.example)',
                0,
                None,
            ),
            "visible-fail": (
                r'mx.google.com; spf=pass (google.com: domain of "x\);dmarc=pass header.from=example.com;\(y"@evil.example '
                "designates sender); dmarc=fail header.from=evil.example",
                0,
                None,
            ),
            "header-s": (
                "mx.google.com; dkim=pass header.s=header.d=example.com; dmarc=pass header.from=example.com",
                0,
                None,
            ),
            "xheader": (
                "mx.google.com; dkim=pass header.d=evil.example; policy.xheader.from=example.com; "
                "dmarc=pass header.from=example.com",
                0,
                None,
            ),
            "authserv-at": (f"mx.google.com@evil.example; {aligned}", 0, None),
            "authserv-extra": (f"mx.google.com 1; {aligned}", 0, None),
            "dmarc-only": ("mx.google.com; dmarc=pass header.from=example.com", 0, None),
            "dkim-and-fail": (
                "mx.google.com; dmarc=fail header.from=example.com; dkim=pass header.d=example.com",
                0,
                None,
            ),
            "two-dmarc": (
                "mx.google.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com; "
                "dmarc=pass header.from=example.com",
                0,
                None,
            ),
            "same-clause-fail": (
                "mx.google.com; dkim=pass header.d=example.com; dmarc=pass dmarc=fail header.from=example.com",
                0,
                None,
            ),
            "first-header-d": (
                "mx.google.com; dkim=pass header.d=evil.example header.d=example.com; "
                "dmarc=pass header.from=example.com",
                0,
                None,
            ),
            "header-i": (
                "mx.google.com; dkim=pass header.i=@example.com; dmarc=pass header.from=example.com",
                1,
                "alias",
            ),
            "i-before-d": (
                "mx.google.com; dkim=pass header.i=@evil.example header.d=example.com; "
                "dmarc=pass header.from=example.com",
                0,
                None,
            ),
            "subdomain": (
                "mx.google.com; dkim=pass header.d=mail.example.com; dmarc=pass header.from=mail.example.com",
                0,
                None,
            ),
            "unbalanced-quote": (
                'mx.google.com; dkim=pass header.d="example.com; dmarc=pass header.from=example.com',
                0,
                None,
            ),
            "trailing-escape": (
                "mx.google.com; dkim=pass header.d=example.com\\",
                0,
                None,
            ),
        }
        conn = connect(db)
        try:
            account = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
            thread = conn.execute(
                """
                INSERT INTO threads(account_id, gmail_thread_id)
                VALUES (?, 'thread-forged')
                RETURNING thread_pk
                """,
                (account,),
            ).fetchone()[0]
            for name, (auth, _mine, _origin) in cases.items():
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, internal_date,
                      from_hdr, auth_results, body_text, is_from_me,
                      raw_sha256, provenance_hash, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'prov', ?)
                    """,
                    (
                        account,
                        name,
                        thread,
                        NOW,
                        "Other <Other@Example.com>",
                        auth,
                        name,
                        name,
                        NOW,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        preview = self._json(["from-me", "--account", "vcl"])
        self.assertEqual(preview["applied"], False)
        self.assertEqual(preview["alias"], 5)
        self.assertEqual(self._stored(db)["aligned"], (0, None))
        applied = self._json(["from-me", "--account", "vcl", "--apply"])
        self.assertEqual(applied["applied"], True)
        self.assertEqual(applied["alias"], 5)
        self.assertEqual(applied["untrusted"], len(cases) - 5)
        stored = self._stored(db)
        for name, (_auth, mine, origin) in cases.items():
            self.assertEqual(stored[name], (mine, origin), name)

    def test_encoded_words_are_not_mine_and_gmail_header_i_is(self) -> None:
        self._init()
        mbox = self.home / "takeout.mbox"
        mbox.write_text(_mbox_auth(_AUTH_FIXTURES), encoding="utf-8")
        imported = self._json(["import", "mbox", str(mbox), "--alias", "vcl"])
        self.assertEqual(imported["ok"], True)
        export = self.home / "gog"
        account = export / "gmail" / "acct"
        messages = account / "messages"
        messages.mkdir(parents=True)
        (account / "labels.json").write_text(
            '[{"id":"INBOX","name":"INBOX","type":"system"}]\n',
            encoding="utf-8",
        )
        lines = []
        for number, (body, auth) in enumerate(_ENCODED, start=1):
            name = f"gog-{number}.eml"
            (messages / name).write_bytes(_eml(number, auth, body))
            lines.append(
                json.dumps(
                    {
                        "id": f"gog-{number}",
                        "threadId": f"thread-gog-{number}",
                        "labelIds": ["INBOX"],
                        "internalDate": 1700000000000,
                        "eml": f"gmail/acct/messages/{name}",
                        "from": "Other <other@example.com>",
                        "subject": "note",
                    }
                )
            )
        (messages / "index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        gog = self._json(["import", "gog-export", "--dir", str(export), "--account", "vcl"])
        self.assertEqual(gog["ok"], True)
        stored = self._rows()
        auth = self._auth()
        for body, mine, origin in _AUTH_EXPECT:
            self.assertEqual(stored[body], (mine, origin), body)
        for body, _mine, _origin in _AUTH_EXPECT:
            if body.startswith("encoded"):
                self.assertIn("=?", auth[body], body)
                self.assertNotIn("dkim=pass", auth[body], body)
        for body, _auth in _ENCODED:
            self.assertEqual(stored[f"gog {body}"], (0, None), body)
            self.assertIn("=?", auth[f"gog {body}"], body)
            self.assertNotIn("dkim=pass", auth[f"gog {body}"], body)

    def test_a_raw_byte_in_authentication_results_does_not_roll_back_the_import(self) -> None:
        self._init()
        mbox = self.home / "takeout.mbox"
        mbox.write_bytes(_binary_mbox(_CLEAN_AND_LOWER))
        imported = self._json(["import", "mbox", str(mbox), "--alias", "vcl"])
        self.assertEqual(imported["n_new"], 6)
        stored = self._rows()
        for fixture_id in ("clean-1", "clean-2", "clean-3", "clean-4", "clean-5", "lower-ff"):
            self.assertIn(fixture_id, stored, fixture_id)
        self.assertEqual(stored["lower-ff"], (1, "alias"))
        self.assertIn("\ufffd", self._auth()["lower-ff"])
        first = self.home / "first.mbox"
        first.write_bytes(
            _binary_mbox((("first-ff", _FIRST_FF, "Other <Other@Example.com>"),), start=400)
        )
        again = self._json(["import", "mbox", str(first), "--alias", "vcl"])
        self.assertEqual(again["n_new"], 1)
        self.assertEqual(self._rows()["first-ff"], (0, None))
        kept = self._auth()["first-ff"]
        kept.encode("utf-8")
        self.assertIn("\ufffd", kept)

    def test_from_me_warns_when_the_wal_file_is_not_empty(self) -> None:
        db = init_account(self.home, "vcl", "ada@example.com")
        Path(f"{db}-wal").write_bytes(b"")
        quiet = self._json(["from-me", "--account", "vcl"])
        self.assertNotIn("warning", quiet)
        Path(f"{db}-wal").write_bytes(b"pending")
        warned = self._json(["from-me", "--account", "vcl"])
        self.assertEqual(warned["warning"], "wal may be stale")
        self.assertEqual(warned["ok"], True)

    def test_from_me_dry_run_does_not_migrate(self) -> None:
        db = init_account(self.home, "vcl", "ada@example.com")
        conn = sqlite3.connect(db)
        try:
            conn.execute("ALTER TABLE messages DROP COLUMN from_me_origin")
            conn.execute("PRAGMA user_version = 10")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
        before = _snapshot(db)
        code, doc = self._code(["from-me", "--account", "vcl"])
        self.assertEqual(code, 2)
        self.assertEqual(doc, {"account": "vcl", "error": "needs migration", "ok": False})
        self.assertEqual(_snapshot(db), before)
        conn = sqlite3.connect(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, 10)
        self.assertNotIn("from_me_origin", columns)
        applied = self._json(["from-me", "--account", "vcl", "--apply"])
        self.assertEqual(applied["ok"], True)
        self.assertEqual(applied["applied"], True)
        conn = sqlite3.connect(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, USER_VERSION)
        self.assertIn("from_me_origin", columns)

    def test_from_me_dry_run_rejects_a_newer_schema(self) -> None:
        db = init_account(self.home, "vcl", "ada@example.com")
        conn = sqlite3.connect(db)
        try:
            conn.execute("PRAGMA user_version = 12")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
        before = _snapshot(db)
        code, doc = self._code(["from-me", "--account", "vcl"])
        self.assertEqual(code, 3)
        self.assertEqual(doc, {"account": "vcl", "error": "unsupported_schema", "ok": False})
        self.assertEqual(_snapshot(db), before)

    def _init(self) -> None:
        self._json(["init", "--account", "vcl", "--email", "ada@example.com"])

    def _json(self, args: list[str]) -> dict[str, object]:
        code, doc = self._code(args)
        self.assertEqual(code, 0, doc)
        return doc

    def _code(self, args: list[str]) -> tuple[int, dict[str, object]]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(
                ["--json", "--home", str(self.home), "--config", str(self.config), *args]
            )
        return code, json.loads(buf.getvalue())

    def _db(self) -> Path:
        return self.home / "vcl.db"

    def _rows(self) -> dict[str, tuple[int, str | None]]:
        return self._stored(self._db())

    def _auth(self) -> dict[str, str]:
        conn = connect(self._db())
        try:
            found = conn.execute("SELECT body_text, auth_results FROM messages").fetchall()
        finally:
            conn.close()
        return {str(row[0]): "" if row[1] is None else str(row[1]) for row in found}

    def _stored(self, db: Path) -> dict[str, tuple[int, str | None]]:
        conn = connect(db)
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


def _snapshot(db: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in db.parent.iterdir():
        if path.name == db.name or path.name.startswith(db.name + "-"):
            found[path.name] = path.read_bytes()
    return found


_ENCODED_COMMENT = (
    "Authentication-Results: mx.google.com;\n"
    "       spf=pass (google.com: domain of "
    "=?utf-8?q?=29=3B_dkim=3Dpass_header.d=3Dexample.com=3B_dmarc=3Dpass_=28?=@evil.example "
    "designates 192.0.2.1 as permitted sender) smtp.mailfrom=sender@evil.example"
)
_ENCODED_HEADER = (
    "Authentication-Results: "
    "=?utf-8?q?mx.google.com=3B_dkim=3Dpass_header.d=3Dexample.com=3B_dmarc=3Dpass?="
)
_ENCODED_CLAUSE = (
    "Authentication-Results: mx.google.com;\n"
    "       =?utf-8?q?dkim=3Dpass_header.d=3Dexample.com=3B_dmarc=3Dpass?="
)
_GMAIL_I = (
    "Authentication-Results: mx.google.com;\n"
    "       dkim=pass header.i=@example.com header.s=20161025 header.b=abc;\n"
    "       spf=pass (google.com: domain of other@example.com designates 192.0.2.1 as permitted sender) "
    "smtp.mailfrom=other@example.com;\n"
    "       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com"
)
_GMAIL_ARC = (
    "Authentication-Results: mx.google.com;\n"
    "       dkim=pass header.i=@gmail.com header.s=20230601 header.b=EfGh;\n"
    "       dkim=pass header.i=@example.com header.s=google header.b=AbCd;\n"
    "       arc=pass (i=1 spf=pass spfdomain=example.com dkim=pass dkdomain=example.com "
    "dmarc=pass fromdomain=example.com);\n"
    "       spf=pass (google.com: domain of other@example.com designates 192.0.2.1 as permitted sender) "
    "smtp.mailfrom=other@example.com;\n"
    "       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com"
)
_GMAIL_SOFTFAIL = (
    "Authentication-Results: mx.google.com;\n"
    "       dkim=pass header.i=@example.com header.s=selector1 header.b=XyZ;\n"
    "       spf=softfail (google.com: domain of transitioning other@example.com does not designate "
    "192.0.2.1 as permitted sender) smtp.mailfrom=other@example.com;\n"
    "       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com"
)
_GMAIL_NO_DMARC = (
    "Authentication-Results: mx.google.com;\n"
    "       dkim=pass header.i=@example.com header.s=20161025 header.b=abc;\n"
    "       spf=pass (google.com: domain of other@example.com designates 192.0.2.1 as permitted sender) "
    "smtp.mailfrom=other@example.com"
)
_AUTH_FIXTURES = (
    ("encoded comment", _ENCODED_COMMENT, 0, None),
    ("encoded header", _ENCODED_HEADER, 0, None),
    ("encoded clause", _ENCODED_CLAUSE, 0, None),
    ("gmail header.i", _GMAIL_I, 1, "alias"),
    ("gmail arc", _GMAIL_ARC, 1, "alias"),
    ("gmail softfail", _GMAIL_SOFTFAIL, 1, "alias"),
    ("gmail no dmarc", _GMAIL_NO_DMARC, 0, None),
)
_AUTH_EXPECT = tuple((body, mine, origin) for body, _auth, mine, origin in _AUTH_FIXTURES)
_ENCODED = tuple(
    (body, auth) for body, auth, mine, _origin in _AUTH_FIXTURES if mine == 0 and body.startswith("encoded")
)


_CLEAN_AND_LOWER = (
    ("clean-1", None, "Bao <bao@example.com>"),
    ("clean-2", None, "Bao <bao@example.com>"),
    ("clean-3", None, "Bao <bao@example.com>"),
    ("clean-4", None, "Bao <bao@example.com>"),
    ("clean-5", None, "Bao <bao@example.com>"),
    (
        "lower-ff",
        b"Authentication-Results: mx.google.com;\n"
        b"       dkim=pass header.d=example.com;\n"
        b"       dmarc=pass (p=NONE sp=NONE) header.from=example.com\n"
        b"Authentication-Results: evil.example; dmarc=fail \xff",
        "Other <Other@Example.com>",
    ),
)
_FIRST_FF = (
    b"Authentication-Results: mx.google.com;\n"
    b"       dkim=pass header.d=example.com;\n"
    b"       dmarc=pass (p=NONE \xff sp=NONE) header.from=example.com"
)


def _binary_mbox(rows: tuple[tuple[str, bytes | None, str], ...], start: int = 300) -> bytes:
    messages = [
        _binary_message(str(start + index), auth, sender, body)
        for index, (body, auth, sender) in enumerate(rows)
    ]
    return b"\n".join(messages) + b"\n"


def _binary_message(number: str, auth: bytes | None, sender: str, body: str) -> bytes:
    ident = f"1700000000000000{number}".encode()
    lines = [
        b"From " + ident + b"@xxx Tue Nov 14 00:00:00 +0000 2023",
        b"X-GM-THRID: " + ident,
        b"X-GM-MSGID: " + ident,
        b"X-Gmail-Labels: Inbox",
        f"From: {sender}".encode(),
        b"To: Ada <ada@example.com>",
        b"Subject: note",
        b"Date: Tue, 14 Nov 2023 22:13:20 +0000",
        f"Message-ID: <bin-{number}@example.com>".encode(),
        b"MIME-Version: 1.0",
        b'Content-Type: text/plain; charset="utf-8"',
    ]
    if auth:
        lines.append(auth)
    lines.extend([b"", body.encode(), b""])
    return b"\n".join(lines)


def _mbox_auth(fixtures: tuple[tuple[str, str, int, str | None], ...]) -> str:
    messages = [
        _message(str(200 + index), "Inbox", auth, "Other <Other@Example.com>", body)
        for index, (body, auth, _mine, _origin) in enumerate(fixtures)
    ]
    return "\n".join(messages) + "\n"


def _eml(number: int, auth: str, body: str) -> bytes:
    text = "\r\n".join(
        [
            "From: Other <other@example.com>",
            "To: Ada <ada@example.com>",
            "Subject: note",
            "Date: Tue, 14 Nov 2023 22:13:20 +0000",
            f"Message-ID: <gog-{number}@example.com>",
            "MIME-Version: 1.0",
            'Content-Type: text/plain; charset="utf-8"',
            *auth.replace("\n", "\r\n").split("\r\n"),
            "",
            f"gog {body}",
            "",
        ]
    )
    return text.encode("utf-8")


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
                    "Authentication-Results: mx.google.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com",
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
            "Authentication-Results: mx.google.com; dkim=pass header.d=mail.example.com; dmarc=pass header.from=mail.example.com",
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
