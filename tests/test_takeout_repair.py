"""Localized Takeout labels, the unknown-locale Sent fallback, and time reevaluation."""

import calendar
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mailcrawl.ingest import import_mbox
from mailcrawl.mbox import gmail_hex
from mailcrawl.paths import UsageError
from mailcrawl.rules import apply_rules, bundled_rules_path, load_rules
from mailcrawl.schema import SCHEMA_SQL, USER_VERSION
from mailcrawl.reevaluate import reevaluate_time
from mailcrawl.store import connect, init_account, migrate
from mailcrawl.takeout_repair import repair_takeout
from mailcrawl.takeout_labels import LABEL_SOURCES, system_id

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = "Thu Jan 02 10:00:00 +0000 2020"
HEADER = "Thu, 02 Jan 2020 11:00:00 +0000"
RECEIPT_STAMP = calendar.timegm((2020, 1, 2, 10, 0, 0))
HEADER_STAMP = calendar.timegm((2020, 1, 2, 11, 0, 0))
AS_OF = calendar.timegm((2026, 10, 17, 12, 0, 0))
RULES = bundled_rules_path()


def run_mailcrawl(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["http_proxy"] = "http://127.0.0.1:9"
    env["https_proxy"] = "http://127.0.0.1:9"
    env.pop("MAILCRAWL_HOME", None)
    return subprocess.run(
        [sys.executable, "-m", "mailcrawl", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _sent_outcome(conn: sqlite3.Connection) -> tuple[object, ...]:
    gid = gmail_hex("1501", decimal_only=True)
    row = conn.execute(
        "SELECT is_from_me, internal_date FROM messages WHERE gmail_message_id = ?",
        (gid,),
    ).fetchone()
    labels = tuple(
        str(item[0])
        for item in conn.execute(
            """
            SELECT gmail_label_id FROM message_labels
            JOIN messages ON messages.msg_pk = message_labels.msg_pk
            WHERE gmail_message_id = ?
              AND gmail_label_id NOT LIKE 'pseudo:%'
            ORDER BY gmail_label_id
            """,
            (gid,),
        )
    )
    decision = conn.execute(
        """
        SELECT rule_id, next_state FROM decisions
        JOIN messages ON messages.thread_pk = decisions.thread_pk
        WHERE messages.gmail_message_id = ? AND decisions.superseded_by IS NULL
        """,
        (gid,),
    ).fetchone()
    return (int(row[0]), int(row[1]), labels, str(decision[0]), str(decision[1]))


def _block(number: int, labels: str, sender: str, body: str) -> str:
    return (
        f"From {number}@example.com {RECEIPT}\n"
        f"X-Gmail-Labels: {labels}\n"
        f"From: {sender}\n"
        "To: Bao Fixture <bao@example.com>\n"
        "Subject: synthetic\n"
        f"Date: {HEADER}\n"
        f"Message-ID: <m{number}@example.com>\n"
        "\n"
        f"{body}\n"
        "\n"
    )


class TakeoutRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.conn = connect(init_account(self.home, "personal", "ada@example.com"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_cited_names_cover_the_requested_languages(self) -> None:
        self.assertIn("https://support.google.com/mail/answer/90559", LABEL_SOURCES)
        names = {
            "Enviado": "SENT",
            "Enviados": "SENT",
            "Messages envoyés": "SENT",
            "Gesendet": "SENT",
            "Inviati": "SENT",
            "Verzonden": "SENT",
            "Wysłane": "SENT",
            "送信済み": "SENT",
            "已发邮件": "SENT",
            "寄件備份": "SENT",
            "전송됨": "SENT",
            "отправленные": "SENT",
            "البريد المرسَل": "SENT",
            "gönderildi": "SENT",
            "भेजा गया": "SENT",
            "Marcado com estrela": "STARRED",
            "Caixa de entrada": "INBOX",
            "Postausgang": None,
            "FooSent": None,
        }
        for name, label_id in names.items():
            self.assertEqual(system_id(name), label_id, name)

    def test_import_maps_pt_es_fr_de_and_japanese_on_an_accepted_block(self) -> None:
        path = self.home / "langs.mbox"
        path.write_text(
            _block(
                1701,
                'Enviado,Aberto,"Categoria: Atualizações",Projetos',
                "Ada Fixture <ada@example.com>",
                "arquivo",
            )
            + _block(
                1702,
                "Marcado com estrela,Caixa de entrada,Rascunhos,Importante,Não lida,Lixo,Arquivado",
                "Bao Fixture <bao@example.com>",
                "caixa",
            )
            + _block(1703, "Enviados,Destacados,Bandeja de entrada", "Ada Fixture <ada@example.com>", "hola")
            + _block(1704, "Messages envoyés,Boîte de réception", "Ada Fixture <ada@example.com>", "bonjour")
            + _block(1705, "Gesendet,Posteingang,Papierkorb", "Ada Fixture <ada@example.com>", "hallo")
            + _block(1706, "送信済み,受信トレイ,未読", "Ada Fixture <ada@example.com>", "こんにちは"),
            encoding="utf-8",
        )
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(result.n_new, 6)
        self.assertEqual(
            self._labels(1701),
            ["CATEGORY_UPDATES", "SENT", "mbox:Projetos", "pseudo:opened"],
        )
        self.assertEqual(self._row(1701)["is_from_me"], 1)
        self.assertEqual(self._row(1701)["internal_date"], RECEIPT_STAMP)
        self.assertEqual(self._row(1701)["date_trusted"], 1)
        self.assertNotEqual(self._row(1701)["internal_date"], HEADER_STAMP)
        self.assertEqual(
            self._labels(1702),
            ["DRAFT", "IMPORTANT", "INBOX", "STARRED", "TRASH", "UNREAD", "pseudo:archived"],
        )
        self.assertEqual(self._row(1702)["is_from_me"], 0)
        self.assertEqual(self._labels(1703), ["INBOX", "SENT", "STARRED"])
        self.assertEqual(self._labels(1704), ["INBOX", "SENT"])
        self.assertEqual(self._labels(1705), ["INBOX", "SENT", "TRASH"])
        self.assertEqual(self._labels(1706), ["INBOX", "SENT", "UNREAD"])
        self.assertNotIn("mbox:Aberto", self._kinds())
        self.assertNotIn("mbox:Enviado", self._kinds())

    def test_a_plain_file_does_not_treat_enviado_as_sent(self) -> None:
        path = self.home / "plain.mbox"
        path.write_text(
            "From bob@example.com Thu Jan 02 10:00:00 +0000 2020\n"
            "X-Gmail-Labels: Enviado\n"
            "From: Ada Fixture <ada@example.com>\n"
            "To: Bao Fixture <bao@example.com>\n"
            "Subject: plain\n"
            f"Date: {HEADER}\n"
            "Message-ID: <plain-enviado@example.com>\n"
            "\n"
            "not a takeout block\n",
            encoding="utf-8",
        )
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual((result.n_new, result.n_refused), (1, 0))
        labels = [
            row[0]
            for row in self.conn.execute(
                "SELECT gmail_label_id FROM labels ORDER BY gmail_label_id"
            )
        ]
        self.assertNotIn("SENT", labels)
        self.assertNotIn("mbox:Enviado", labels)
        mine = self.conn.execute("SELECT is_from_me FROM messages").fetchone()
        self.assertEqual(int(mine[0]), 0)

    def test_unknown_locale_sent_is_recorded_and_a_short_list_is_not(self) -> None:
        self.assertIsNone(system_id("Postausgang"))
        long_path = self.home / "long.mbox"
        parts = []
        for number in range(1800, 1819):
            parts.append(_block(number, "Postausgang", "Ada Fixture <ada@example.com>", "out"))
        parts.append(_block(1819, "Postausgang", "Bao Fixture <bao@example.com>", "out"))
        long_path.write_text("".join(parts), encoding="utf-8")
        found = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=long_path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertIn("Postausgang", found.takeout_unmapped)
        self.assertEqual(self._labels(1819), ["mbox:Postausgang"])
        self.assertEqual(self._row(1819)["is_from_me"], 0)
        self.assertEqual(self._row(1800)["is_from_me"], 0)
        again = repair_takeout(self.conn)
        self.conn.commit()
        self.assertIn("Postausgang", again.unmapped)

        short = connect(init_account(self.home, "vcl", "ada@example.com"))
        try:
            short_path = self.home / "short.mbox"
            short_path.write_text(
                "".join(
                    _block(number, "Postausgang", "Ada Fixture <ada@example.com>", "out")
                    for number in range(1900, 1910)
                ),
                encoding="utf-8",
            )
            missed = import_mbox(
                short,
                alias="vcl",
                mbox_path=short_path,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.com",
            )
            self.assertIn("Postausgang", missed.takeout_unmapped)
            self.assertEqual(
                [
                    row[0]
                    for row in short.execute(
                        """
                        SELECT gmail_label_id FROM message_labels
                        JOIN messages ON messages.msg_pk = message_labels.msg_pk
                        WHERE gmail_message_id = ?
                        """,
                        (gmail_hex("1900", decimal_only=True),),
                    )
                ],
                ["mbox:Postausgang"],
            )
            mines = [int(row[0]) for row in short.execute("SELECT is_from_me FROM messages")]
            self.assertEqual(mines, [0] * 10)
        finally:
            short.close()

    def test_config_label_map_overrides_one_unknown_name(self) -> None:
        path = self.home / "override.mbox"
        path.write_text(
            _block(1601, "FooSent", "Ada Fixture <ada@example.com>", "one"),
            encoding="utf-8",
        )
        with self.assertRaises(UsageError):
            import_mbox(
                self.conn,
                alias="personal",
                mbox_path=path,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.com",
                label_map={"FooSent": "NOT_A_LABEL"},
            )
        with self.assertRaises(UsageError) as sent:
            import_mbox(
                self.conn,
                alias="personal",
                mbox_path=path,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.com",
                label_map={"FooSent": "SENT"},
            )
        self.assertEqual(str(sent.exception), "add FooSent to the Takeout label table via PR")
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map={"FooSent": "INBOX"},
        )
        self.assertEqual(result.n_new, 1)
        self.assertEqual(self._labels(1601), ["INBOX"])
        self.assertEqual(self._row(1601)["is_from_me"], 0)

    def test_v8_migration_restores_enviado_and_keeps_an_account_with_no_sent(self) -> None:
        mbox = self.home / "old.mbox"
        mbox.write_text(
            _block(1501, "Enviado", "Ada Fixture <ada@example.com>", "old"),
            encoding="utf-8",
        )
        club = self.home / "club.mbox"
        club.write_text(
            _block(1502, "Club", "Ada Fixture <ada@example.com>", "club"),
            encoding="utf-8",
        )
        path = self.home / "old.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_SQL)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA user_version = 8")
            conn.execute(
                "INSERT INTO accounts(account_id, alias, email_hash, created_at) VALUES (1, 'personal', 'abc', 1)"
            )
            conn.execute(
                "INSERT INTO accounts(account_id, alias, email_hash, created_at) VALUES (2, 'vcl', 'abc', 1)"
            )
            conn.execute(
                "INSERT INTO threads(thread_pk, account_id, gmail_thread_id, msg_count, image_only) VALUES (1, 1, 't1', 1, 0)"
            )
            conn.execute(
                "INSERT INTO threads(thread_pk, account_id, gmail_thread_id, msg_count, image_only) VALUES (2, 2, 't2', 1, 0)"
            )
            conn.execute(
                """
                INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
                VALUES (1, 'mbox:Enviado', 'Enviado', 'user', 0)
                """
            )
            conn.execute(
                """
                INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
                VALUES (2, 'mbox:Club', 'Club', 'user', 0)
                """
            )
            conn.execute(
                """
                INSERT INTO messages(
                  msg_pk, account_id, gmail_message_id, thread_pk, internal_date, is_from_me,
                  raw_path, raw_sha256, provenance_hash, imported_at
                ) VALUES (1, 1, ?, 1, ?, 0, ?, 'a', 'a', 1)
                """,
                (gmail_hex("1501", decimal_only=True), HEADER_STAMP, f"mbox:{mbox.resolve()}#0"),
            )
            conn.execute(
                """
                INSERT INTO messages(
                  msg_pk, account_id, gmail_message_id, thread_pk, internal_date, is_from_me,
                  raw_path, raw_sha256, provenance_hash, imported_at
                ) VALUES (2, 2, ?, 2, ?, 1, ?, 'b', 'b', 1)
                """,
                (gmail_hex("1502", decimal_only=True), HEADER_STAMP, f"mbox:{club.resolve()}#0"),
            )
            conn.execute(
                "INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at) VALUES (1, 1, 'mbox:Enviado', 0)"
            )
            conn.execute(
                "INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at) VALUES (2, 2, 'mbox:Club', 0)"
            )
            conn.commit()
            migrate(conn)

            def snapshot():
                flags = {
                    str(row[0]): (int(row[1]), int(row[2]), int(row[3]))
                    for row in conn.execute(
                        "SELECT gmail_message_id, is_from_me, internal_date, date_trusted FROM messages ORDER BY msg_pk"
                    )
                }
                labels = [
                    (int(row[0]), str(row[1]))
                    for row in conn.execute(
                        "SELECT msg_pk, gmail_label_id FROM message_labels ORDER BY msg_pk, gmail_label_id"
                    )
                ]
                notes = [
                    (int(row[0]), str(row[1]), str(row[2]))
                    for row in conn.execute(
                        "SELECT account_id, stream, value FROM cursors ORDER BY account_id, stream"
                    )
                ]
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                return flags, labels, notes, version

            got = snapshot()
            self.assertEqual(got[0][gmail_hex("1501", decimal_only=True)], (1, RECEIPT_STAMP, 1))
            self.assertEqual(got[0][gmail_hex("1502", decimal_only=True)], (0, RECEIPT_STAMP, 1))
            self.assertEqual(got[1], [(1, "SENT"), (2, "mbox:Club")])
            digest = hashlib.sha256(b"").hexdigest()
            self.assertEqual(
                got[2],
                [
                    (1, "takeout_label_map", digest),
                    (2, "takeout_label_map", digest),
                ],
            )
            self.assertEqual(got[3], USER_VERSION)
            conn.execute("PRAGMA user_version = 8")
            conn.commit()
            migrate(conn)
            self.assertEqual(snapshot(), got)
        finally:
            conn.close()

    def test_a_v7_plain_mbox_does_not_propose_or_grant_sent(self) -> None:
        plain = self.home / "plain.mbox"
        blob = bytearray()
        offsets: list[int] = []
        for number in range(22):
            offsets.append(len(blob))
            blob += (
                "From MAILER-DAEMON Sun Jan 04 10:00:00 2026\n"
                "X-Gmail-Labels: Outbox\n"
                "From: Ada Fixture <ada@example.com>\n"
                "To: Bao Fixture <bao@example.com>\n"
                "Subject: synthetic\n"
                "Date: Sun, 04 Jan 2026 10:00:00 +0000\n"
                f"Message-ID: <plain{number}@example.com>\n"
                "\n"
                "note\n"
                "\n"
            ).encode()
        plain.write_bytes(blob)
        takeout = self.home / "real.mbox"
        takeout.write_text(
            _block(1501, "Enviado", "Ada Fixture <ada@example.com>", "old"),
            encoding="utf-8",
        )
        path = self.home / "v7.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_SQL)
            conn.execute("PRAGMA foreign_keys = ON")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
            if "last_msg_from_me" not in columns:
                conn.execute("ALTER TABLE threads ADD COLUMN last_msg_from_me INTEGER")
            digest = hashlib.sha256(b"ada@example.com").hexdigest()
            conn.execute(
                "INSERT INTO accounts(account_id, alias, email_hash, created_at) VALUES (1, 'personal', ?, 1)",
                (digest,),
            )
            conn.execute(
                "INSERT INTO threads(thread_pk, account_id, gmail_thread_id, msg_count, image_only, last_msg_from_me) VALUES (1, 1, 't1', 23, 0, 1)"
            )
            conn.execute(
                """
                INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
                VALUES (1, 'mbox:Outbox', 'Outbox', 'user', 0),
                       (1, 'mbox:Enviado', 'Enviado', 'user', 0)
                """
            )
            for number, offset in enumerate(offsets):
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, internal_date, from_hdr, is_from_me,
                      raw_path, raw_sha256, provenance_hash, imported_at
                    ) VALUES (1, ?, 1, ?, 'Ada Fixture <ada@example.com>', 0, ?, ?, 'prov', 1)
                    """,
                    (
                        f"plain-{number}",
                        RECEIPT_STAMP,
                        f"mbox:{plain.resolve()}#{offset}",
                        f"sha-{number}",
                    ),
                )
                msg_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                    VALUES (?, 1, 'mbox:Outbox', ?)
                    """,
                    (msg_pk, RECEIPT_STAMP),
                )
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, from_hdr, is_from_me,
                  raw_path, raw_sha256, provenance_hash, imported_at
                ) VALUES (1, ?, 1, ?, 'Ada Fixture <ada@example.com>', 0, ?, 'sent', 'prov', 1)
                """,
                (gmail_hex("1501", decimal_only=True), RECEIPT_STAMP, f"mbox:{takeout.resolve()}#0"),
            )
            sent_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                VALUES (?, 1, 'mbox:Enviado', ?)
                """,
                (sent_pk, RECEIPT_STAMP),
            )
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
            migrate(conn)
            labels = [
                str(row[0])
                for row in conn.execute(
                    "SELECT gmail_label_id FROM message_labels ORDER BY gmail_label_id"
                )
            ]
            self.assertEqual(labels.count("SENT"), 1)
            self.assertEqual(labels.count("mbox:Outbox"), 22)
            self.assertNotIn("mbox:Enviado", labels)
            mines = [int(row[0]) for row in conn.execute("SELECT is_from_me FROM messages ORDER BY msg_pk")]
            self.assertEqual(mines, [0] * 22 + [1])
            with self.assertRaises(UsageError) as caught:
                repair_takeout(conn, {"Outbox": "SENT"})
            self.assertEqual(str(caught.exception), "add Outbox to the Takeout label table via PR")
            self.assertEqual(
                [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT gmail_label_id FROM message_labels WHERE gmail_label_id = 'SENT'"
                    )
                ],
                ["SENT"],
            )
            self.assertEqual(
                [int(row[0]) for row in conn.execute("SELECT is_from_me FROM messages ORDER BY msg_pk")],
                [0] * 22 + [1],
            )
            self.assertNotIn(
                "last_msg_from_me",
                {row[1] for row in conn.execute("PRAGMA table_info(threads)")},
            )
        finally:
            conn.close()

    def _labels(self, number: int) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                """
                SELECT gmail_label_id FROM message_labels
                JOIN messages ON messages.msg_pk = message_labels.msg_pk
                WHERE gmail_message_id = ? ORDER BY gmail_label_id
                """,
                (gmail_hex(str(number), decimal_only=True),),
            )
        ]

    def _row(self, number: int):
        return self.conn.execute(
            "SELECT * FROM messages WHERE gmail_message_id = ?",
            (gmail_hex(str(number), decimal_only=True),),
        ).fetchone()

    def _kinds(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT gmail_label_id, kind FROM labels").fetchall())

    def _cursor(self, stream: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM cursors WHERE stream = ?",
            (stream,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0])


class TimeReevaluateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = init_account(self.home, "personal", "ada@example.com")
        self.config = self.home / "config.toml"
        self.config.write_text(
            '[accounts.personal]\nemail = "ada@example.com"\n',
            encoding="utf-8",
        )
        conn = connect(self.db)
        try:
            account = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
            when = AS_OF - 100 * 86400
            rows = (
                ("rule-thread", "rule", "rule", "rule-4"),
                ("knn-thread", "knn", "embedding-cluster", "cluster"),
                ("model-thread", "model", "llm", "local"),
                ("human-thread", "human", "human", "person"),
                ("review-thread", "rule", "rule", "rule-0"),
            )
            for thread_id, origin, source, detail in rows:
                conn.execute(
                    "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (?, ?, 1, 0)",
                    (account, thread_id),
                )
                thread_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, internal_date, from_hdr,
                      subject, body_text, is_from_me, date_trusted, raw_sha256, provenance_hash, imported_at
                    ) VALUES (?, ?, ?, ?, 'Ada <ada@example.com>', 'synthetic', 'please reply', 1, 1, ?, 'prov', 1)
                    """,
                    (account, f"m-{thread_id}", thread_pk, when, f"sha-{thread_id}"),
                )
                state = "needs_review" if thread_id == "review-thread" else "reply"
                conn.execute(
                    """
                    INSERT INTO decisions(
                      thread_pk, next_state, lane, source, origin, source_detail,
                      rule_id, rules_version, evidence, created_at
                    ) VALUES (?, ?, 'imbox', ?, ?, ?, '5', 'rules-v4', '[]', 1)
                    """,
                    (thread_pk, state, source, origin, detail),
                )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reevaluate_is_dry_run_until_apply_and_undo_restores_it(self) -> None:
        bare = run_mailcrawl(
            self.home,
            "--json",
            "--home",
            str(self.home),
            "--config",
            str(self.config),
            "rules",
            "--account",
            "personal",
            "--rules",
            str(RULES),
            "--now",
            str(AS_OF),
            "--apply",
        )
        self.assertEqual(bare.returncode, 2)
        missing = run_mailcrawl(
            self.home,
            "--json",
            "--home",
            str(self.home),
            "--config",
            str(self.config),
            "rules",
            "--account",
            "personal",
            "--rules",
            str(RULES),
            "--now",
            str(AS_OF),
            "--reevaluate",
        )
        self.assertEqual(missing.returncode, 2)
        csv_path = self.home / "plan.csv"
        dry = self._rules("--reevaluate", "--csv", str(csv_path))
        self.assertEqual(dry["applied"], False)
        self.assertEqual(dry["changed"], 3)
        self.assertEqual(dry["skipped_human"], 1)
        self.assertEqual(dry["skipped_review"], 1)
        self.assertEqual(dry["source_detail"], "time")
        with csv_path.open(encoding="utf-8", newline="") as handle:
            plan = list(csv.DictReader(handle))
        self.assertEqual(
            set(plan[0].keys()),
            {
                "gmail_thread_id",
                "origin",
                "state",
                "lane",
                "next_state",
                "next_lane",
                "changed",
                "skipped",
                "reason",
            },
        )
        by_thread = {row["gmail_thread_id"]: row for row in plan}
        self.assertEqual(by_thread["rule-thread"]["changed"], "1")
        self.assertEqual(by_thread["rule-thread"]["next_state"], "resolved")
        self.assertEqual(by_thread["human-thread"]["skipped"], "human")
        self.assertEqual(by_thread["review-thread"]["skipped"], "needs_review")
        self.assertEqual(self._current("rule-thread"), ("rule", "reply", "rule-4"))
        applied = self._rules("--reevaluate", "--apply", "--csv", str(csv_path))
        self.assertEqual(applied["applied"], True)
        self.assertEqual(applied["changed"], 3)
        self.assertEqual(self._current("rule-thread"), ("rule", "resolved", "time"))
        self.assertEqual(self._current("knn-thread"), ("knn", "resolved", "time"))
        self.assertEqual(self._current("model-thread"), ("model", "resolved", "time"))
        self.assertEqual(self._rule("model-thread"), "5")
        self.assertEqual(self._current("human-thread"), ("human", "reply", "person"))
        self.assertEqual(self._current("review-thread"), ("rule", "needs_review", "rule-0"))
        undone = run_mailcrawl(
            self.home,
            "--json",
            "--home",
            str(self.home),
            "--config",
            str(self.config),
            "review",
            "undo",
            "--account",
            "personal",
            "--source-detail",
            "time",
            "--now",
            str(AS_OF),
        )
        self.assertEqual(undone.returncode, 0, undone.stderr)
        self.assertEqual(self._current("rule-thread"), ("rule", "reply", "rule-4"))
        self.assertEqual(self._current("knn-thread"), ("knn", "reply", "cluster"))
        self.assertEqual(self._current("model-thread"), ("model", "reply", "local"))
        self.assertEqual(self._current("human-thread"), ("human", "reply", "person"))

    def test_dry_run_export_includes_time_columns(self) -> None:
        export = self.home / "decisions.csv"
        result = run_mailcrawl(
            self.home,
            "--json",
            "--home",
            str(self.home),
            "--config",
            str(self.config),
            "dry-run",
            "--account",
            "personal",
            "--rules",
            str(RULES),
            "--now",
            str(AS_OF),
            "--export-decisions",
            str(export),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        header = export.read_text(encoding="utf-8").splitlines()[0]
        for column in (
            "state",
            "lane",
            "last_human",
            "age_days",
            "date_trusted",
            "past_deadline",
            "reason",
        ):
            self.assertIn(column, header.split(","))
        self.assertNotIn("@", export.read_text(encoding="utf-8"))

    def test_time_supersede_of_a_harness_row_is_not_a_harness_decision(self) -> None:
        conn = connect(self.db)
        try:
            account = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
            conn.execute(
                """
                INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only)
                VALUES (?, 'harness-thread', 1, 0)
                """,
                (account,),
            )
            thread_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, from_hdr,
                  subject, body_text, is_from_me, date_trusted, raw_sha256, provenance_hash, imported_at
                ) VALUES (?, 'm-harness', ?, ?, 'Bao Fixture <bao@example.com>', 'synthetic', 'note', 0, 1, 'sha-harness', 'prov', 1)
                """,
                (account, thread_pk, calendar.timegm((2020, 1, 2, 10, 0, 0))),
            )
            conn.execute(
                """
                INSERT INTO decisions(
                  thread_pk, next_state, lane, source, origin, source_detail,
                  rule_id, rules_version, evidence, created_at
                ) VALUES (?, 'reply', 'imbox', 'llm', 'model', 'harness-fixture', 'harness', 'review-v1', '[]', 1)
                """,
                (thread_pk,),
            )
            conn.commit()
            reevaluate_time(conn, now=AS_OF, apply=True)
            current = conn.execute(
                """
                SELECT origin, rule_id, source_detail, next_state FROM decisions
                WHERE thread_pk = ? AND superseded_by IS NULL
                """,
                (thread_pk,),
            ).fetchone()
            self.assertEqual(
                (
                    str(current["origin"]),
                    str(current["rule_id"]),
                    str(current["source_detail"]),
                    str(current["next_state"]),
                ),
                ("model", "time", "time", "reference"),
            )
            self.assertEqual(self._rule("model-thread"), "5")
        finally:
            conn.close()

    def _rule(self, thread_id: str) -> str:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                """
                SELECT rule_id FROM decisions
                JOIN threads ON threads.thread_pk = decisions.thread_pk
                WHERE gmail_thread_id = ? AND superseded_by IS NULL
                """,
                (thread_id,),
            ).fetchone()
        finally:
            conn.close()
        return str(row[0])

    def _rules(self, *extra: str) -> dict:
        result = run_mailcrawl(
            self.home,
            "--json",
            "--home",
            str(self.home),
            "--config",
            str(self.config),
            "rules",
            "--account",
            "personal",
            "--rules",
            str(RULES),
            "--now",
            str(AS_OF),
            *extra,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def _current(self, thread_id: str) -> tuple[str, str, str]:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                """
                SELECT origin, next_state, source_detail FROM decisions
                JOIN threads ON threads.thread_pk = decisions.thread_pk
                WHERE gmail_thread_id = ? AND superseded_by IS NULL
                """,
                (thread_id,),
            ).fetchone()
        finally:
            conn.close()
        return (str(row[0]), str(row[1]), str(row[2]))


