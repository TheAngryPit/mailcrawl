"""Harness queue and decide stay off the model path unless review run is configured."""

import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from mailcrawl.cli import main
from mailcrawl.configfile import local_llm, write_account
from mailcrawl.paths import UsageError
from mailcrawl.review import parse_agent_decision, review_queue, review_run
from mailcrawl.store import connect


class ReviewHarnessTest(unittest.TestCase):
    def test_queue_marks_text_untrusted_and_skips_an_image_only_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "personal.db"
            _seed(db)
            before = _snapshot(db)
            conn = connect(db)
            try:
                items = review_queue(conn, "personal", limit=None, max_tokens=2)
            finally:
                conn.close()
            self.assertEqual(_snapshot(db), before)
            by_id = {item.thread_id: item for item in items}
            self.assertEqual(set(by_id), {"thread-review", "thread-open", "thread-human"})
            self.assertEqual(by_id["thread-review"].state, "needs_review")
            self.assertEqual(by_id["thread-review"].rule, "else")
            self.assertEqual(by_id["thread-review"].untrusted_text, "aa bb")
            self.assertEqual(by_id["thread-review"].untrusted_images, ("scans/one.png",))
            self.assertEqual(by_id["thread-review"].jev[0]["question_id"], "reply-requested")
            self.assertTrue(by_id["thread-open"].state is None)
            document = by_id["thread-review"].document()
            self.assertEqual(document["untrusted"]["text"], "aa bb")
            self.assertEqual(document["untrusted"]["images"], ["scans/one.png"])
            _artifact("review-queue.json", {"items": [item.document() for item in items]})

    def test_decide_round_trip_leaves_a_human_row(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "personal.db"
            _seed(db)
            code, doc = _cli(
                [
                    "--json",
                    "--home",
                    str(home),
                    "decide",
                    "--account",
                    "personal",
                    "--thread",
                    "thread-open",
                    "--state",
                    "reply",
                    "--lane",
                    "imbox",
                    "--source-detail",
                    "harness-fixture",
                    "--reason",
                    "synthetic",
                    "--now",
                    "10",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(doc["decisions"][0]["origin"], "model")
            self.assertEqual(doc["decisions"][0]["state"], "reply")
            self.assertTrue(doc["decisions"][0]["changed"])
            code, again = _cli(
                [
                    "--json",
                    "--home",
                    str(home),
                    "decide",
                    "--account",
                    "personal",
                    "--thread",
                    "thread-open",
                    "--state",
                    "noise",
                    "--lane",
                    "feed",
                    "--source-detail",
                    "harness-fixture",
                    "--now",
                    "11",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(again["decisions"][0]["state"], "reply")
            self.assertFalse(again["decisions"][0]["changed"])
            conn = connect(db)
            try:
                self.assertEqual(_count(conn, "thread-open"), 1)
                code, blocked = _cli(
                    [
                        "--json",
                        "--home",
                        str(home),
                        "decide",
                        "--account",
                        "personal",
                        "--thread",
                        "thread-human",
                        "--state",
                        "noise",
                        "--lane",
                        "feed",
                        "--source-detail",
                        "harness-fixture",
                        "--now",
                        "12",
                    ]
                )
                self.assertEqual(code, 2)
                self.assertEqual(blocked["detail"], "a human decision stays")
                human = conn.execute(
                    """
                    SELECT origin, next_state FROM decisions
                    WHERE thread_pk = (
                      SELECT thread_pk FROM threads WHERE gmail_thread_id = 'thread-human'
                    )
                    """
                ).fetchone()
                self.assertEqual(tuple(human), ("human", "needs_review"))
                self.assertEqual(_count(conn, "thread-human"), 1)
                _artifact(
                    "review-decide.json",
                    {"again": again["decisions"][0], "human": list(human)},
                )
            finally:
                conn.close()

    def test_jsonl_supersedes_needs_review_once(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "personal.db"
            _seed(db)
            path = home / "decisions.jsonl"
            line = json.dumps(
                {
                    "lane": "imbox",
                    "source_detail": "harness-fixture",
                    "state": "reference",
                    "thread_id": "thread-review",
                }
            )
            path.write_text(line + "\n" + line + "\n", encoding="utf-8")
            code, doc = _cli(
                [
                    "--json",
                    "--home",
                    str(home),
                    "decide",
                    "--account",
                    "personal",
                    "--file",
                    str(path),
                    "--now",
                    "20",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual([row["changed"] for row in doc["decisions"]], [True, False])
            self.assertEqual(doc["decisions"][1]["state"], "reference")
            conn = connect(db)
            try:
                self.assertEqual(_count(conn, "thread-review"), 2)
                current = conn.execute(
                    """
                    SELECT origin, source_detail, superseded_by IS NULL
                    FROM decisions
                    WHERE thread_pk = (
                      SELECT thread_pk FROM threads WHERE gmail_thread_id = 'thread-review'
                    )
                    ORDER BY decision_id
                    """
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(tuple(current[0]), ("rule", "else", 0))
            self.assertEqual(tuple(current[1]), ("model", "harness-fixture", 1))

    def test_remote_url_is_refused_and_a_fake_local_model_can_decide(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "personal.db"
            _seed(db)
            conn = connect(db)
            try:
                with self.assertRaises(UsageError) as caught:
                    review_run(
                        conn,
                        "personal",
                        ("http://example.com/v1", "fixture-local", False),
                        limit=None,
                        max_tokens=8,
                        now=30,
                    )
                self.assertEqual(str(caught.exception), "local llm refuses a remote URL")
                self.assertEqual(_count(conn, "thread-open"), 0)
            finally:
                conn.close()
            seen: list[str] = []
            server = _server(seen)
            host, port = server.server_address[:2]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config = home / "config.toml"
                config.write_text(
                    "\n".join(
                        [
                            "[inference.llm]",
                            f'base_url = "http://{host}:{port}/v1"',
                            'model = "fixture-local"',
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(local_llm(config)[1], "fixture-local")
                write_account(config, "personal", "ada@example.com")
                self.assertEqual(local_llm(config)[0], f"http://{host}:{port}/v1")
                code, doc = _cli(
                    [
                        "--json",
                        "--home",
                        str(home),
                        "--config",
                        str(config),
                        "review",
                        "run",
                        "--account",
                        "personal",
                        "--now",
                        "40",
                    ]
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(code, 0, doc)
            self.assertEqual(
                [row["thread_id"] for row in doc["decisions"]],
                ["thread-review", "thread-open"],
            )
            self.assertEqual(seen, ["thread-review", "thread-open"])
            conn = connect(db)
            try:
                row = conn.execute(
                    """
                    SELECT origin, source_detail, next_state FROM decisions
                    WHERE thread_pk = (
                      SELECT thread_pk FROM threads WHERE gmail_thread_id = 'thread-open'
                    ) AND superseded_by IS NULL
                    """
                ).fetchone()
                human = conn.execute(
                    """
                    SELECT origin, next_state FROM decisions
                    WHERE thread_pk = (
                      SELECT thread_pk FROM threads WHERE gmail_thread_id = 'thread-human'
                    )
                    """
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(tuple(row), ("model", "fixture-local", "reply"))
            self.assertEqual(tuple(human), ("human", "needs_review"))
            _artifact(
                "review-local.json",
                {"decisions": doc["decisions"], "requests": seen},
            )

    def test_off_by_default_and_bad_state(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed(home / "personal.db")
            code, doc = _cli(
                [
                    "--json",
                    "--home",
                    str(home),
                    "review",
                    "run",
                    "--account",
                    "personal",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(doc["detail"], "local llm is off")
            with self.assertRaises(UsageError):
                parse_agent_decision(
                    {
                        "lane": "imbox",
                        "source_detail": "harness-fixture",
                        "state": "archive",
                        "thread_id": "thread-open",
                    }
                )


def _cli(argv: list[str]) -> tuple[int, dict[str, object]]:
    buf = StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    text = buf.getvalue()
    if not text:
        return code, {}
    return code, json.loads(text)


def _server(seen: list[str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
            document = json.loads(raw["messages"][0]["content"].split("\n")[-1])
            thread_id = str(document["thread_id"])
            if thread_id == "thread-human":
                content = json.dumps({"lane": "feed", "state": "noise"})
            else:
                seen.append(thread_id)
                content = json.dumps({"lane": "imbox", "reason": "synthetic", "state": "reply"})
            body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _seed(db: Path) -> None:
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO accounts(alias, email_hash, created_at) VALUES ('personal', 'abc', 1)"
        )
        account_id = int(conn.execute("SELECT account_id FROM accounts").fetchone()[0])
        rows = (
            (1, "thread-review", 0, "aa bb cc dd", "else", "rule", "rule"),
            (2, "thread-open", 0, "open body", None, None, None),
            (3, "thread-scan", 1, "scan", None, None, None),
            (4, "thread-human", 0, "human body", "0", "human", "human"),
        )
        for thread_pk, thread_id, image_only, body, rule_id, origin, source in rows:
            conn.execute(
                """
                INSERT INTO threads(
                  thread_pk, account_id, gmail_thread_id, msg_count, image_only
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (thread_pk, account_id, thread_id, image_only),
            )
            conn.execute(
                """
                INSERT INTO messages(
                  account_id, gmail_message_id, thread_pk, internal_date, subject,
                  body_text, raw_sha256, provenance_hash, imported_at
                ) VALUES (?, ?, ?, 1, 'Synthetic', ?, ?, ?, 1)
                """,
                (
                    account_id,
                    f"m-{thread_pk}",
                    thread_pk,
                    body,
                    f"sha-{thread_pk}",
                    f"prov-{thread_pk}",
                ),
            )
            if origin is None:
                continue
            conn.execute(
                """
                INSERT INTO decisions(
                  thread_pk, next_state, lane, source, origin, source_detail,
                  rule_id, rules_version, evidence, created_at
                ) VALUES (?, 'needs_review', 'imbox', ?, ?, ?, ?, 'rules-v4', '[]', 1)
                """,
                (thread_pk, source, origin, rule_id, rule_id),
            )
        msg_pk = int(
            conn.execute(
                "SELECT msg_pk FROM messages WHERE gmail_message_id = 'm-1'"
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO attachments(
              msg_pk, mime_type, size_bytes, is_inline, stored_path
            ) VALUES (?, 'image/png', 30000, 0, 'scans/one.png')
            """,
            (msg_pk,),
        )
        conn.execute(
            """
            INSERT INTO jev_answers(
              thread_pk, question_id, battery_version, model_id, kind, answer_json
            ) VALUES (1, 'reply-requested', 'battery-v1', 'fake-jev', 'noul', ?)
            """,
            (json.dumps({"noul": 0.1, "type": "noul"}),),
        )
        conn.commit()
    finally:
        conn.close()


def _count(conn, thread_id: str) -> int:
    row = conn.execute(
        """
        SELECT count(*) FROM decisions
        WHERE thread_pk = (SELECT thread_pk FROM threads WHERE gmail_thread_id = ?)
        """,
        (thread_id,),
    ).fetchone()
    return int(row[0])


def _snapshot(path: Path) -> bytes:
    conn = connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return path.read_bytes()


def _artifact(name: str, document: dict[str, object]) -> None:
    root = os.environ.get("PROOF_DIR")
    if not root:
        return
    dest = Path(root) / name
    dest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
