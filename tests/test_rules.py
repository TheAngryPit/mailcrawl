"""Rules 0–8 are deterministic, synthetic, and first-match."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from mailcrawl.cli import main
from mailcrawl.configfile import account_identity, write_account
from mailcrawl.rules import RULES_VERSION, apply_rules, load_rules, preview_rules
from mailcrawl.store import connect, init_account

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "testdata" / "rules" / "rules-v0.json"
NOW = 1_700_000_000
DAY = 86400


class RulesEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_account(Path(self.tmp.name), "vcl", "ada@example.com")
        self.conn = connect(self.db)
        self.config = load_rules(RULES)
        self.account = int(
            self.conn.execute("SELECT account_id FROM accounts").fetchone()[0]
        )
        self.n = 0

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_starred_forces_needs_review_without_the_body(self) -> None:
        thread = self._thread("secretbodyqx", labels=[("STARRED", "STARRED", None)])
        apply_rules(self.conn, now=NOW, config=self.config)
        row = self._current(thread)
        self.assertEqual(row["next_state"], "needs_review")
        self.assertEqual(row["rule_id"], "0")
        self.assertEqual(row["rules_version"], RULES_VERSION)
        evidence = json.loads(row["evidence"])
        blob = json.dumps(evidence)
        self.assertNotIn("secretbodyqx", blob)
        self.assertIn("STARRED", blob)

    def test_spam_and_trash_resolve_before_reply_or_action(self) -> None:
        spam_reply = self._thread("hello there", labels=[("SPAM", "SPAM", None)])
        trash_reply = self._thread("hello there", labels=[("TRASH", "TRASH", None)])
        spam_action = self._thread(
            "please send the file",
            labels=[("SPAM", "SPAM", None)],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        trash_action = self._thread(
            "please send the file",
            labels=[("TRASH", "TRASH", None)],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        spam_else = self._thread("note", labels=[("SPAM", "SPAM", None)], from_me=1)
        starred_spam = self._thread(
            "hello there",
            labels=[("STARRED", "STARRED", None), ("SPAM", "SPAM", None)],
        )
        apply_rules(self.conn, now=NOW, config=self.config)
        for thread in (spam_reply, trash_reply, spam_action, trash_action, spam_else):
            row = self._current(thread)
            self.assertEqual(row["rule_id"], "spam-trash")
            self.assertEqual(row["next_state"], "resolved")
            self.assertEqual(row["lane"], "screened_out")
            self.assertNotIn("hello there", row["evidence"])
            self.assertNotIn("please send the file", row["evidence"])
        starred = self._current(starred_spam)
        self.assertEqual(starred["rule_id"], "0")
        self.assertEqual(starred["next_state"], "needs_review")

    def test_important_falls_through_and_else_stays_needs_review(self) -> None:
        important = self._thread("hello there", labels=[("IMPORTANT", "IMPORTANT", None)])
        starred = self._thread("hello there", labels=[("STARRED", "STARRED", None)])
        plain = self._thread("hello there")
        fallthrough = self._thread("note", from_me=1)
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(important)["rule_id"], "5")
        self.assertEqual(self._current(important)["next_state"], "reply")
        self.assertNotIn("IMPORTANT", self._current(important)["evidence"])
        self.assertEqual(self._current(starred)["rule_id"], "0")
        self.assertEqual(self._current(starred)["next_state"], "needs_review")
        self.assertEqual(self._current(plain)["rule_id"], "5")
        self.assertEqual(self._current(fallthrough)["rule_id"], "else")
        self.assertEqual(self._current(fallthrough)["next_state"], "needs_review")

    def test_muted_thread_is_noise_and_finance_is_not(self) -> None:
        muted = self._thread("hello", mute=True)
        finance = self._thread(
            "hello",
            mute=True,
            labels=[("L1", "Example type/Receipt", "invoice_receipt")],
        )
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(muted)["next_state"], "noise")
        self.assertEqual(self._current(muted)["rule_id"], "1")
        self.assertEqual(self._current(muted)["lane"], "screened_out")
        self.assertEqual(self._current(finance)["next_state"], "reference")
        self.assertEqual(self._current(finance)["rule_id"], "3")

    def test_newsletter_noise_and_read_list(self) -> None:
        noise = self._thread(
            "bulletin",
            labels=[
                ("L2", "Example origin/Machine", "automated"),
                ("L3", "Example type/Bulletin", "newsletter"),
            ],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        listed = self._thread(
            "bulletin",
            labels=[
                ("L2", "Example origin/Machine", "automated"),
                ("L3", "Example type/Bulletin", "newsletter"),
            ],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
            sender_decision=("approved", "read", None),
        )
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(noise)["next_state"], "noise")
        self.assertEqual(self._current(noise)["rule_id"], "2")
        self.assertEqual(self._current(listed)["next_state"], "read_later")
        self.assertEqual(self._current(listed)["rule_id"], "2-read")
        apply_rules(self.conn, now=NOW + 30 * DAY, config=self.config)
        self.assertEqual(self._current(listed)["next_state"], "noise")
        self.assertEqual(self._current(listed)["rule_id"], "t-expiry")

    def test_account_security_age_and_human_reply_priority(self) -> None:
        fresh = self._thread(
            "check the login",
            when=NOW - DAY,
            labels=[("L6", "Example type/Account", "account_security")],
        )
        stale = self._thread(
            "check the login",
            when=NOW - 8 * DAY,
            labels=[("L6", "Example type/Account", "account_security")],
        )
        reply = self._thread("hello there", sender_decision=("approved", "imbox", 2))
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(fresh)["next_state"], "needs_review")
        self.assertEqual(self._current(fresh)["rule_id"], "4")
        self.assertEqual(self._current(stale)["next_state"], "reference")
        self.assertEqual(self._current(reply)["next_state"], "reply")
        self.assertEqual(self._current(reply)["priority"], 2)
        self.assertEqual(self._current(reply)["rule_id"], "5")

    def test_waiting_task_closing_and_transitions(self) -> None:
        waiting = self._thread("Can you confirm?", from_me=1)
        task = self._thread(
            "please send the file",
            labels=[("L8", "Example origin/Machine", "automated")],
        )
        closed = self._thread("Thanks, all set.", from_me=1)
        guarded = self._thread(
            "Thanks, all set.",
            from_me=1,
            labels=[("L9", "Example type/Agreement", "contract")],
        )
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(waiting)["next_state"], "waiting")
        self.assertEqual(self._current(waiting)["review_date"], NOW + 7 * DAY)
        self.assertEqual(self._current(task)["next_state"], "action")
        self.assertEqual(self._current(task)["rule_id"], "7")
        self.assertEqual(self._current(closed)["next_state"], "resolved")
        self.assertEqual(self._current(closed)["rule_id"], "8")
        self.assertEqual(self._current(guarded)["next_state"], "needs_review")
        self.assertEqual(self._current(guarded)["rule_id"], "else")

        self._message(waiting, "A new note.", NOW + 10, from_me=0)
        apply_rules(self.conn, now=NOW + 10, config=self.config)
        self.assertEqual(self._current(waiting)["next_state"], "reply")
        self.assertEqual(self._current(waiting)["rule_id"], "t-reply")

        overdue = self._thread("Could you look?", from_me=1)
        apply_rules(self.conn, now=NOW, config=self.config)
        apply_rules(self.conn, now=NOW + 7 * DAY, config=self.config)
        self.assertEqual(self._current(overdue)["next_state"], "needs_review")
        self.assertEqual(self._current(overdue)["rule_id"], "t-overdue")

    def test_second_run_does_not_add_a_decision(self) -> None:
        self._thread("hello there")
        apply_rules(self.conn, now=NOW, config=self.config)
        before = self.conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        apply_rules(self.conn, now=NOW, config=self.config)
        after = self.conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        self.assertEqual(before, after)
        report = apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(report.threads, 1)
        self.assertEqual(report.covered, 1)

    def test_rules_v2_keeps_machine_mail_and_owners_out_of_reply(self) -> None:
        both = self._thread(
            "hello there",
            labels=[
                ("Lh", "Example origin/Person", "human"),
                ("La", "Example origin/Machine", "automated"),
            ],
        )
        machine = self._thread("hello there", address="no-reply@example.com")
        transfer = self._thread("Your download is ready")
        invoice = self._thread("Your invoice is attached")
        security_list = self._thread(
            "bulletin",
            labels=[
                ("Ls", "Example type/Account", "account_security"),
                ("Ln", "Example type/Bulletin", "newsletter"),
                ("La", "Example origin/Machine", "automated"),
            ],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        listed_task = self._thread(
            "please send the file",
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        promo_task = self._thread(
            "please send the file",
            labels=[("CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS")],
        )
        draft = self._thread("Can you confirm?", from_me=1, labels=[("DRAFT", "DRAFT", None)])
        calendar = self._thread("Accepted this invitation")
        notes = self._thread(
            "weekly report",
            labels=[("La", "Example origin/Machine", "automated")],
        )
        human_report = self._thread("weekly report")
        short_close = self._thread("Thanks")
        long_close = self._thread("Thanks\n" + ("hello there " * 20))
        owner = self._thread("Can you confirm?", address="owner@example.com")
        to_self = self._thread(
            "note",
            from_me=1,
            to_hdr="Ada <ada@example.com>",
        )
        apply_rules(
            self.conn,
            now=NOW,
            config=self.config,
            owners=frozenset({"owner@example.com", "ada@example.com"}),
        )
        self.assertNotEqual(self._current(both)["rule_id"], "5")
        self.assertEqual(self._current(both)["rule_id"], "else")
        self.assertEqual(self._current(machine)["rule_id"], "else")
        self.assertEqual(self._current(transfer)["rule_id"], "auto-ref")
        self.assertEqual(self._current(transfer)["next_state"], "reference")
        self.assertEqual(self._current(transfer)["lane"], "paper_trail")
        self.assertNotIn("download", self._current(transfer)["evidence"])
        self.assertEqual(self._current(invoice)["rule_id"], "3")
        self.assertEqual(self._current(invoice)["lane"], "paper_trail")
        self.assertNotIn("invoice", self._current(invoice)["evidence"])
        self.assertEqual(self._current(security_list)["rule_id"], "2")
        self.assertNotEqual(self._current(listed_task)["rule_id"], "7")
        self.assertNotEqual(self._current(promo_task)["rule_id"], "7")
        self.assertEqual(self._current(draft)["rule_id"], "draft")
        self.assertEqual(self._current(draft)["next_state"], "resolved")
        self.assertEqual(self._current(draft)["lane"], "screened_out")
        self.assertEqual(self._current(calendar)["rule_id"], "housekeeping")
        self.assertEqual(self._current(calendar)["next_state"], "resolved")
        self.assertEqual(self._current(notes)["rule_id"], "auto-ref")
        self.assertEqual(self._current(human_report)["rule_id"], "5")
        self.assertEqual(self._current(short_close)["rule_id"], "8")
        self.assertEqual(self._current(short_close)["next_state"], "resolved")
        self.assertEqual(self._current(long_close)["rule_id"], "5")
        self.assertEqual(self._current(owner)["rule_id"], "6")
        self.assertEqual(self._current(owner)["next_state"], "waiting")
        self.assertEqual(self._current(to_self)["rule_id"], "housekeeping")
        self.assertNotEqual(self._current(to_self)["next_state"], "waiting")

    def test_rules_v3_uses_locale_phrases_and_tabs(self) -> None:
        notes = self._thread(
            "notas da reunião",
            labels=[("La", "Example origin/Machine", "automated")],
        )
        promo = self._thread(
            "hello there",
            labels=[
                ("Lh", "Example origin/Person", "human"),
                ("CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS"),
            ],
        )
        human_invoice = self._thread("hello there")
        self._message(human_invoice, "Your invoice is attached", NOW - 100, from_me=0)
        billing = self._thread("Payment failed for this order")
        auto_header = self._thread(
            "please send the file",
            headers=[("auto-submitted", "auto-generated")],
        )
        auto_subject = self._thread("hello there", subject="Resposta automática")
        mine = self._thread("Thanks", from_me=1)
        template = self._thread(
            "<table><tr><td>Hello there friend</td></tr><tr><td>Second row</td></tr></table>",
            from_me=1,
        )
        confirm = self._thread("Diz-me se chegou.", from_me=1)
        updates = self._thread(
            "A security alert was opened",
            labels=[("CATEGORY_UPDATES", "CATEGORY_UPDATES", "CATEGORY_UPDATES")],
        )
        listed = self._thread(
            "hello there",
            labels=[("Lh", "Example origin/Person", "human")],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        bulk = self._thread(
            "hello there",
            labels=[("Lh", "Example origin/Person", "human")],
            headers=[("precedence", "bulk")],
        )
        info = self._thread(
            "hello there",
            address="info@example.com",
            labels=[("Lh", "Example origin/Person", "human")],
        )
        newsletter = self._thread(
            "hello there",
            address="newsletter@example.com",
            labels=[("Lh", "Example origin/Person", "human")],
        )
        mail_domain = self._thread(
            "hello there",
            address="person@mail.example.com",
            labels=[("Lh", "Example origin/Person", "human")],
        )
        security_else = self._thread(
            "hello there",
            labels=[("Ls", "Example type/Account", "account_security")],
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        allow = self._thread("hello there", address="alerts@example.com")
        sales = self._thread("A special offer for you", address="ada.sales@example.com")
        self._thread("hello there", address="ada.known@example.com")
        repeat = self._thread("A special offer for you", address="ada.known@example.com")
        apply_rules(
            self.conn,
            now=NOW,
            config=self.config,
            security_senders=frozenset({"alerts@example.com"}),
        )
        self.assertEqual(self._current(notes)["rule_id"], "auto-ref")
        self.assertEqual(self._current(notes)["lane"], "paper_trail")
        self.assertEqual(self._current(promo)["rule_id"], "2")
        self.assertNotEqual(self._current(promo)["rule_id"], "5")
        self.assertEqual(self._current(human_invoice)["rule_id"], "5")
        self.assertEqual(self._current(billing)["rule_id"], "7")
        self.assertEqual(self._current(billing)["next_state"], "action")
        self.assertNotEqual(self._current(billing)["next_state"], "reference")
        self.assertEqual(self._current(auto_header)["rule_id"], "auto-reply")
        self.assertEqual(self._current(auto_header)["next_state"], "noise")
        self.assertEqual(self._current(auto_subject)["rule_id"], "auto-reply")
        self.assertEqual(self._current(mine)["rule_id"], "8")
        self.assertEqual(self._current(mine)["next_state"], "resolved")
        self.assertEqual(self._current(template)["rule_id"], "test-send")
        self.assertEqual(self._current(template)["next_state"], "resolved")
        self.assertEqual(self._current(confirm)["rule_id"], "6")
        self.assertEqual(self._current(confirm)["next_state"], "waiting")
        self.assertEqual(self._current(updates)["rule_id"], "4")
        self.assertNotEqual(self._current(listed)["rule_id"], "5")
        self.assertNotEqual(self._current(bulk)["rule_id"], "5")
        self.assertNotEqual(self._current(info)["rule_id"], "5")
        self.assertEqual(self._current(newsletter)["rule_id"], "2")
        self.assertNotEqual(self._current(mail_domain)["rule_id"], "5")
        self.assertEqual(self._current(security_else)["rule_id"], "4")
        self.assertNotEqual(self._current(security_else)["rule_id"], "else")
        self.assertEqual(self._current(allow)["rule_id"], "4")
        self.assertEqual(self._current(sales)["rule_id"], "screener")
        self.assertEqual(self._current(sales)["next_state"], "screener")
        self.assertEqual(self._current(repeat)["rule_id"], "screener")

    def test_rules_v4_unions_tabs_and_keeps_precedence(self) -> None:
        promo = self._thread(
            "hello there",
            labels=[
                ("Lh4", "Example origin/Person", "human"),
                ("Lc4", "Example type/Letter", "correspondence"),
                ("CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS"),
            ],
        )
        updates = self._thread(
            "hello there",
            labels=[
                ("La4", "Example origin/Machine", "automated"),
                ("CATEGORY_UPDATES", "CATEGORY_UPDATES", "CATEGORY_UPDATES"),
            ],
        )
        letter = self._thread(
            "hello there",
            labels=[
                ("Lc4", "Example type/Letter", "correspondence"),
                ("CATEGORY_UPDATES", "CATEGORY_UPDATES", "CATEGORY_UPDATES"),
            ],
        )
        tracked = self._thread("Can you confirm?", from_me=1)
        self._message(tracked, "viewed your file", NOW, from_me=0)
        files = self._thread("files (3) from Ada Example")
        arquivos = self._thread("arquivos (2) para Ada Example", address="transfer@example.com")
        html = (
            "<table><tr><td>Ada Example</td></tr>"
            "<tr><td>Second row</td></tr><tr><td>Third row</td></tr></table>"
        )
        signature = self._thread("Can you confirm this note?\n" + html, from_me=1)
        tester = self._thread(
            "Can you confirm this note?\n" + html,
            from_me=1,
            to_hdr="Tester <tester@example.com>",
        )
        promo_close = self._thread(
            "Thanks",
            labels=[("CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS")],
        )
        list_close = self._thread(
            "Thanks",
            headers=[("list-unsubscribe", "<https://example.com/unsub>")],
        )
        pitch = self._thread("A special offer for you", address="ada.pitch@example.com")
        pitch_sender = self._sender_of(pitch)
        self._message(
            pitch,
            "You are invited to the session",
            NOW - 10,
            from_me=0,
            sender_id=pitch_sender,
        )
        answered = self._thread(
            "A partnership proposal for you",
            address="ada.answered@example.com",
        )
        answered_sender = self._sender_of(answered)
        self._message(answered, "Noted for later.", NOW - 30, from_me=1)
        self._message(
            answered,
            "A partnership proposal for you",
            NOW - 10,
            from_me=0,
            sender_id=answered_sender,
        )
        invite = self._thread("Convite para o evento na sala", address="ada.invite@example.com")
        overdue = self._thread("This invoice is overdue")
        unpaid = self._thread("A fatura está por pagar")
        asked = self._thread(
            "Your invoice is attached",
            labels=[("Li4", "Example type/Receipt", "invoice_receipt")],
        )
        self._message(asked, "Can you confirm?", NOW, from_me=1)
        receipt = self._thread(
            "Your invoice is attached",
            address="no-reply@example.com",
            subject="Automatic reply",
        )
        task = self._thread(
            "É só enviar o ficheiro",
            labels=[("La4", "Example origin/Machine", "automated")],
        )
        stale = self._thread(
            "The session is happening now",
            labels=[("La4", "Example origin/Machine", "automated")],
        )
        decorrer = self._thread(
            "A sessão está a decorrer agora",
            labels=[("La4", "Example origin/Machine", "automated")],
        )
        recovery = self._thread("The recovery email validated yesterday")
        exported = self._thread("A exportação de dados está pronta")
        budget = self._thread("Budget alert for this workspace")
        handoff = self._thread("Segue o ficheiro em anexo.", from_me=1)
        handoff_q = self._thread("Segue o ficheiro em anexo. Can you confirm?", from_me=1)
        access = self._thread(
            "Ada Example is requesting access",
            labels=[("La4", "Example origin/Machine", "automated")],
        )
        apply_rules(
            self.conn,
            now=NOW,
            config=self.config,
            test_recipients=frozenset({"tester@example.com"}),
        )
        self.assertEqual(self._current(promo)["rule_id"], "2")
        self.assertEqual(self._current(updates)["rule_id"], "auto-ref")
        self.assertNotEqual(self._current(updates)["rule_id"], "else")
        self.assertEqual(self._current(letter)["rule_id"], "auto-ref")
        self.assertEqual(self._current(letter)["next_state"], "reference")
        self.assertEqual(self._current(tracked)["rule_id"], "6")
        self.assertEqual(self._current(tracked)["next_state"], "waiting")
        self.assertEqual(self._current(files)["rule_id"], "auto-ref")
        self.assertEqual(self._current(arquivos)["rule_id"], "auto-ref")
        self.assertEqual(self._current(signature)["rule_id"], "6")
        self.assertEqual(self._current(signature)["next_state"], "waiting")
        self.assertEqual(self._current(tester)["rule_id"], "test-send")
        self.assertEqual(self._current(tester)["next_state"], "resolved")
        self.assertEqual(self._current(promo_close)["rule_id"], "2")
        self.assertNotEqual(self._current(promo_close)["lane"], "imbox")
        self.assertNotEqual(self._current(list_close)["rule_id"], "8")
        self.assertEqual(self._current(pitch)["rule_id"], "screener")
        self.assertEqual(self._current(answered)["rule_id"], "5")
        self.assertEqual(self._current(invite)["rule_id"], "screener")
        self.assertEqual(self._current(overdue)["rule_id"], "7")
        self.assertEqual(self._current(overdue)["next_state"], "action")
        self.assertEqual(self._current(unpaid)["rule_id"], "7")
        self.assertEqual(self._current(asked)["rule_id"], "6")
        self.assertNotEqual(self._current(asked)["rule_id"], "3")
        self.assertEqual(self._current(receipt)["rule_id"], "3")
        self.assertNotEqual(self._current(receipt)["rule_id"], "auto-reply")
        self.assertEqual(self._current(task)["rule_id"], "7")
        self.assertEqual(self._current(stale)["rule_id"], "auto-ref")
        self.assertEqual(self._current(decorrer)["rule_id"], "auto-ref")
        self.assertEqual(self._current(recovery)["rule_id"], "4")
        self.assertEqual(self._current(exported)["rule_id"], "4")
        self.assertEqual(self._current(budget)["rule_id"], "7")
        self.assertEqual(self._current(handoff)["rule_id"], "handoff")
        self.assertEqual(self._current(handoff)["next_state"], "resolved")
        self.assertEqual(self._current(handoff_q)["rule_id"], "6")
        self.assertEqual(self._current(access)["rule_id"], "7")
        self.assertEqual(self._current(access)["next_state"], "action")

    def test_owner_domain_is_from_me_through_config(self) -> None:
        self._thread("Can you confirm?", address="owner@example.com")
        self.conn.commit()
        config = Path(self.tmp.name) / "config.toml"
        config.write_text(
            "\n".join(
                [
                    "[accounts.vcl]",
                    'email = "ada@example.com"',
                    "owners = []",
                    'domains = ["example.com"]',
                    'security_senders = ["alerts@example.com"]',
                    'test_recipients = ["tester@example.com"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        write_account(config, "vcl", "ada@example.com")
        kept = config.read_text(encoding="utf-8")
        self.assertIn('domains = ["example.com"]', kept)
        self.assertIn('test_recipients = ["tester@example.com"]', kept)
        code = main(
            [
                "--json",
                "--home",
                self.tmp.name,
                "--config",
                str(config),
                "dry-run",
                "--account",
                "vcl",
                "--now",
                str(NOW),
                "--rules",
                str(RULES),
            ]
        )
        self.assertEqual(code, 0)
        row = self.conn.execute(
            "SELECT rule_id FROM decisions WHERE superseded_by IS NULL"
        ).fetchone()
        self.assertIsNone(row)
        owners, domains, _security, _testers = account_identity(config, "vcl")
        preview = preview_rules(
            self.conn, now=NOW, config=self.config, owners=owners, domains=domains
        )
        self.assertEqual(preview[0].rule_id, "6")
        self.assertNotEqual(preview[0].rule_id, "5")

    def test_doubtful_line_is_not_a_question(self) -> None:
        thread = self._thread("On the meeting we decided.", from_me=1)
        apply_rules(self.conn, now=NOW, config=self.config)
        self.assertEqual(self._current(thread)["rule_id"], "else")

    def _sender_of(self, thread_pk: int) -> int:
        row = self.conn.execute(
            "SELECT sender_id FROM messages WHERE thread_pk = ? ORDER BY msg_pk LIMIT 1",
            (thread_pk,),
        ).fetchone()
        return int(row[0])

    def _current(self, thread_pk: int):
        return self.conn.execute(
            """
            SELECT next_state, rule_id, lane, priority, review_date, rules_version, evidence
            FROM decisions
            WHERE thread_pk = ? AND superseded_by IS NULL
            """,
            (thread_pk,),
        ).fetchone()

    def _thread(
        self,
        body: str,
        *,
        labels: list[tuple[str, str, str | None]] | None = None,
        headers: list[tuple[str, str]] | None = None,
        from_me: int = 0,
        when: int = NOW - DAY,
        sender_decision: tuple[str, str, int | None] | None = None,
        mute: bool = False,
        address: str | None = None,
        to_hdr: str | None = None,
        subject: str | None = None,
    ) -> int:
        self.n += 1
        sender_id = self._sender(address or f"bao{self.n}@example.com", sender_decision)
        thread_pk = self._insert_thread(f"thread-{self.n}")
        self._message(
            thread_pk,
            body,
            when,
            from_me=from_me,
            sender_id=sender_id,
            labels=labels,
            headers=headers,
            to_hdr=to_hdr,
            subject=subject,
        )
        if mute:
            self.conn.execute(
                "INSERT INTO thread_mutes(thread_pk, muted_at) VALUES (?, ?)",
                (thread_pk, when),
            )
        return thread_pk

    def _message(
        self,
        thread_pk: int,
        body: str,
        when: int,
        *,
        from_me: int,
        sender_id: int | None = None,
        labels: list[tuple[str, str, str | None]] | None = None,
        headers: list[tuple[str, str]] | None = None,
        to_hdr: str | None = None,
        subject: str | None = None,
    ) -> None:
        self.n += 1
        if sender_id is None:
            sender_id = self._sender(f"bao{self.n}@example.com", None)
        msg = self.conn.execute(
            """
            INSERT INTO messages(
              account_id, gmail_message_id, thread_pk, internal_date, sender_id,
              body_text, is_from_me, to_hdr, subject, raw_sha256, provenance_hash, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw', 'prov', ?)
            RETURNING msg_pk
            """,
            (
                self.account,
                f"msg-{self.n}",
                thread_pk,
                when,
                sender_id,
                body,
                from_me,
                to_hdr,
                subject,
                when,
            ),
        ).fetchone()[0]
        for label_id, name, key in labels or []:
            self.conn.execute(
                """
                INSERT INTO labels(
                  account_id, gmail_label_id, name, kind, catalogue_key, seen_at
                ) VALUES (?, ?, ?, 'user', ?, ?)
                ON CONFLICT(account_id, gmail_label_id) DO NOTHING
                """,
                (self.account, label_id, name, key, when),
            )
            self.conn.execute(
                """
                INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
                VALUES (?, ?, ?, ?)
                """,
                (msg, self.account, label_id, when),
            )
        for name, value in headers or []:
            self.conn.execute(
                "INSERT INTO headers(msg_pk, name, value) VALUES (?, ?, ?)",
                (msg, name, value),
            )

    def _insert_thread(self, gmail_id: str) -> int:
        return int(
            self.conn.execute(
                """
                INSERT INTO threads(account_id, gmail_thread_id)
                VALUES (?, ?)
                RETURNING thread_pk
                """,
                (self.account, gmail_id),
            ).fetchone()[0]
        )

    def _sender(
        self,
        address: str,
        decision: tuple[str, str, int | None] | None,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO senders(account_id, address_norm, domain)
            VALUES (?, ?, 'example.com')
            ON CONFLICT(account_id, address_norm) DO NOTHING
            RETURNING sender_id
            """,
            (self.account, address),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT sender_id FROM senders
                WHERE account_id = ? AND address_norm = ?
                """,
                (self.account, address),
            ).fetchone()
        sender_id = int(row[0])
        if decision is not None:
            status, route, priority = decision
            self.conn.execute(
                """
                INSERT INTO sender_decisions(sender_id, status, route, priority)
                VALUES (?, ?, ?, ?)
                """,
                (sender_id, status, route, priority),
            )
        return sender_id


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
