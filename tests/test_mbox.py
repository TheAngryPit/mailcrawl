# SPDX-License-Identifier: MIT
"""Import a synthetic Takeout-style mbox through the gog-export writer."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import tracemalloc
import unicodedata
import unittest
from pathlib import Path

from mailcrawl.contract import MessageRec
from mailcrawl.ingest import import_gog_export, import_mbox
from mailcrawl.mbox import MboxSource, Refused, gmail_hex, label_names, system_label
from mailcrawl.textnorm import clean_body
from mailcrawl.store import connect, init_account, ordered_row_dump_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "testdata" / "fixtures" / "mbox"
TAKEOUT = FIXTURE / "takeout.mbox"
GOG_AFTER = FIXTURE / "gog-after"
NATIVE_LABELS = ROOT / "testdata/fixtures/gog-export/native/gmail/acct_synthetic/labels.json"
PLAIN = "18867251edfa0001"
HTML = "1886e1a4667f4002"
PARTS = "18873038f7ce4003"


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


class MboxImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.conn = connect(init_account(self.home, "personal", "ada@example.com"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def mbox(self, path: Path = TAKEOUT, labels: Path | None = None):
        return import_mbox(
            self.conn,
            alias="personal",
            mbox_path=path,
            labels_path=labels,
            taxonomy_path=None,
            account_email="ada@example.com",
        )

    def gog(self, export: Path = GOG_AFTER):
        return import_gog_export(
            self.conn,
            alias="personal",
            export_dir=export,
            taxonomy_path=None,
            account_email="ada@example.com",
        )

    def row(self, gmail_id: str):
        return self.conn.execute(
            """
            SELECT messages.*, threads.gmail_thread_id FROM messages
            JOIN threads ON threads.thread_pk = messages.thread_pk
            WHERE gmail_message_id = ?
            """,
            (gmail_id,),
        ).fetchone()

    def labels_of(self, gmail_id: str) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                """
                SELECT gmail_label_id FROM message_labels
                JOIN messages ON messages.msg_pk = message_labels.msg_pk
                WHERE gmail_message_id = ? ORDER BY gmail_label_id
                """,
                (gmail_id,),
            )
        ]

    def search(self, term: str) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                """
                SELECT messages.gmail_message_id FROM msg_fts
                JOIN messages ON messages.msg_pk = msg_fts.rowid
                WHERE msg_fts MATCH ? ORDER BY 1
                """,
                (term,),
            )
        ]

    def test_takeout_ids_threads_and_labels(self) -> None:
        result = self.mbox()
        self.assertEqual(
            (result.n_seen, result.n_new, result.n_dup, result.n_error, result.n_refused),
            (6, 5, 0, 0, 1),
        )
        plain = self.row(PLAIN)
        self.assertEqual(plain["gmail_thread_id"], PLAIN)
        self.assertEqual(plain["subject"], "Reunião de arquivo")
        self.assertEqual(plain["internal_date"], 1767258000)
        self.assertEqual(
            self.labels_of(PLAIN),
            [
                "CATEGORY_UPDATES",
                "IMPORTANT",
                "INBOX",
                "mbox:Example/Synthetic",
                "mbox:Projetos, Exemplo",
                "pseudo:opened",
            ],
        )
        self.assertEqual(
            self.labels_of(HTML), ["STARRED", "UNREAD", "pseudo:archived"]
        )
        self.assertEqual(self.labels_of(PARTS), ["SENT", "mbox:Projetos, Exemplo", "pseudo:archived"])
        self.assertEqual(self.row(PARTS)["gmail_thread_id"], PLAIN)
        self.assertIsNone(self.row("3e7"))
        kinds = dict(self.conn.execute("SELECT gmail_label_id, kind FROM labels").fetchall())
        self.assertNotIn("mbox:Etiqueta Nova", kinds)
        self.assertEqual(kinds["UNREAD"], "system")
        self.assertEqual((kinds["pseudo:opened"], kinds["pseudo:archived"]), ("system", "system"))
        self.assertNotIn("mbox:Opened", kinds)
        self.assertNotIn("mbox:Aberto", kinds)
        odd = "rfc822:" + hashlib.sha256(b"mbox-odd@example.com").hexdigest()
        self.assertEqual(self.labels_of(odd), [])

    def test_raw_sha256_drops_takeout_parts(self) -> None:
        self.mbox()
        eml = GOG_AFTER / "gmail/acct_synthetic/messages/2026/01/m1.eml"
        plain = self.row(PLAIN)
        self.assertEqual(plain["raw_sha256"], hashlib.sha256(eml.read_bytes()).hexdigest())
        self.assertTrue(plain["raw_path"].startswith("mbox:"))
        self.assertTrue(plain["raw_path"].endswith("takeout.mbox#0"))

    def test_fallback_keys_refusal_and_malformed_separator(self) -> None:
        self.mbox()
        noid = self.row("rfc822:" + hashlib.sha256(b"mbox-noid@example.com").hexdigest())
        odd = self.row("rfc822:" + hashlib.sha256(b"mbox-odd@example.com").hexdigest())
        html = self.row(HTML)
        provisional = "mbox:" + hashlib.sha256(b"mbox-html@example.com").hexdigest()
        self.assertEqual(html["gmail_thread_id"], provisional)
        self.assertEqual(noid["gmail_thread_id"], provisional)
        self.assertEqual(
            odd["gmail_thread_id"], "mbox:" + hashlib.sha256(b"mbox-odd@example.com").hexdigest()
        )
        self.assertEqual(odd["body_text"], "From the archive oddtoken.")
        self.assertEqual(self.search("refusedtoken"), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 5)
        detail = json.loads(
            self.conn.execute("SELECT detail_json FROM ledger ORDER BY seq DESC").fetchone()[0]
        )
        self.assertEqual(detail["refused"], [{"offset": detail["refused"][0]["offset"], "reason": "no_message_id"}])
        self.assertEqual(detail["source"], "mbox")

    def test_bodies_attachment_and_search(self) -> None:
        self.mbox()
        self.assertEqual(self.row(HTML)["body_text"].split(), ["Boletim", "htmltoken"])
        self.assertEqual(self.row(PARTS)["body_text"], "Segue o anexo multitoken.")
        attachment = self.conn.execute(
            "SELECT filename, extract_status, extracted_text, file_sha256 FROM attachments"
        ).fetchone()
        self.assertEqual(attachment["filename"], "mbox-note.txt")
        self.assertEqual(attachment["extract_status"], "ok")
        self.assertEqual(attachment["extracted_text"], "anexotoken synthetic note")
        self.assertEqual(self.search("anexotoken"), [PARTS])
        self.assertEqual(self.search("reuniao"), [PLAIN, PARTS])
        self.assertEqual(self.search("htmltoken"), [HTML])
        plain = self.row(PLAIN)
        self.assertNotIn("\r", plain["body_text_raw"])
        self.assertEqual(
            plain["body_text"],
            "A reunião do arquivo mboxtoken fica no índice.\nFrom the minutes crlftoken.",
        )
        self.assertEqual(self.search("crlftoken"), [PLAIN])

    def test_reimport_adds_nothing(self) -> None:
        self.mbox()
        digest = ordered_row_dump_hash(self.conn)
        again = self.mbox()
        self.assertEqual((again.n_new, again.n_dup, again.n_refused, again.n_error), (0, 5, 1, 0))
        self.assertEqual(ordered_row_dump_hash(self.conn), digest)

    def test_reimport_without_body_parse_keeps_outcomes(self) -> None:
        self.mbox()
        kept = self.row(PLAIN)
        changed = self.home / "changed.mbox"
        text = TAKEOUT.read_bytes()
        text = text.replace(b"Caixa de Entrada,Importante,", b"Importante,", 1)
        text = text.replace(b"Segue o anexo multitoken.", b"Outro anexo multitoken.", 1)
        changed.write_bytes(text)
        again = self.mbox(changed)
        self.assertEqual((again.n_new, again.n_dup, again.n_error, again.n_refused), (0, 4, 1, 1))
        self.assertNotIn("INBOX", self.labels_of(PLAIN))
        self.assertEqual(self.row(PLAIN)["body_text"], kept["body_text"])
        self.assertEqual(self.row(PARTS)["body_text"], "Segue o anexo multitoken.")
        detail = json.loads(
            self.conn.execute("SELECT detail_json FROM ledger ORDER BY seq DESC").fetchone()[0]
        )
        self.assertEqual(detail["conflicts"], [PARTS])

    def test_connect_pins_mmap_off(self) -> None:
        self.assertEqual(self.conn.execute("PRAGMA mmap_size").fetchone()[0], 0)

    def test_labels_json_matches_user_label_names(self) -> None:
        self.mbox(labels=NATIVE_LABELS)
        self.assertIn("Label_synthetic", self.labels_of(PLAIN))
        self.assertNotIn("mbox:Example/Synthetic", self.labels_of(PLAIN))
        self.assertIn("mbox:Projetos, Exemplo", self.labels_of(PLAIN))

    def test_gog_after_mbox_keeps_one_row_and_replaces_provisional_ids(self) -> None:
        self.mbox()
        kept = self.row(HTML)["raw_sha256"]
        result = self.gog()
        self.assertEqual(
            (result.n_seen, result.n_new, result.n_dup, result.n_reconciled, result.n_error),
            (2, 0, 1, 1, 0),
        )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 5)
        self.assertEqual(
            self.labels_of(PLAIN), ["INBOX", "Label_proj", "Label_synthetic", "pseudo:opened"]
        )
        self.assertEqual(self.labels_of(HTML), ["INBOX", "Label_nova", "pseudo:archived"])
        self.assertEqual(self.labels_of(PARTS), ["Label_proj", "SENT", "pseudo:archived"])
        names = dict(self.conn.execute("SELECT gmail_label_id, name FROM labels").fetchall())
        self.assertEqual(names["Label_opened"], "Opened")
        self.assertEqual(names["pseudo:opened"], "pseudo:opened")
        self.assertEqual(names["pseudo:archived"], "pseudo:archived")
        html = self.row(HTML)
        self.assertEqual(html["gmail_thread_id"], HTML)
        self.assertEqual(html["raw_sha256"], kept)
        provisional = self.conn.execute(
            "SELECT count(*) FROM labels WHERE gmail_label_id LIKE 'mbox:%'"
        ).fetchone()[0]
        self.assertEqual(provisional, 0)
        detail = json.loads(
            self.conn.execute("SELECT detail_json FROM ledger ORDER BY seq DESC").fetchone()[0]
        )
        self.assertEqual(detail["reconciled"][0]["gmail_message_id"], HTML)
        self.assertEqual(detail["reconciled"][0]["kept_raw_sha256"], kept)
        self.assertNotEqual(detail["reconciled"][0]["seen_raw_sha256"], kept)

    def pair(self, cases: list[tuple[bytes, bytes]]) -> Path:
        """Write one Takeout copy and one gog copy per case and import the Takeout side.

        Each case is (body as Takeout writes it, the same message as gog's raw).
        """
        takeout = self.home / "pair.mbox"
        export = self.home / "pair-gog"
        messages = export / "gmail/acct/messages"
        messages.mkdir(parents=True)
        (export / "gmail/acct/labels.json").write_text("[]", encoding="utf-8")
        index = []
        with takeout.open("wb") as handle:
            for number, (takeout_text, gog_raw) in enumerate(cases, start=1):
                handle.write(
                    b"From %d@xxx Thu Jan 01 00:00:00 +0000 2026\r\n"
                    b"X-GM-THRID: %d\r\nX-Gmail-Labels: Caixa de Entrada\r\n" % (number, number)
                )
                handle.write(takeout_text + b"\r\n")
                (messages / f"{number}.eml").write_bytes(gog_raw)
                index.append(
                    {"id": format(number, "x"), "threadId": format(number, "x"), "internalDate": 1,
                     "labelIds": ["INBOX"], "eml": f"gmail/acct/messages/{number}.eml"}
                )
        (messages / "index.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in index), encoding="utf-8"
        )
        self.assertEqual(self.mbox(takeout).n_new, len(cases))
        return export

    def test_hash_mechanisms_between_takeout_and_gog(self) -> None:
        head = b"From: Ada Fixture <ada@example.com>\r\nMessage-ID: <%d@example.com>\r\nSubject: s\r\n\r\n"
        state = b"X-Gm-Message-State: c3ludGhldGlj\r\n"
        cases = {
            "same bytes": (head % 1 + b"body\r\n", head % 1 + b"body\r\n"),
            "an original LF line": (head % 2 + b"one\ntwo\r\n", head % 2 + b"one\ntwo\r\n"),
            "a Google header on both sides": (state + head % 3 + b"body\r\n", state + head % 3 + b"body\r\n"),
            "mboxrd escaping": (head % 4 + b">>From x\r\n", head % 4 + b">From x\r\n"),
            "mboxo escaping": (head % 5 + b">From x\r\n", head % 5 + b">From x\r\n"),
        }
        export = self.pair(list(cases.values()))
        result = self.gog(export)
        self.assertEqual((result.n_new, result.n_dup, result.n_reconciled, result.n_error), (0, 2, 3, 0))
        detail = json.loads(
            self.conn.execute("SELECT detail_json FROM ledger ORDER BY seq DESC").fetchone()[0]
        )
        self.assertEqual([item["gmail_message_id"] for item in detail["reconciled"]], ["2", "3", "5"])

    def test_gog_reimport_counts_the_same_rows_as_reconciled_again(self) -> None:
        self.mbox()
        first = self.gog()
        digest = ordered_row_dump_hash(self.conn)
        kept = self.row(HTML)["raw_sha256"]
        again = self.gog()
        self.assertEqual((first.n_dup, first.n_reconciled), (1, 1))
        self.assertEqual((again.n_new, again.n_dup, again.n_reconciled, again.n_error), (0, 1, 1, 0))
        self.assertEqual(self.row(HTML)["raw_sha256"], kept)
        self.assertTrue(self.row(HTML)["raw_path"].startswith("mbox:"))
        self.assertEqual(ordered_row_dump_hash(self.conn), digest)

    def test_mbox_after_gog_leaves_api_rows_alone(self) -> None:
        self.gog()
        before = (self.labels_of(PLAIN), self.labels_of(HTML), self.row(HTML)["raw_path"])
        result = self.mbox()
        self.assertEqual((result.n_new, result.n_dup, result.n_error), (3, 2, 0))
        self.assertEqual((self.labels_of(PLAIN), self.labels_of(HTML), self.row(HTML)["raw_path"]), before)

    def test_rfc822_row_is_not_merged_with_an_api_row(self) -> None:
        self.mbox()
        export = self.home / "same-message-id"
        messages = export / "gmail/acct/messages"
        messages.mkdir(parents=True)
        (export / "gmail/acct/labels.json").write_text("[]", encoding="utf-8")
        (messages / "x.eml").write_bytes(
            b"From: Dee Fixture <dee@example.com>\nMessage-ID: <mbox-noid@example.com>\n\nApi copy.\n"
        )
        (messages / "index.jsonl").write_text(
            json.dumps({"id": "abc123", "threadId": "abc123", "internalDate": 1, "eml": "gmail/acct/messages/x.eml"})
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(self.gog(export).n_new, 1)
        rows = self.conn.execute(
            "SELECT gmail_message_id FROM messages WHERE rfc822_message_id = '<mbox-noid@example.com>' ORDER BY 1"
        ).fetchall()
        self.assertEqual(
            [row[0] for row in rows],
            ["abc123", "rfc822:" + hashlib.sha256(b"mbox-noid@example.com").hexdigest()],
        )

    def test_import_streams_a_large_file(self) -> None:
        big = self.home / "big.mbox"
        line = b"streamed synthetic body line padding padding padding padding padding\n"
        body = line * 1500
        with big.open("wb") as handle:
            for number in range(1, 301):
                handle.write(
                    b"From %d@xxx Thu Jan 01 00:00:00 +0000 2026\n"
                    b"From: Ada Fixture <ada@example.com>\nSubject: big %d\n"
                    b"Date: Thu, 01 Jan 2026 00:00:00 +0000\n\n" % (number, number)
                )
                handle.write(body)
                handle.write(b"\n")
        size = big.stat().st_size
        self.assertGreater(size, 30_000_000)
        tracemalloc.start()
        try:
            result = self.mbox(big)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((result.n_new, result.n_refused), (300, 0))
        self.assertLess(peak, 8 * 1024 * 1024)


RSS_DRIVER = """
import json, resource, sys
from pathlib import Path
from mailcrawl.ingest import import_mbox
from mailcrawl.store import connect, init_account
home, mbox = Path(sys.argv[1]), Path(sys.argv[2])
db = home / "personal.db"
conn = connect(db if db.exists() else init_account(home, "personal", "ada@example.com"))
result = import_mbox(conn, alias="personal", mbox_path=mbox, labels_path=None,
                     taxonomy_path=None, account_email="ada@example.com")
status = Path("/proc/self/status")
if status.exists():
    # ru_maxrss survives fork and exec on Linux, so it would report the test runner's own peak.
    kib = next(line.split()[1] for line in status.read_text().splitlines() if line.startswith("VmHWM:"))
    peak = int(kib) * 1024
else:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"n_new": result.n_new, "n_dup": result.n_dup, "peak": peak}))
"""


def write_attachment_mbox(path: Path, sizes_mb: list[int]) -> int:
    """Return the largest message in bytes. Payloads are random, so nothing compresses them."""
    largest = 0
    with path.open("wb") as handle:
        for number, size in enumerate(sizes_mb, start=1):
            head = (
                b"From %d@xxx Thu Jan 01 00:00:00 +0000 2026\r\n"
                b"X-GM-THRID: %d\r\nX-Gmail-Labels: Caixa de Entrada\r\n"
                b"From: Ada Fixture <ada@example.com>\r\nSubject: anexo %d\r\n"
                b"Message-ID: <rss-%d@example.com>\r\nDate: Thu, 01 Jan 2026 00:00:00 +0000\r\n"
                b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
                b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nrsstoken\r\n"
                b"--b\r\nContent-Type: application/octet-stream; name=a.bin\r\n"
                b"Content-Disposition: attachment; filename=a.bin\r\n"
                b"Content-Transfer-Encoding: base64\r\n\r\n" % (number, number, number, number)
            )
            payload = base64.encodebytes(os.urandom(size * 1024 * 1024)).replace(b"\n", b"\r\n")
            handle.write(head)
            handle.write(payload)
            handle.write(b"--b--\r\n\r\n")
            largest = max(largest, len(head) + len(payload) + 9)
    return largest


class MboxMemoryTest(unittest.TestCase):
    def peak_rss(self, mbox: Path, home: Path | None = None) -> tuple[int, int]:
        found = self.run_driver(mbox, home)
        return found["n_new"], found["peak"]

    def run_driver(self, mbox: Path, home: Path | None = None) -> dict[str, int]:
        with tempfile.TemporaryDirectory() as scratch:
            done = subprocess.run(
                [sys.executable, "-c", RSS_DRIVER, str(home or scratch), str(mbox)],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                text=True,
                capture_output=True,
                check=True,
            )
        return json.loads(done.stdout)

    def test_reimport_peak_rss_stays_below_the_first_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "attachments.mbox"
            home = Path(tmp) / "home"
            write_attachment_mbox(big, [24, 4, 4, 4, 4, 4, 4])
            base = self.run_driver(TAKEOUT)["peak"]
            first = self.run_driver(big, home)
            again = self.run_driver(big, home)
        self.assertEqual((first["n_new"], again["n_new"], again["n_dup"]), (7, 0, 7))
        print(f"\nmbox reimport RSS: base {base >> 20} MB, first {first['peak'] >> 20} MB, "
              f"reimport {again['peak'] >> 20} MB", file=sys.stderr)
        self.assertLess(again["peak"] - base, (first["peak"] - base) // 4)

    def test_peak_rss_stays_near_the_largest_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "attachments.mbox"
            largest = write_attachment_mbox(big, [24, 4, 4, 4, 4, 4, 4])
            n_new, base = self.peak_rss(TAKEOUT)
            self.assertEqual(n_new, 5)
            n_new, peak = self.peak_rss(big)
        self.assertEqual(n_new, 7)
        growth = peak - base
        print(f"\nmbox RSS: base {base >> 20} MB, peak {peak >> 20} MB, largest message {largest >> 20} MB",
              file=sys.stderr)
        self.assertLess(growth, 4 * largest)


class MboxParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def items(self, text: bytes) -> list[object]:
        path = self.dir / "one.mbox"
        path.write_bytes(text)
        return list(MboxSource(path, {}).items())

    def test_gmail_hex_falls_back_on_odd_values(self) -> None:
        self.assertEqual(gmail_hex("1767225600000000001", decimal_only=True), "18867251edfa0001")
        self.assertEqual(gmail_hex("18867251EDFA0001", decimal_only=False), "18867251edfa0001")
        self.assertEqual(gmail_hex("0x00ff", decimal_only=False), "ff")
        self.assertIsNone(gmail_hex("18867251edfa0001", decimal_only=True))
        self.assertIsNone(gmail_hex("0", decimal_only=True))
        self.assertIsNone(gmail_hex(str(1 << 64), decimal_only=True))
        self.assertIsNone(gmail_hex("not-a-number", decimal_only=False))
        self.assertIsNone(gmail_hex("", decimal_only=False))
        self.assertIsNone(gmail_hex("9" * 5000, decimal_only=True))

    def test_label_names_and_system_table(self) -> None:
        self.assertEqual(
            label_names('Inbox, "Projetos, Exemplo",Category Updates,,'),
            ["Inbox", "Projetos, Exemplo", "Category Updates"],
        )
        self.assertEqual(system_label("CAIXA DE ENTRADA"), "INBOX")
        self.assertEqual(system_label("Categoria Promoções"), "CATEGORY_PROMOTIONS")
        self.assertEqual(system_label("CATEGORY_FORUMS"), "CATEGORY_FORUMS")
        self.assertIsNone(system_label("Projetos"))

    def test_xxx_separator_and_x_gmail_message_id(self) -> None:
        found = self.items(
            b"From 1767225600000000001@xxx Thu Jan 01 00:00:00 +0000 2026\n"
            b"Subject: one\n\nbody\n\n"
            b"From 5@xxx Thu Jan 01 00:00:00 +0000 2026\n"
            b"X-Gmail-Message-ID: 18867251edfa0002\nX-Gmail-Labels: =?utf-8?q?N=C3=A3o_lidas?=\n"
            b"Subject: two\n\nbody\n"
        )
        messages = [item for item in found if isinstance(item, MessageRec)]
        self.assertEqual([m.gmail_message_id for m in messages], ["18867251edfa0001", "18867251edfa0002"])
        self.assertEqual(messages[1].label_ids, ("UNREAD",))

    def test_observed_pt_system_labels(self) -> None:
        observed = {
            "Caixa de Entrada": "INBOX",
            "Categoria Atualizações": "CATEGORY_UPDATES",
            "Categoria Pessoal": "CATEGORY_PERSONAL",
            "Categoria Promoções": "CATEGORY_PROMOTIONS",
            "Importante": "IMPORTANT",
            "Não lidas": "UNREAD",
            "Rascunhos": "DRAFT",
            "Spam": "SPAM",
        }
        not_observed = {
            "Enviados": "SENT",
            "Com estrela": "STARRED",
            "Lixo": "TRASH",
            "Categoria Social": "CATEGORY_SOCIAL",
            "Categoria Fóruns": "CATEGORY_FORUMS",
        }
        for name, label_id in {**observed, **not_observed}.items():
            self.assertEqual(system_label(name), label_id, name)
            self.assertEqual(system_label(unicodedata.normalize("NFD", name)), label_id, name)

    def test_line_endings_and_from_escaping_do_not_change_hash_or_body(self) -> None:
        lines = [
            b"From 1767225600000000001@xxx Thu Jan 01 00:00:00 +0000 2026",
            b"Message-ID: <endings@example.com>",
            b"Subject: endings",
            b"Content-Type: text/plain; charset=utf-8",
            b"",
            b"First line.",
            b">From the archive.",
            b">>From quoted twice.",
            b"-- ",
            b"Sig",
        ]
        variants = {
            "lf": b"\n".join(lines) + b"\n",
            "crlf": b"\r\n".join(lines) + b"\r\n",
            "mixed": b"".join(line + (b"\n" if i % 3 else b"\r\n") for i, line in enumerate(lines)),
        }
        records = {}
        for name, text in variants.items():
            (message,) = self.items(text)
            assert isinstance(message, MessageRec)
            records[name] = message
        expected_raw = b"\r\n".join(
            [line.replace(b">From", b"From", 1) for line in lines[1:]]
        ) + b"\r\n"
        for name, message in records.items():
            self.assertEqual(message.raw_sha256, hashlib.sha256(expected_raw).hexdigest(), name)
            self.assertEqual(
                message.body_text,
                "First line.\nFrom the archive.\n>From quoted twice.\n-- \nSig\n",
                name,
            )
            self.assertEqual(clean_body(message.body_text or ""), "First line.\nFrom the archive.", name)

    def test_x_gmail_original_message_id_is_not_an_id(self) -> None:
        (refused,) = self.items(
            b"From nobody Thu Jan 01 00:00:00 2026\r\n"
            b"X-Gmail-Original-Message-ID: <only-original@example.com>\r\n"
            b"Subject: no id\r\n\r\nbody\r\n"
        )
        self.assertIsInstance(refused, Refused)
        (message,) = self.items(
            b"From nobody Thu Jan 01 00:00:00 2026\r\n"
            b"X-Gmail-Original-Message-ID: <original@example.com>\r\n"
            b"Message-ID: <rfc@example.com>\r\n"
            b"Subject: id\r\n\r\nbody\r\n"
        )
        assert isinstance(message, MessageRec)
        self.assertEqual(message.gmail_message_id, "rfc822:" + hashlib.sha256(b"rfc@example.com").hexdigest())

    def test_unparseable_values_never_raise(self) -> None:
        found = self.items(
            b"From \n\xff\xfe: \x00\nX-GM-MSGID:\nX-GM-THRID: -1\n\n\n"
            b"From 7@xxx\nMessage-ID: <ok@example.com>\nDate: nonsense\n\nbody\n"
        )
        self.assertIsInstance(found[0], Refused)
        last = found[-1]
        self.assertIsInstance(last, MessageRec)
        self.assertEqual((last.gmail_message_id, last.internal_date), ("7", 0))


class MboxCliTest(unittest.TestCase):
    def test_cli_alias_synonym_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            init = run_mailcrawl(home, "--json", "init", "--account", "pessoal", "--email", "ada@example.com")
            self.assertEqual(init.returncode, 0, init.stderr)
            imported = run_mailcrawl(home, "--json", "import", "mbox", str(TAKEOUT), "--alias", "pessoal")
            self.assertEqual(imported.returncode, 0, imported.stderr)
            doc = json.loads(imported.stdout)
            self.assertEqual(doc["account"], "personal")
            self.assertEqual((doc["n_seen"], doc["n_new"], doc["n_refused"]), (6, 5, 1))
            again = run_mailcrawl(home, "--json", "import", "mbox", str(TAKEOUT), "--account", "personal")
            self.assertEqual(json.loads(again.stdout)["n_dup"], 5)
            not_mbox = home / "note.txt"
            not_mbox.write_text("hello\n", encoding="utf-8")
            bad = run_mailcrawl(home, "--json", "import", "mbox", str(not_mbox), "--alias", "personal")
            self.assertEqual(bad.returncode, 3)
            self.assertEqual(json.loads(bad.stdout)["error"], "unsupported_schema")
            missing = run_mailcrawl(home, "--json", "import", "mbox", str(home / "nope.mbox"), "--alias", "personal")
            self.assertEqual(missing.returncode, 1)
            self.assertEqual(json.loads(missing.stdout)["error"], "missing_file")
            self.assertNotIn("Traceback", bad.stderr + missing.stderr)


if __name__ == "__main__":
    unittest.main()