RECENT = "Thu Oct 15 10:00:00 +0000 2026"
RECENT_HEADER = "Thu, 15 Oct 2026 10:00:00 +0000"


def _recent(number: int, labels: str, sender: str, body: str, thrid: int | None = None) -> str:
    thread = f"X-GM-THRID: {thrid}\n" if thrid is not None else ""
    return (
        f"From {number}@example.com {RECENT}\n"
        f"{thread}"
        f"X-Gmail-Labels: {labels}\n"
        f"From: {sender}\n"
        "To: Bao Fixture <bao@example.com>\n"
        "Subject: synthetic\n"
        f"Date: {RECENT_HEADER}\n"
        f"Message-ID: <m{number}@example.com>\n"
        "\n"
        f"{body}\n"
        "\n"
    )


class SentPoisonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.conn = connect(init_account(self.home, "personal", "ada@example.com"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_known_sent_keeps_forged_spam_as_mail(self) -> None:
        parts = [_recent(3000, "Đã gửi", "Ada Fixture <ada@example.com>", "note")]
        for number in range(3001, 3026):
            parts.append(_recent(number, "Thư rác", "Ada Fixture <ada@example.com>", "spam"))
        parts.append(
            _recent(3026, "Thư rác", "Cam Fixture <cam@example.com>", "Can you send the file?", 3025)
        )
        path = self.home / "vi.mbox"
        path.write_text("".join(parts), encoding="utf-8")
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertIn("Thư rác", result.takeout_unmapped)
        self.assertEqual(self._labels(3000), ["SENT"])
        self.assertEqual(self._mine(3000), 1)
        self.assertEqual(self._labels(3001), ["mbox:Thư rác"])
        self.assertEqual(self._mine(3001), 0)
        self.assertEqual(self._labels(3026), ["mbox:Thư rác"])
        self.assertEqual(self._mine(3026), 0)
        apply_rules(self.conn, now=AS_OF, config=load_rules(RULES))
        decision = self.conn.execute(
            """
            SELECT rule_id, next_state FROM decisions
            JOIN messages ON messages.thread_pk = decisions.thread_pk
            WHERE messages.gmail_message_id = ? AND superseded_by IS NULL
            """,
            (gmail_hex("3026", decimal_only=True),),
        ).fetchone()
        self.assertEqual((decision[0], decision[1]), ("5", "reply"))
        status = run_mailcrawl(
            self.home,
            "--json",
            "--home",
            str(self.home),
            "takeout",
            "status",
            "--account",
            "personal",
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        reported = json.loads(status.stdout)
        self.assertEqual(reported["takeout_unmapped"], ["Thư rác"])

    def test_an_unknown_name_stays_unmapped(self) -> None:
        path = self.home / "out.mbox"
        path.write_text(
            "".join(
                _recent(number, "Postausgang", "Ada Fixture <ada@example.com>", "out")
                for number in range(1800, 1820)
            ),
            encoding="utf-8",
        )
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(result.takeout_unmapped, ("Postausgang",))
        self.assertEqual(self._labels(1800), ["mbox:Postausgang"])
        self.assertEqual(self._mine(1800), 0)

    def test_a_sent_target_is_rejected_for_every_spelling(self) -> None:
        path = self.home / "spell.mbox"
        path.write_text(
            _recent(1601, "FooSent", "Ada Fixture <ada@example.com>", "one"),
            encoding="utf-8",
        )
        for target in ("SENT", "sent", "Sent", "Enviado"):
            with self.assertRaises(UsageError) as caught:
                import_mbox(
                    self.conn,
                    alias="personal",
                    mbox_path=path,
                    labels_path=None,
                    taxonomy_path=None,
                    account_email="ada@example.com",
                    label_map={"FooSent": target},
                )
            self.assertEqual(str(caught.exception), "add FooSent to the Takeout label table via PR")
        self.assertIsNone(self.conn.execute("SELECT msg_pk FROM messages").fetchone())

    def test_label_map_rejects_names_that_normalize_together(self) -> None:
        path = self.home / "dup.mbox"
        path.write_text(
            _block(1701, "FooSent", "Ada Fixture <ada@example.com>", "one"),
            encoding="utf-8",
        )
        with self.assertRaises(UsageError) as caught:
            import_mbox(
                self.conn,
                alias="personal",
                mbox_path=path,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.com",
                label_map={"Foo Sent": "INBOX", "foo:sent": "STARRED"},
            )
        self.assertEqual(str(caught.exception), "takeout label_map repeats a name")
        self.assertIsNone(self.conn.execute("SELECT msg_pk FROM messages").fetchone())

    def test_same_label_map_does_not_reread_the_mbox(self) -> None:
        first = "Thu Jan 02 10:00:00 +0000 2020"
        second = "Thu Feb 02 10:00:00 +0000 2020"
        path = self.home / "once.mbox"
        path.write_text(
            _block(1601, "FooSent", "Bao Fixture <bao@example.com>", "note"),
            encoding="utf-8",
        )
        mapping = {"FooSent": "INBOX"}
        import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=mapping,
        )
        self.assertEqual(self._labels(1601), ["INBOX"])
        self.conn.close()
        path.write_text(path.read_text(encoding="utf-8").replace(first, second), encoding="utf-8")
        again = connect(self.home / "personal.db")
        try:
            repair_takeout(again, mapping)
            stamp = int(
                again.execute(
                    "SELECT internal_date FROM messages WHERE gmail_message_id = ?",
                    (gmail_hex("1601", decimal_only=True),),
                ).fetchone()[0]
            )
            self.assertEqual(stamp, RECEIPT_STAMP)
        finally:
            again.close()
        self.conn = connect(self.home / "personal.db")

    def test_gog_label_ids_are_not_remapped_to_sent(self) -> None:
        path = self.home / "bob.mbox"
        path.write_text(
            _recent(6000, "Club", "Bob Fixture <bob@example.com>", "note"),
            encoding="utf-8",
        )
        import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        account = int(self.conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        msg_pk = int(
            self.conn.execute(
                "SELECT msg_pk FROM messages WHERE gmail_message_id = ?",
                (gmail_hex("6000", decimal_only=True),),
            ).fetchone()[0]
        )
        self.conn.execute(
            """
            INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
            VALUES (?, 'Label_9', 'Enviados', 'user', 0)
            """,
            (account,),
        )
        self.conn.execute(
            """
            INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
            VALUES (?, ?, 'Label_9', 0)
            """,
            (msg_pk, account),
        )
        self.conn.commit()
        repair_takeout(self.conn, rewrite_unprovable=True)
        self.conn.commit()
        self.assertEqual(self._labels(6000), ["Label_9", "mbox:Club"])
        self.assertEqual(self._mine(6000), 0)
        with self.assertRaises(UsageError):
            repair_takeout(self.conn, {"Enviados": "SENT"})
        self.assertEqual(self._labels(6000), ["Label_9", "mbox:Club"])

    def test_reexport_to_the_same_path_keeps_each_message(self) -> None:
        path = self.home / "All mail Including Spam and Trash.mbox"
        front_receipt = "Fri Mar 01 12:00:00 +0000 2024"
        client_receipt = "Sat Jun 06 08:00:00 +0000 2021"
        front_stamp = calendar.timegm((2024, 3, 1, 12, 0, 0))
        client_stamp = calendar.timegm((2021, 6, 6, 8, 0, 0))
        sent = _block(2401, "Sent", "Ada Fixture <ada@example.com>", "sent body")
        client = _block(2402, "Inbox", "Bao Fixture <bao@example.com>", "client body").replace(
            RECEIPT, client_receipt
        )
        front = _block(2400, "Inbox", "Cam Fixture <cam@example.com>", "front").replace(
            RECEIPT, front_receipt
        )
        path.write_text(sent + client, encoding="utf-8")
        import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        sent_path = str(self._row(2401)["raw_path"])
        path.write_text(front + sent + client, encoding="utf-8")
        repair_takeout(self.conn, rewrite_unprovable=True)
        self.conn.commit()
        self.assertEqual(self._labels(2401), ["SENT"])
        self.assertEqual(self._mine(2401), 1)
        self.assertEqual(int(self._row(2401)["internal_date"]), RECEIPT_STAMP)
        self.assertEqual(int(self._row(2401)["date_trusted"]), 1)
        self.assertEqual(int(self._row(2402)["internal_date"]), client_stamp)
        self.assertEqual(int(self._row(2402)["date_trusted"]), 1)
        again = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(again.n_dup, 2)
        self.assertEqual(again.n_new, 1)
        self.assertNotEqual(str(self._row(2401)["raw_path"]), sent_path)
        self.assertTrue(str(self._row(2401)["raw_path"]).endswith(f"#{len(front.encode())}"))
        repair_takeout(self.conn, rewrite_unprovable=True)
        self.conn.commit()
        self.assertEqual(self._labels(2401), ["SENT"])
        self.assertEqual(self._mine(2401), 1)
        self.assertEqual(int(self._row(2401)["internal_date"]), RECEIPT_STAMP)
        self.assertEqual(int(self._row(2402)["internal_date"]), client_stamp)
        self.assertEqual(int(self._row(2402)["date_trusted"]), 1)
        self.assertEqual(int(self._row(2400)["internal_date"]), front_stamp)
        self.assertNotEqual(int(self._row(2402)["internal_date"]), front_stamp)

    def test_offset_alignment_does_not_copy_another_message(self) -> None:
        path = self.home / "All mail Including Spam and Trash.mbox"
        sent = _block(8800, "Sent", "Ada Fixture <ada@example.com>", "clean").encode()
        path.write_bytes(b"x" * 240 + sent)
        account = int(self.conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        self.conn.execute(
            "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (?, 'forged-thread', 1, 0)",
            (account,),
        )
        thread_pk = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, from_hdr, subject,
              is_from_me, date_trusted, raw_path, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, 'forged-inbound', ?, 111, 'Ada Fixture <ada@example.com>', 'Re: Contrato',
                      0, 0, ?, 'forged', 'forged', 1)
            """,
            (account, thread_pk, f"mbox:{path.resolve()}#240"),
        )
        msg_pk = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.conn.execute(
            """
            INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
            VALUES (?, 'INBOX', 'INBOX', 'system', 0)
            """,
            (account,),
        )
        self.conn.execute(
            """
            INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
            VALUES (?, ?, 'INBOX', 111)
            """,
            (msg_pk, account),
        )
        self.conn.commit()
        repair_takeout(self.conn, rewrite_unprovable=True)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT is_from_me, internal_date, date_trusted FROM messages WHERE gmail_message_id = 'forged-inbound'"
        ).fetchone()
        self.assertEqual((int(row[0]), int(row[1]), int(row[2])), (0, 111, 0))
        labels = [
            item[0]
            for item in self.conn.execute(
                "SELECT gmail_label_id FROM message_labels WHERE msg_pk = ?",
                (msg_pk,),
            )
        ]
        self.assertEqual(labels, ["INBOX"])

    def test_a_missing_mbox_leaves_the_row_unchanged(self) -> None:
        account = int(self.conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        self.conn.execute(
            "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (?, 'gone', 1, 0)",
            (account,),
        )
        thread_pk = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, is_from_me, date_trusted,
              raw_path, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, 'gone-msg', ?, 111, 0, 1, ?, 'gone', 'gone', 1)
            """,
            (account, thread_pk, "mbox:/tmp/does-not-exist-mailcrawl.mbox#240"),
        )
        msg_pk = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.conn.execute(
            """
            INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
            VALUES (?, 'mbox:Enviado', 'Enviado', 'user', 0)
            """,
            (account,),
        )
        self.conn.execute(
            """
            INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
            VALUES (?, ?, 'mbox:Enviado', 111)
            """,
            (msg_pk, account),
        )
        self.conn.commit()
        repair_takeout(self.conn, rewrite_unprovable=True)
        self.conn.commit()
        row = self.conn.execute(
            """
            SELECT is_from_me, internal_date, date_trusted
            FROM messages WHERE gmail_message_id = 'gone-msg'
            """
        ).fetchone()
        self.assertEqual((int(row[0]), int(row[1]), int(row[2])), (0, 111, 1))
        self.assertEqual(
            [
                item[0]
                for item in self.conn.execute(
                    "SELECT gmail_label_id FROM message_labels WHERE msg_pk = ?",
                    (msg_pk,),
                )
            ],
            ["mbox:Enviado"],
        )

    def test_v7_and_v8_clear_a_forged_from_that_has_no_sent_label(self) -> None:
        def plant(version: int) -> sqlite3.Connection:
            path = self.home / f"v{version}.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA_SQL)
            conn.execute("PRAGMA foreign_keys = ON")
            if version == 7:
                conn.execute("ALTER TABLE messages DROP COLUMN date_trusted")
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute(
                "INSERT INTO accounts(account_id, alias, email_hash, created_at) VALUES (1, 'personal', 'abc', 1)"
            )
            conn.execute(
                "INSERT INTO threads(thread_pk, account_id, gmail_thread_id, msg_count, image_only) VALUES (1, 1, 'contrato', 2, 0)"
            )
            conn.execute(
                """
                INSERT INTO messages(
                  msg_pk, account_id, gmail_message_id, thread_pk, internal_date, from_hdr,
                  subject, is_from_me, raw_sha256, provenance_hash, imported_at
                ) VALUES (1, 1, 'bob-request', 1, 111, 'Bob Fixture <bob@example.com>',
                          'Contrato', 0, 'a', 'a', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO messages(
                  msg_pk, account_id, gmail_message_id, thread_pk, internal_date, from_hdr,
                  subject, is_from_me, raw_sha256, provenance_hash, imported_at
                ) VALUES (2, 1, 'forged-reply', 1, 222, 'Ada Fixture <ada@example.com>',
                          'Re: Contrato', 1, 'b', 'b', 1)
                """
            )
            conn.commit()
            return conn

        old = plant(7)
        try:
            migrate(old)
            reply = old.execute(
                "SELECT is_from_me FROM messages WHERE subject = 'Re: Contrato'"
            ).fetchone()
            request = old.execute(
                "SELECT is_from_me FROM messages WHERE gmail_message_id = 'bob-request'"
            ).fetchone()
            self.assertEqual(int(reply[0]), 0)
            self.assertEqual(int(request[0]), 0)
        finally:
            old.close()
        kept = plant(8)
        try:
            migrate(kept)
            reply = kept.execute(
                "SELECT is_from_me FROM messages WHERE subject = 'Re: Contrato'"
            ).fetchone()
            self.assertEqual(int(reply[0]), 0)
        finally:
            kept.close()

    def test_dropping_the_retired_column_can_be_retried(self) -> None:
        path = self.home / "retry.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_SQL)
            conn.execute("PRAGMA user_version = 9")
            conn.execute("ALTER TABLE threads ADD COLUMN last_msg_from_me INTEGER")
            conn.execute(
                """
                CREATE TABLE takeout_label_override (
                  msg_pk INTEGER NOT NULL,
                  label_key TEXT NOT NULL,
                  previous_id TEXT NOT NULL,
                  applied_id TEXT NOT NULL,
                  replaced INTEGER NOT NULL,
                  PRIMARY KEY (msg_pk, label_key)
                )
                """
            )
            conn.execute(
                "INSERT INTO cursors(account_id, stream, value, updated_at) VALUES (1, 'takeout_sent', 'proposed:X', 0)"
            )
            conn.commit()
            migrate(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("last_msg_from_me", columns)
            self.assertNotIn("takeout_label_override", names)
            self.assertIsNone(
                conn.execute("SELECT value FROM cursors WHERE stream = 'takeout_sent'").fetchone()
            )
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), USER_VERSION)
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
            migrate(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("last_msg_from_me", columns)
            self.assertNotIn("takeout_label_override", names)
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), USER_VERSION)
        finally:
            conn.close()

    def test_rfc822_mbox_labels_are_dropped_and_do_not_steer_rules(self) -> None:
        path = self.home / "v7-plain.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_SQL)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("ALTER TABLE messages DROP COLUMN date_trusted")
            conn.execute("PRAGMA user_version = 7")
            conn.execute(
                "INSERT INTO accounts(account_id, alias, email_hash, created_at) VALUES (1, 'personal', 'abc', 1)"
            )
            conn.execute(
                """
                INSERT INTO senders(sender_id, account_id, address_norm, domain, display_name, msg_count)
                VALUES (1, 1, 'bob@example.com', 'example.com', 'Bob Fixture', 23)
                """
            )
            conn.execute(
                """
                INSERT INTO labels(account_id, gmail_label_id, name, kind, catalogue_key, seen_at)
                VALUES (1, 'mbox:Outbox', 'Outbox', 'user', 'CATEGORY_PROMOTIONS', 0)
                """
            )
            recent = AS_OF - 16 * 86400
            for number in range(22):
                conn.execute(
                    "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (1, ?, 1, 0)",
                    (f"out-{number}",),
                )
                thread_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, internal_date, sender_id, from_hdr,
                      subject, body_text, is_from_me, raw_path, raw_sha256, provenance_hash, imported_at
                    ) VALUES (1, ?, ?, ?, 1, 'Bob Fixture <bob@example.com>', 'synthetic',
                              'Can you send the file?', 0, ?, ?, 'prov', 1)
                    """,
                    (
                        f"rfc822:{number:064x}",
                        thread_pk,
                        recent,
                        f"mbox:{self.home / 'missing.mbox'}#0",
                        f"sha-{number}",
                    ),
                )
                msg_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                    VALUES (?, 1, 'mbox:Outbox', 111)
                    """,
                    (msg_pk,),
                )
            conn.execute(
                "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (1, 'plain-mail', 1, 0)"
            )
            plain_thread = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, sender_id, from_hdr,
                  subject, body_text, is_from_me, raw_path, raw_sha256, provenance_hash, imported_at
                ) VALUES (1, 'rfc822:plain', ?, ?, 1, 'Bob Fixture <bob@example.com>', 'synthetic',
                          'Can you send the file?', 0, ?, 'plain', 'prov', 1)
                """,
                (plain_thread, recent, f"mbox:{self.home / 'missing.mbox'}#0"),
            )
            conn.commit()
            migrate(conn)
            left = conn.execute(
                "SELECT count(*) FROM message_labels WHERE gmail_label_id = 'mbox:Outbox'"
            ).fetchone()
            self.assertEqual(int(left[0]), 0)
            apply_rules(conn, now=AS_OF, config=load_rules(RULES))
            labeled = conn.execute(
                """
                SELECT decisions.rule_id, decisions.next_state
                FROM decisions
                JOIN messages ON messages.thread_pk = decisions.thread_pk
                WHERE messages.gmail_message_id = ?
                  AND decisions.superseded_by IS NULL
                """,
                (f"rfc822:{0:064x}",),
            ).fetchone()
            control = conn.execute(
                """
                SELECT decisions.rule_id, decisions.next_state
                FROM decisions
                JOIN messages ON messages.thread_pk = decisions.thread_pk
                WHERE messages.gmail_message_id = 'rfc822:plain'
                  AND decisions.superseded_by IS NULL
                """
            ).fetchone()
            self.assertEqual((labeled[0], labeled[1]), ("5", "reply"))
            self.assertEqual((control[0], control[1]), ("5", "reply"))
        finally:
            conn.close()

    def test_migrated_v7_and_v8_match_a_fresh_import(self) -> None:
        path = self.home / "enviado.mbox"
        receipt = "Thu Oct 01 12:00:00 +0000 2026"
        recent = calendar.timegm((2026, 10, 1, 12, 0, 0))
        path.write_text(
            _block(1501, "Enviado", "Ada Fixture <ada@example.com>", "Can you confirm?").replace(
                RECEIPT, receipt
            ),
            encoding="utf-8",
        )
        fresh = connect(init_account(self.home, "vcl", "ada@example.com"))
        try:
            import_mbox(
                fresh,
                alias="vcl",
                mbox_path=path,
                labels_path=None,
                taxonomy_path=None,
                account_email="ada@example.com",
            )
            apply_rules(fresh, now=AS_OF, config=load_rules(RULES))
            imported = _sent_outcome(fresh)
            self.assertEqual(imported, (1, recent, ("SENT",), "6", "waiting"))
        finally:
            fresh.close()
        for version in (7, 8):
            conn = sqlite3.connect(self.home / f"src-{version}.db")
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(SCHEMA_SQL)
                conn.execute("PRAGMA foreign_keys = ON")
                if version == 7:
                    conn.execute("ALTER TABLE messages DROP COLUMN date_trusted")
                conn.execute(f"PRAGMA user_version = {version}")
                conn.execute(
                    "INSERT INTO accounts(account_id, alias, email_hash, created_at) VALUES (1, 'personal', 'abc', 1)"
                )
                conn.execute(
                    "INSERT INTO threads(thread_pk, account_id, gmail_thread_id, msg_count, image_only) VALUES (1, 1, 't', 1, 0)"
                )
                conn.execute(
                    """
                    INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
                    VALUES (1, 'mbox:Enviado', 'Enviado', 'user', 0)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO messages(
                      msg_pk, account_id, gmail_message_id, thread_pk, internal_date, from_hdr,
                      subject, body_text, is_from_me, raw_path, raw_sha256, provenance_hash, imported_at
                    ) VALUES (1, 1, ?, 1, ?, 'Ada Fixture <ada@example.com>', 'synthetic',
                              'Can you confirm?', 0, ?, 'a', 'a', 1)
                    """,
                    (
                        gmail_hex("1501", decimal_only=True),
                        HEADER_STAMP,
                        f"mbox:{path.resolve()}#0",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                    VALUES (1, 1, 'mbox:Enviado', 0)
                    """
                )
                conn.commit()
                migrate(conn)
                apply_rules(conn, now=AS_OF, config=load_rules(RULES))
                self.assertEqual(_sent_outcome(conn), imported)
            finally:
                conn.close()

    def test_a_from_line_inside_the_body_is_not_a_separator(self) -> None:
        number = 8801
        receipt = "Fri Mar 01 12:00:00 +0000 2024"
        path = self.home / "body.mbox"
        path.write_text(
            _block(
                number,
                "Sent",
                "Ada Fixture <ada@example.com>",
                f"note\nFrom {number}@xxx {receipt}\nstill",
            ),
            encoding="utf-8",
        )
        raw = path.read_bytes()
        offset = raw.index(f"From {number}@xxx ".encode())
        self.assertGreater(offset, 0)
        account = int(self.conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        self.conn.execute(
            "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (?, 'mid', 1, 0)",
            (account,),
        )
        thread_pk = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, is_from_me, date_trusted,
              raw_path, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, ?, ?, 111, 0, 0, ?, 'mid', 'mid', 1)
            """,
            (
                account,
                gmail_hex(str(number), decimal_only=True),
                thread_pk,
                f"mbox:{path.resolve()}#{offset}",
            ),
        )
        self.conn.commit()
        repair_takeout(self.conn, rewrite_unprovable=True)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT internal_date, date_trusted FROM messages WHERE gmail_message_id = ?",
            (gmail_hex(str(number), decimal_only=True),),
        ).fetchone()
        self.assertEqual((int(row[0]), int(row[1])), (111, 0))

    def test_a_numeric_plain_sender_loses_mbox_labels_without_the_file(self) -> None:
        recent = AS_OF - 16 * 86400
        numeric_id = gmail_hex("123456789", decimal_only=True)
        takeout_id = gmail_hex("1501", decimal_only=True)
        daemon_id = "rfc822:" + "ab" * 32
        path = self.home / "v7-numeric.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_SQL)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("ALTER TABLE messages DROP COLUMN date_trusted")
            conn.execute("PRAGMA user_version = 7")
            conn.execute(
                "INSERT INTO accounts(account_id, alias, email_hash, created_at) VALUES (1, 'personal', 'abc', 1)"
            )
            conn.execute(
                """
                INSERT INTO senders(sender_id, account_id, address_norm, domain, display_name, msg_count)
                VALUES (1, 1, 'bob@example.com', 'example.com', 'Bob Fixture', 4)
                """
            )
            conn.execute(
                """
                INSERT INTO labels(account_id, gmail_label_id, name, kind, catalogue_key, seen_at)
                VALUES (1, 'mbox:Outbox', 'Outbox', 'user', 'CATEGORY_PROMOTIONS', 0),
                       (1, 'mbox:Clientes', 'Clientes', 'user', NULL, 0)
                """
            )
            rows = (
                (numeric_id, "mbox:" + "11" * 32),
                (daemon_id, "mbox:" + "22" * 32),
                ("rfc822:" + "33" * 32, "mbox:" + "33" * 32),
                (takeout_id, str(takeout_id)),
            )
            for gid, thread_id in rows:
                conn.execute(
                    "INSERT INTO threads(account_id, gmail_thread_id, msg_count, image_only) VALUES (1, ?, 1, 0)",
                    (thread_id,),
                )
                thread_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO messages(
                      account_id, gmail_message_id, thread_pk, internal_date, sender_id, from_hdr,
                      subject, body_text, is_from_me, raw_path, raw_sha256, provenance_hash, imported_at
                    ) VALUES (1, ?, ?, ?, 1, 'Bob Fixture <bob@example.com>', 'synthetic',
                              'Can you send the file?', 0, ?, ?, 'prov', 1)
                    """,
                    (gid, thread_pk, recent, f"mbox:{self.home / 'gone.mbox'}#0", f"sha-{gid}"),
                )
                if gid == "rfc822:" + "33" * 32:
                    continue
                msg_pk = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                    VALUES (?, 1, 'mbox:Outbox', ?), (?, 1, 'mbox:Clientes', ?)
                    """,
                    (msg_pk, recent, msg_pk, recent),
                )
            conn.commit()
            migrate(conn)
            def labels_of(gid: str) -> list[str]:
                return [
                    str(item[0])
                    for item in conn.execute(
                        """
                        SELECT gmail_label_id FROM message_labels
                        JOIN messages ON messages.msg_pk = message_labels.msg_pk
                        WHERE messages.gmail_message_id = ?
                        ORDER BY gmail_label_id
                        """,
                        (gid,),
                    )
                ]

            self.assertEqual(labels_of(numeric_id), [])
            self.assertEqual(labels_of(daemon_id), [])
            self.assertEqual(labels_of(takeout_id), ["mbox:Clientes", "mbox:Outbox"])
            apply_rules(conn, now=AS_OF, config=load_rules(RULES))

            def route(gid: str) -> tuple[str, str]:
                row = conn.execute(
                    """
                    SELECT decisions.rule_id, decisions.next_state
                    FROM decisions
                    JOIN messages ON messages.thread_pk = decisions.thread_pk
                    WHERE messages.gmail_message_id = ?
                      AND decisions.superseded_by IS NULL
                    """,
                    (gid,),
                ).fetchone()
                return (str(row[0]), str(row[1]))

            self.assertEqual(route(numeric_id), ("5", "reply"))
            self.assertEqual(route("rfc822:" + "33" * 32), ("5", "reply"))
        finally:
            conn.close()

    def test_a_numeric_plain_separator_stores_an_rfc822_id(self) -> None:
        path = self.home / "numeric-plain.mbox"
        path.write_text(
            "From 123456789@qq.com Thu Oct 01 12:00:00 +0000 2026\n"
            "X-Gmail-Labels: Outbox,Clientes\n"
            "From: Bob Fixture <bob@example.com>\n"
            "To: Bao Fixture <bao@example.com>\n"
            "Subject: synthetic\n"
            "Date: Thu, 01 Oct 2026 12:00:00 +0000\n"
            "Message-ID: <plain-num@example.com>\n"
            "\n"
            "Can you send the file?\n"
            "\n"
            "From MAILER-DAEMON Thu Oct 01 12:00:00 +0000 2026\n"
            "X-Gmail-Labels: Outbox,Clientes\n"
            "From: Bob Fixture <bob@example.com>\n"
            "To: Bao Fixture <bao@example.com>\n"
            "Subject: synthetic\n"
            "Date: Thu, 01 Oct 2026 12:00:00 +0000\n"
            "Message-ID: <plain-daemon@example.com>\n"
            "\n"
            "Can you send the file?\n",
            encoding="utf-8",
        )
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual((result.n_new, result.n_refused), (2, 0))
        stored = [
            str(row[0])
            for row in self.conn.execute("SELECT gmail_message_id FROM messages ORDER BY gmail_message_id")
        ]
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(item.startswith("rfc822:") for item in stored))
        self.assertNotIn(gmail_hex("123456789", decimal_only=True), stored)
        threads = [
            str(row[0])
            for row in self.conn.execute("SELECT gmail_thread_id FROM threads")
        ]
        self.assertTrue(all(item.startswith("mbox:") for item in threads))

    def test_an_invalid_takeout_separator_id_is_refused(self) -> None:
        path = self.home / "bad-id.mbox"
        huge = str(1 << 64)
        path.write_text(
            "From 0@xxx Thu Oct 01 12:00:00 +0000 2026\n"
            "X-Gmail-Labels: Sent,Clientes\n"
            "From: Ada Fixture <ada@example.com>\n"
            "To: Bao Fixture <bao@example.com>\n"
            "Subject: synthetic\n"
            "Date: Thu, 01 Oct 2026 12:00:00 +0000\n"
            "Message-ID: <zero@example.com>\n"
            "\n"
            "note\n"
            "\n"
            f"From {huge}@xxx Thu Oct 01 12:00:00 +0000 2026\n"
            "X-Gmail-Labels: Sent,Clientes\n"
            "From: Ada Fixture <ada@example.com>\n"
            "To: Bao Fixture <bao@example.com>\n"
            "Subject: synthetic\n"
            "Date: Thu, 01 Oct 2026 12:00:00 +0000\n"
            "Message-ID: <huge@example.com>\n"
            "\n"
            "note\n"
            "\n"
            "From 1501@xxx Thu Oct 01 12:00:00 +0000 2026\n"
            "X-GM-THRID: 4242\n"
            "X-Gmail-Labels: Sent,Clientes\n"
            "From: Ada Fixture <ada@example.com>\n"
            "To: Bao Fixture <bao@example.com>\n"
            "Subject: synthetic\n"
            "Date: Thu, 01 Oct 2026 12:00:00 +0000\n"
            "Message-ID: <ok@example.com>\n"
            "\n"
            "note\n",
            encoding="utf-8",
        )
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual((result.n_new, result.n_refused), (1, 2))
        stored = [
            str(row[0])
            for row in self.conn.execute("SELECT gmail_message_id FROM messages")
        ]
        self.assertEqual(stored, [gmail_hex("1501", decimal_only=True)])
        self.assertFalse(any(item.startswith("rfc822:") for item in stored))
        labels = [
            str(row[0])
            for row in self.conn.execute(
                """
                SELECT gmail_label_id FROM message_labels
                JOIN messages ON messages.msg_pk = message_labels.msg_pk
                WHERE messages.gmail_message_id = ?
                ORDER BY gmail_label_id
                """,
                (gmail_hex("1501", decimal_only=True),),
            )
        ]
        self.assertIn("SENT", labels)
        self.assertIn("mbox:Clientes", labels)

    def _labels(self, number: int) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                """
                SELECT gmail_label_id FROM message_labels
                JOIN messages ON messages.msg_pk = message_labels.msg_pk
                WHERE gmail_message_id = ? ORDER BY gmail_label_id
                """,
                (gmail_hex(str(number), decimal_only=True),),
            )
        ]

    def _mine(self, number: int) -> int:
        row = self.conn.execute(
            "SELECT is_from_me FROM messages WHERE gmail_message_id = ?",
            (gmail_hex(str(number), decimal_only=True),),
        ).fetchone()
        return int(row[0])

    def _row(self, number: int):
        return self.conn.execute(
            "SELECT * FROM messages WHERE gmail_message_id = ?",
            (gmail_hex(str(number), decimal_only=True),),
        ).fetchone()

