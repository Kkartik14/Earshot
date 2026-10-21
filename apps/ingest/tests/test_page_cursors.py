"""Crafted page cursors are refused with 400 ``EARSHOT_INVALID_CURSOR`` (round 1, B-3).

A cursor is opaque to clients, but it is client input: base64 of a JSON
``[time, ID]``. Whatever a client puts there, the list routes answer 400, never a
500 or a database error. Real HTTP on a real server, both dialects.
"""

from __future__ import annotations

import base64
import json

import pytest

from earshot.api import create_app
from otlp_support import request, serve
from store_support import make_store

pytestmark = pytest.mark.integration


def _cursor(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


CRAFTED = {
    "lone-surrogate-id": ["1", "\ud800"],
    "nul-in-id": ["1", "call_\x00"],
    "non-ascii-digit-time": ["\u0663", "call_x"],
    "fullwidth-digit-time": ["\uff11", "call_x"],
}


@pytest.mark.parametrize("route", ["/v1/calls", "/v1/incidents"])
@pytest.mark.parametrize("value", list(CRAFTED.values()), ids=list(CRAFTED))
def test_a_crafted_cursor_is_400_invalid_cursor(tmp_path, route, value) -> None:
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = request(port, "GET", f"{route}?cursor={_cursor(value)}")

    assert (reply.status, reply.problem_code()) == (400, "EARSHOT_INVALID_CURSOR")


@pytest.mark.parametrize("route", ["/v1/calls", "/v1/incidents"])
def test_a_well_formed_cursor_is_still_accepted(tmp_path, route) -> None:
    with serve(create_app(store=make_store(tmp_path))) as port:
        reply = request(port, "GET", f"{route}?cursor={_cursor(['1', 'call_x'])}")

    assert reply.status == 200
    assert reply.json()["items"] == []
