"""Fail-closed gate for an unverified export contract.

``mailcrawl.gog-export.v0`` names the synthetic fixture. It is not a map of
real gog field names. Every document fails closed until those names are verified.
"""

UNSUPPORTED_SCHEMA = "unsupported_schema"
CONTRACT_V0 = "mailcrawl.gog-export.v0"


def classify_export(document: object) -> str:
    """Return ``unsupported_schema`` for every document, including CONTRACT_V0."""
    _ = document
    return UNSUPPORTED_SCHEMA
