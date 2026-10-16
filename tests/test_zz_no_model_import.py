"""The suite must finish without loading the real embedding model."""

import sys
import unittest


class NoModelImportTest(unittest.TestCase):
    def test_sentence_transformers_stays_unimported(self) -> None:
        self.assertNotIn("sentence_transformers", sys.modules)
        self.assertNotIn("torch", sys.modules)
        self.assertNotIn("httpx", sys.modules)
        self.assertNotIn("requests", sys.modules)
        self.assertNotIn("aiohttp", sys.modules)
