"""Synthetic regression checks for archive consumer boundaries."""

import contextlib
import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from mailcrawl import jev, review, vectors
from mailcrawl.cli import _write_reevaluate
from mailcrawl.output import terminal_text, spreadsheet_cell
from mailcrawl.paths import UsageError
from mailcrawl.report import _ExportRow, _write_decisions
from mailcrawl.runtime_limits import context_parts, response_bytes
from mailcrawl.textnorm import subject_norm


class RuntimeSecurityTest(unittest.TestCase):
    def test_output_boundaries_leave_plain_identifiers_unchanged(self):
        self.assertEqual(spreadsheet_cell('1768000000000001'), '1768000000000001')
        self.assertEqual(terminal_text('Ada Fixture'), 'Ada Fixture')
        for text in ['=1+1', ' +1', '\t@SUM(1)', '\r-1', '\x85=1']:
            self.assertEqual(spreadsheet_cell(text), "'" + text)
        self.assertEqual(terminal_text('\x1b]0;x\x07\r\n\x9b'), '\\x1b]0;x\\x07\\x0d\\x0a\\x9b')

    def test_both_csv_writers_neutralize_formulas_without_mutating_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = '=1+1'
            row = _ExportRow(raw, *['plain'] * 12)
            _write_decisions(root/'decisions.csv', [row])
            data = {'gmail_thread_id': raw, 'reason': 'ordinary'}
            _write_reevaluate(root/'reevaluate.csv', [data])
            for name in ['decisions.csv', 'reevaluate.csv']:
                with (root/name).open(newline='') as stream:
                    result = list(csv.DictReader(stream))[0]
                self.assertEqual(result['gmail_thread_id'], "'" + raw)
            self.assertEqual(row.gmail_thread_id, raw)
            self.assertEqual(data['gmail_thread_id'], raw)

    def test_inference_reader_never_reads_an_unbounded_body(self):
        class Source:
            def read(self, size=-1):
                self.size = size
                return b'x' * size
        source = Source()
        with self.assertRaises(UsageError):
            response_bytes(source, 8)
        self.assertEqual(source.size, 9)
        self.assertEqual(response_bytes(io.BytesIO(b'normal'), 8), b'normal')

    def test_response_deadline_stops_a_trickle_stream(self):
        class Trickle:
            calls = 0
            def read1(self, size):
                self.calls += 1
                return b"x"
            read = read1
        stream = Trickle()
        with patch('mailcrawl.runtime_limits.time.monotonic', side_effect=[0, 0, 31]):
            with self.assertRaisesRegex(UsageError, "time budget"):
                response_bytes(stream, 10, timeout=30)
        self.assertEqual(stream.calls, 1)

    def test_context_limit_counts_utf8_and_separators(self):
        self.assertEqual(context_parts(['ab', 'cd'], 5), 'ab\ncd')
        with self.assertRaises(UsageError):
            context_parts(['ab', 'cd'], 4)
        with self.assertRaises(UsageError):
            context_parts(['éé'], 3)

    def test_review_endpoint_transport_matrix(self):
        for url in ['http://remote.example.com/v1', 'http://192.0.2.1/v1']:
            with self.assertRaises(UsageError):
                review._endpoint(url, True)
        self.assertEqual(review._endpoint('http://127.0.0.1:8080/v1', False), 'http://127.0.0.1:8080/v1')
        self.assertEqual(review._endpoint('https://remote.example.com/v1', True), 'https://remote.example.com/v1')
        with self.assertRaises(UsageError):
            review._endpoint('https://remote.example.com/v1', False)

    def test_review_response_limit_before_parsing(self):
        stream = io.BytesIO(b'x' * 9)
        opener = MagicMock()
        opener.open.return_value = contextlib.closing(stream)
        with patch.object(review, 'enter_model'), patch.object(review, '_prompt', return_value='synthetic'), patch.object(review.urllib.request, 'build_opener', return_value=opener), patch.object(review, 'response_bytes', side_effect=lambda s: response_bytes(s, 8)):
            with self.assertRaises(UsageError):
                review._complete('http://127.0.0.1', 'fixture', None)

    def test_jev_success_and_error_bodies_are_capped(self):
        import urllib.error
        from email.message import Message
        for response in [contextlib.closing(io.BytesIO(b'x'*9)), urllib.error.HTTPError('https://example.com', 429, 'fixture', Message(), io.BytesIO(b'x'*9))]:
            opener = MagicMock()
            if isinstance(response, Exception):
                opener.open.side_effect = response
            else:
                opener.open.return_value = response
            with patch.object(jev, 'enter_model'), patch.object(jev, '_api_key', return_value='synthetic'), patch.object(jev.urllib.request, 'build_opener', return_value=opener), patch.object(jev, 'response_bytes', side_effect=lambda s: response_bytes(s, 8)):
                with self.assertRaises(UsageError):
                    jev.HttpJev().ask('normal', ())

    def test_jev_context_is_capped_before_full_sql_fetch(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('create table messages(thread_pk, body_text, internal_date, msg_pk)')
        conn.execute('insert into messages values(1, ?, 1, 1)', ('x'*(1024*1024+1),))
        with self.assertRaises(UsageError):
            jev._thread_state(conn, 1)
        conn.close()

    def test_thread_iterator_selects_only_requested_ids(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('create table messages(thread_pk, subject, body_text, internal_date, msg_pk)')
        conn.executemany('insert into messages values(?,?,?,?,?)', [(1,'one','first',1,1),(1,'one','second',2,2),(2,'two','x'*100,3,3)])
        self.assertEqual(list(vectors._threads(conn, thread_ids={1})), [(1,'one','first\nsecond')])
        self.assertEqual(list(vectors._threads(conn, include_text=False)), [(1,'',''),(2,'','')])
        conn.close()

    def test_truncate_scans_only_the_required_prefix(self):
        class NoSplit(str):
            def split(self, *args, **kwargs):
                raise AssertionError('whole text split')
        self.assertEqual(vectors.truncate_text(NoSplit('one two three'), 2), 'one two')
        self.assertEqual(vectors.truncate_text(NoSplit('x'*1000), 2), 'x'*8)
        self.assertEqual(vectors.truncate_text('small', 8), 'small')

    def test_image_query_never_opens_unselected_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'small').write_bytes(b'abc')
            (root/'large').write_bytes(b'x'*9)
            conn = sqlite3.connect(':memory:')
            conn.executescript('create table messages(msg_pk,thread_pk); create table attachments(att_pk,msg_pk,mime_type,is_inline,size_bytes,stored_path);')
            conn.executemany('insert into messages values(?,?)', [(1,1),(2,2)])
            conn.executemany('insert into attachments values(?,?,?,?,?,?)', [(1,1,'image/png',0,3,str(root/'small')),(2,2,'image/png',0,9,str(root/'large'))])
            with patch.object(vectors, 'MAX_IMAGE_BYTES', 4), patch.object(vectors, 'image_gate', return_value=(True,None)):
                kept = list(vectors._kept_images(conn, {1}))
                self.assertEqual([item.blob for item in kept], [b'abc'])
                with self.assertRaises(UsageError):
                    list(vectors._kept_images(conn, {2}))
            conn.close()

    def test_image_batch_is_bounded_even_for_repeated_small_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'small'
            path.write_bytes(b'abc')
            conn = sqlite3.connect(':memory:')
            conn.executescript('create table messages(msg_pk,thread_pk); create table attachments(att_pk,msg_pk,mime_type,is_inline,size_bytes,stored_path); insert into messages values(1,1);')
            conn.executemany('insert into attachments values(?,?,?,?,?,?)', [(i,1,'image/png',0,3,str(path)) for i in (1,2)])
            with patch.object(vectors, 'MAX_IMAGE_BATCH_BYTES', 5), patch.object(vectors, 'image_gate', return_value=(True,None)):
                source = vectors._kept_images(conn, {1})
                self.assertEqual(next(source).blob, b'abc')
                with self.assertRaises(UsageError):
                    next(source)
            conn.close()

    def test_over_limit_whitespace_does_not_hide_a_truncated_message(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('create table messages(thread_pk,subject,body_text,internal_date,msg_pk)')
        conn.execute('insert into messages values(1,?,?,1,1)', ('title',' '*9+'important'))
        with patch.object(vectors, 'MAX_CONTEXT_BYTES', 8):
            with self.assertRaises(UsageError):
                list(vectors._threads(conn))
        conn.close()

    def test_subject_prefixes_are_consumed_without_repeated_copy(self):
        self.assertEqual(subject_norm(' Re: Fwd: fw:  Hello  World '), 'Hello World')
        self.assertEqual(subject_norm('Re: '*10000+'hello'), 'hello')
        with self.assertRaises(UsageError):
            subject_norm('x'*65537)


if __name__ == '__main__':
    unittest.main()
