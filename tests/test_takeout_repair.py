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
        self.assertEqual(result.takeout_sent, "")
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
        self.assertEqual(found.takeout_sent, "")
        self.assertEqual(found.takeout_warnings, ())
        self.assertIn("Postausgang", found.takeout_unmapped)
        self.assertIsNone(self._cursor("takeout_sent"))
        self.assertEqual(self._cursor("owner_flag"), "kept")
        self.assertEqual(self._labels(1819), ["mbox:Postausgang"])
        self.assertEqual(self._row(1819)["is_from_me"], 0)
        self.assertEqual(self._row(1800)["is_from_me"], 0)
        again = repair_takeout(self.conn)
        self.conn.commit()
        self.assertEqual(again.sent, "")
        self.assertIsNone(self._cursor("takeout_sent"))

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
            self.assertEqual(missed.takeout_sent, "")
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
            kept = short.execute(
                "SELECT value FROM cursors WHERE stream = 'owner_flag'"
            ).fetchone()
            self.assertEqual(kept[0], "kept")
            sent_note = short.execute(
                "SELECT value FROM cursors WHERE stream = 'takeout_sent'"
            ).fetchone()
            self.assertIsNone(sent_note)
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
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map={"FooSent": "SENT"},
        )
        self.assertEqual(result.takeout_sent, "override:foosent")
        self.assertEqual(self._labels(1601), ["SENT"])
        self.assertEqual(self._row(1601)["is_from_me"], 1)
        self.assertEqual(self._cursor("takeout_sent"), "override:foosent")

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
                ) VALUES (1, 1, 'sent-old', 1, ?, 0, ?, 'a', 'a', 1)
                """,
                (HEADER_STAMP, f"mbox:{mbox.resolve()}#0"),
            )
            conn.execute(
                """
                INSERT INTO messages(
                  msg_pk, account_id, gmail_message_id, thread_pk, internal_date, is_from_me,
                  raw_path, raw_sha256, provenance_hash, imported_at
                ) VALUES (2, 2, 'club-old', 2, ?, 1, ?, 'b', 'b', 1)
                """,
                (HEADER_STAMP, f"mbox:{club.resolve()}#0"),
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
            self.assertEqual(got[0]["sent-old"], (1, RECEIPT_STAMP, 1))
            self.assertEqual(got[0]["club-old"], (1, RECEIPT_STAMP, 1))
            self.assertEqual(got[1], [(1, "SENT"), (2, "mbox:Club")])
            digest = hashlib.sha256(b"").hexdigest()
            self.assertEqual(
                got[2],
                [
                    (1, "takeout_label_map", digest),
                    (2, "owner_flag", "kept"),
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
                ) VALUES (1, 'sent-old', 1, ?, 'Ada Fixture <ada@example.com>', 0, ?, 'sent', 'prov', 1)
                """,
                (RECEIPT_STAMP, f"mbox:{takeout.resolve()}#0"),
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
            self.assertEqual(labels, ["SENT"])
            mines = [int(row[0]) for row in conn.execute("SELECT is_from_me FROM messages ORDER BY msg_pk")]
            self.assertEqual(mines, [0] * 22 + [1])
            note = conn.execute(
                "SELECT value FROM cursors WHERE stream = 'takeout_sent'"
            ).fetchone()
            self.assertTrue(note is None or not str(note[0]).startswith("proposed:"))
            granted = repair_takeout(conn, {"Outbox": "SENT"})
            conn.commit()
            self.assertEqual(granted.sent, "")
            self.assertEqual(granted.skipped, ())
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


