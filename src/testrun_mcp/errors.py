"""Errors the server turns into messages an agent can act on."""

from __future__ import annotations


class TestrunError(Exception):
    """An expected failure; the message is shown to the client verbatim."""

    __test__ = False  # not a pytest test class
