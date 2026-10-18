# SPDX-License-Identifier: MIT
"""Cleaned body text drops clear quote and signature markers only."""

import os
import unittest

from mailcrawl.textnorm import clean_body


class CleanBodyTest(unittest.TestCase):
    def test_quote_line_is_cut_and_midline_marker_stays(self) -> None:
        self.assertEqual(
            clean_body("Keep this.\nA line with > inside stays.\n> quoted secretwordqx"),
            "Keep this.\nA line with > inside stays.",
        )

    def test_on_wrote_cuts_through_the_end(self) -> None:
        self.assertEqual(
            clean_body("Keep this.\nOn Monday Ada wrote:\nquoted tail"),
            "Keep this.",
        )

    def test_em_escreveu_cuts_through_the_end(self) -> None:
        self.assertEqual(
            clean_body("Fica isto.\nEm segunda a Ada escreveu:\ncitação"),
            "Fica isto.",
        )

    def test_original_message_cuts_through_the_end(self) -> None:
        self.assertEqual(
            clean_body("Fica isto.\n-----Original Message-----\nsecret"),
            "Fica isto.",
        )

    def test_outlook_from_sent_cuts_through_the_end(self) -> None:
        raw = "Fica isto.\nFrom: Ada <ada@example.com>\nSent: Monday\nsecret"
        self.assertEqual(clean_body(raw), "Fica isto.")

    def test_outlook_de_enviada_cuts_through_the_end(self) -> None:
        raw = "Fica isto.\nDe: Ada <ada@example.com>\nEnviada: segunda\nsecret"
        self.assertEqual(clean_body(raw), "Fica isto.")

    def test_signature_delimiter_cuts_and_bare_dashes_stay(self) -> None:
        self.assertEqual(clean_body("Fica isto.\n-- \nAda Fixture"), "Fica isto.")
        self.assertEqual(
            clean_body("Fica isto.\n--\nnot a delimiter"),
            "Fica isto.\n--\nnot a delimiter",
        )

    def test_doubtful_lines_stay(self) -> None:
        raw = (
            "On the meeting we decided.\n"
            "Em casa fica.\n"
            "From: the office we sent the file."
        )
        self.assertEqual(clean_body(raw), raw)

    def test_sent_header_nine_lines_later_stays(self) -> None:
        raw = "\n".join(
            [
                "Keep the ask.",
                "From: Ada <ada@example.com>",
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
                "7",
                "8",
                "Sent: later",
                "keep-after",
            ]
        )
        self.assertEqual(clean_body(raw), raw)

    def test_none_and_empty(self) -> None:
        self.assertIsNone(clean_body(None))
        self.assertEqual(clean_body(""), "")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