def _hop(number: int, header: str) -> str:
    return _recent(number, "Caixa", "Ada Fixture <ada@example.com>", "inbound").replace(
        "X-Gmail-Labels: Caixa\n",
        "X-Gmail-Labels: Caixa\n" + header,
    )


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
        self.assertEqual(result.takeout_sent, "")
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
        self.assertEqual(reported["takeout_sent"], "")
        self.assertEqual(reported["takeout_skipped"], [])
        self.assertEqual(reported["takeout_warnings"], [])
        self.assertIn("Thư rác", reported["takeout_unmapped"])

    def test_a_mixed_user_label_is_not_proposed_or_applied(self) -> None:
        parts = [
            _recent(number, "Clientes", "Ada Fixture <ada@example.com>", "note")
            for number in range(4000, 4019)
        ]
        parts.append(_recent(4019, "Clientes", "Bao Fixture <bao@example.com>", "note"))
        path = self.home / "clientes.mbox"
        path.write_text("".join(parts), encoding="utf-8")
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(result.takeout_sent, "")
        self.assertIn("Clientes", result.takeout_unmapped)
        self.assertEqual(self._labels(4000), ["mbox:Clientes"])
        self.assertEqual(self._mine(4019), 0)
        mapped = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map={"Clientes": "SENT"},
        )
        self.assertEqual(mapped.takeout_sent, "override:clientes")
        self.assertEqual(mapped.takeout_skipped, (("Clientes", 1),))
        self.assertEqual(self._labels(4000), ["SENT"])
        self.assertEqual(self._mine(4000), 1)
        self.assertEqual(self._labels(4019), ["mbox:Clientes"])
        self.assertEqual(self._mine(4019), 0)

    def test_an_all_owner_unknown_label_is_only_proposed(self) -> None:
        parts = [
            _recent(number, "Postausgang", "Ada Fixture <ada@example.com>", "note")
            for number in range(4100, 4120)
        ]
        path = self.home / "out.mbox"
        path.write_text("".join(parts), encoding="utf-8")
        result = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(result.takeout_sent, "proposed:Postausgang")
        self.assertEqual(result.takeout_warnings, ("Postausgang",))
        self.assertEqual(self._labels(4100), ["mbox:Postausgang"])
        self.assertEqual(self._mine(4100), 0)
        self.assertIn("Postausgang", result.takeout_unmapped)

    def test_same_label_map_does_not_reread_the_mbox(self) -> None:
        first = "Thu Jan 02 10:00:00 +0000 2020"
        second = "Thu Feb 02 10:00:00 +0000 2020"
        path = self.home / "once.mbox"
        path.write_text(
            f"From 1601@example.com {first}\n"
            "X-Gmail-Labels: FooSent\n"
            "From: Bao Fixture <bao@example.com>\n"
            "To: Ada Fixture <ada@example.com>\n"
            "Subject: synthetic\n"
            f"Date: {HEADER}\n"
            "Message-ID: <once@example.com>\n"
            "\n"
            "note\n",
            encoding="utf-8",
        )
        mapping = {"FooSent": "SENT"}
        import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=mapping,
        )
        self.conn.close()
        path.write_text(path.read_text(encoding="utf-8").replace(first, second), encoding="utf-8")
        again = connect(self.home / "personal.db")
        try:
            stamp = int(
                again.execute(
                    "SELECT internal_date FROM messages WHERE gmail_message_id = ?",
                    (gmail_hex("1601", decimal_only=True),),
                ).fetchone()[0]
            )
            self.assertEqual(stamp, calendar.timegm((2020, 1, 2, 10, 0, 0)))
            repair_takeout(again, mapping)
            stamp = int(
                again.execute(
                    "SELECT internal_date FROM messages WHERE gmail_message_id = ?",
                    (gmail_hex("1601", decimal_only=True),),
                ).fetchone()[0]
            )
            self.assertEqual(stamp, calendar.timegm((2020, 1, 2, 10, 0, 0)))
        finally:
            again.close()
        self.conn = connect(self.home / "personal.db")

    def test_sent_map_skips_non_owners_and_reverts_without_that_file(self) -> None:
        spam = self.home / "spam.mbox"
        spam.write_text(
            "".join(
                _recent(number, "Thư rác", "Ada Fixture <ada@example.com>", "spam")
                for number in range(5000, 5025)
            ),
            encoding="utf-8",
        )
        proposed = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=spam,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(proposed.takeout_sent, "proposed:Thư rác")
        self.assertEqual(proposed.takeout_warnings, ("Thư rác",))
        self.assertEqual(self._mine(5000), 0)
        self.assertEqual(self._labels(5000), ["mbox:Thư rác"])
        later = self.home / "later.mbox"
        later.write_text(
            _recent(5025, "Thư rác", "Mal Fixture <mal@example.com>", "spam")
            + _recent(5026, "Thư rác", "Mal Fixture <mal@example.com>", "spam")
            + _recent(5027, "Thư rác", "Pat Fixture <pat@example.com>", "note"),
            encoding="utf-8",
        )
        owners = frozenset({"pat@example.com"})
        mapping = {"Thư rác": "SENT"}
        mapped = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=later,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=mapping,
            owners=owners,
        )
        self.assertEqual(mapped.takeout_skipped, (("Thư rác", 2),))
        self.assertEqual(self._labels(5000), ["SENT"])
        self.assertEqual(self._mine(5000), 1)
        self.assertEqual(self._labels(5024), ["SENT"])
        self.assertEqual(self._mine(5027), 1)
        self.assertEqual(self._labels(5025), ["mbox:Thư rác"])
        self.assertEqual(self._mine(5025), 0)
        self.assertEqual(self._mine(5026), 0)
        kept = calendar.timegm((2026, 10, 15, 10, 0, 0))
        self.assertEqual(self._stamp(5000), kept)
        spam.write_text(
            spam.read_text(encoding="utf-8").replace(RECENT, "Thu Jan 02 10:00:00 +0000 2020"),
            encoding="utf-8",
        )
        extra = self.home / "extra.mbox"
        extra.write_text(
            _recent(5028, "Thư rác", "Ada Fixture <ada@example.com>", "later"),
            encoding="utf-8",
        )
        import_mbox(
            self.conn,
            alias="personal",
            mbox_path=extra,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=mapping,
            owners=owners,
        )
        self.assertEqual(self._stamp(5000), kept)
        self.assertEqual(self._mine(5028), 1)
        self.assertEqual(self._labels(5028), ["SENT"])
        config = self.home / "config.toml"
        config.write_text(
            "\n".join(
                [
                    "[accounts.personal]",
                    'email = "ada@example.com"',
                    'owners = ["pat@example.com"]',
                    "",
                    "[takeout.personal.label_map]",
                    '"Thư rác" = "SENT"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        status = run_mailcrawl(
            self.home,
            "--json",
            "--home",
            str(self.home),
            "--config",
            str(config),
            "takeout",
            "status",
            "--account",
            "personal",
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        reported = json.loads(status.stdout)
        self.assertEqual(reported["takeout_skipped"], [{"count": 2, "label": "Thư rác"}])
        self.assertEqual(reported["takeout_warnings"], [])
        club = self.home / "club.mbox"
        club.write_text(
            _recent(5099, "Club", "Ada Fixture <ada@example.com>", "note"),
            encoding="utf-8",
        )
        cleared = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=club,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
        )
        self.assertEqual(cleared.takeout_skipped, ())
        for number in (*range(5000, 5029),):
            self.assertEqual(self._labels(number), ["mbox:Thư rác"], number)
            self.assertEqual(self._mine(number), 0, number)
        self.assertEqual(self._labels(5099), ["mbox:Club"])
        granted = repair_takeout(self.conn, mapping, owners=owners)
        self.conn.commit()
        self.assertEqual(granted.skipped, (("Thư rác", 2),))
        self.assertEqual(self._mine(5000), 1)
        self.assertEqual(self._mine(5027), 1)
        self.assertEqual(self._mine(5028), 1)
        self.assertEqual(self._mine(5025), 0)
        self.assertEqual(self._labels(5025), ["mbox:Thư rác"])

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
        repair_takeout(self.conn, {"Enviados": "SENT"}, rewrite_unprovable=True)
        self.conn.commit()
        self.assertEqual(self._labels(6000), ["Label_9", "mbox:Club"])
        self.assertEqual(self._mine(6000), 0)

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
                label_map={"Foo Sent": "SENT", "foo:sent": "INBOX"},
            )
        self.assertEqual(str(caught.exception), "takeout label_map repeats a name")
        self.assertIsNone(self.conn.execute("SELECT msg_pk FROM messages").fetchone())

    def test_reimport_keeps_a_sent_grant(self) -> None:
        path = self.home / "caixa.mbox"
        path.write_text(
            _recent(7000, "Caixa", "Ada Fixture <ada@example.com>", "note"),
            encoding="utf-8",
        )
        mapping = {"Caixa": "SENT"}
        kwargs = dict(
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=mapping,
        )
        import_mbox(self.conn, **kwargs)
        self.assertEqual(self._labels(7000), ["SENT"])
        self.assertEqual(self._mine(7000), 1)
        again = import_mbox(self.conn, **kwargs)
        self.assertEqual(again.n_dup, 1)
        self.assertEqual(self._labels(7000), ["SENT"])
        self.assertEqual(self._mine(7000), 1)
        self.assertEqual(self._observed(7000, "SENT"), calendar.timegm((2026, 10, 15, 10, 0, 0)))

    def test_an_overlapping_export_keeps_the_grant(self) -> None:
        first = self.home / "first.mbox"
        first.write_text(
            _recent(7100, "Caixa", "Ada Fixture <ada@example.com>", "one"),
            encoding="utf-8",
        )
        second = self.home / "second.mbox"
        second.write_text(
            _recent(7100, "Caixa", "Ada Fixture <ada@example.com>", "one")
            + _recent(7101, "Caixa", "Ada Fixture <ada@example.com>", "two"),
            encoding="utf-8",
        )
        mapping = {"Caixa": "SENT"}
        import_mbox(
            self.conn,
            alias="personal",
            mbox_path=first,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=mapping,
        )
        overlapped = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=second,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=mapping,
        )
        self.assertEqual(overlapped.n_dup, 1)
        self.assertEqual(overlapped.n_new, 1)
        self.assertEqual(self._labels(7100), ["SENT"])
        self.assertEqual(self._mine(7100), 1)
        self.assertEqual(self._labels(7101), ["SENT"])
        self.assertEqual(self._mine(7101), 1)

    def test_dropping_one_sent_key_keeps_the_other(self) -> None:
        path = self.home / "two.mbox"
        path.write_text(
            _recent(7200, "Caixa", "Ada Fixture <ada@example.com>", "a")
            + _recent(7201, "Saida", "Ada Fixture <ada@example.com>", "b"),
            encoding="utf-8",
        )
        both = {"Caixa": "SENT", "Saida": "SENT"}
        import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map=both,
        )
        self.assertEqual(self._labels(7200), ["SENT"])
        self.assertEqual(self._labels(7201), ["SENT"])
        kept = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map={"Saida": "SENT"},
        )
        self.assertEqual(kept.takeout_skipped, ())
        self.assertEqual(self._labels(7201), ["SENT"])
        self.assertEqual(self._mine(7201), 1)
        self.assertEqual(self._labels(7200), ["mbox:Caixa"])
        self.assertEqual(self._mine(7200), 0)
        self.assertEqual(self._observed(7200, "mbox:Caixa"), calendar.timegm((2026, 10, 15, 10, 0, 0)))

    def test_inbound_headers_are_not_granted_and_takeout_sent_still_is(self) -> None:
        path = self.home / "hops.mbox"
        path.write_text(
            _recent(7300, "Caixa", "Ada Fixture <ada@example.com>", "sent")
            + _hop(7301, "Delivered-To: ada@example.com\n")
            + _hop(
                7302,
                "Received: from mail.evil.example (mail.evil.example [203.0.113.9]) by mx.google.com with ESMTPS id x\n",
            )
            + _hop(
                7303,
                "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=ada@example.com\n",
            )
            + _hop(7304, "Received: by 2002:a05:6402:4e0:b0:4a:8f:3a with SMTP id abc\n")
            + _hop(7305, "Received: from mail-sor-f41.google.com by mx.google.com with ESMTPS id y\n"),
            encoding="utf-8",
        )
        mapped = import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=None,
            taxonomy_path=None,
            account_email="ada@example.com",
            label_map={"Caixa": "SENT"},
        )
        self.assertEqual(mapped.takeout_skipped, (("Caixa", 3),))
        for number in (7300, 7304, 7305):
            self.assertEqual(self._labels(number), ["SENT"], number)
            self.assertEqual(self._mine(number), 1, number)
        for number in (7301, 7302, 7303):
            self.assertEqual(self._labels(number), ["mbox:Caixa"], number)
            self.assertEqual(self._mine(number), 0, number)

    def _observed(self, number: int, label_id: str) -> int:
        row = self.conn.execute(
            """
            SELECT observed_at FROM message_labels
            JOIN messages ON messages.msg_pk = message_labels.msg_pk
            WHERE gmail_message_id = ? AND gmail_label_id = ?
            """,
            (gmail_hex(str(number), decimal_only=True), label_id),
        ).fetchone()
        return int(row[0])

    def _stamp(self, number: int) -> int:
        row = self.conn.execute(
            "SELECT internal_date FROM messages WHERE gmail_message_id = ?",
            (gmail_hex(str(number), decimal_only=True),),
        ).fetchone()
        return int(row[0])

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
