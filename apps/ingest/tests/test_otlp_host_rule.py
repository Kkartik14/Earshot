"""The loopback-Host rule and API keys, on every ``/v1`` route (AC5.9).

The ``Host`` check stops DNS rebinding against a server that serves without a
credential: a page that rebinds its own name to 127.0.0.1 makes the browser send
``Host: attacker.example`` with no credential (and no cookie for Earshot's host).
So the check applies only where anonymous access is possible, and a request that
carries a valid project API key, or the operator token (``EARSHOT_TOKEN``), is
exempt under ``trust_local_network``: a rebinding page cannot send a credential it
does not have. That is what lets a bot in another container export to
``http://earshot:4319``.

``behind_tls_proxy`` always requires a credential, so the check does not apply
there, whatever the listener address: a proxy forwards the public ``Host``.

Every cell is a real request to a real server; an accepted export is checked by
the project its span landed in.
"""

from __future__ import annotations

import itertools

import pytest

from earshot.api import ApiConfig, create_app
from otlp_support import keyed_request, post_otlp, request, serve
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration

LOOPBACK = "127.0.0.1:4319"
CONTAINER = "earshot:4319"
ACCEPTED_DEFAULT = (200, "default")
ACCEPTED_KEY = (200, "support")
UNAUTHORIZED = (401, "EARSHOT_UNAUTHORIZED")
UNTRUSTED_HOST = (400, "EARSHOT_UNTRUSTED_HOST")

OPERATOR_TOKEN = "operator-secret"
SETTINGS = {
    "trust_local_network (0.0.0.0)": ApiConfig(host="0.0.0.0", trust_local_network=True),
    "behind_tls_proxy (0.0.0.0)": ApiConfig(host="0.0.0.0", behind_tls_proxy=True),
    "behind_tls_proxy (127.0.0.1)": ApiConfig(host="127.0.0.1", behind_tls_proxy=True),
    "neither (127.0.0.1)": ApiConfig(host="127.0.0.1"),
    "trust_local_network + EARSHOT_TOKEN (0.0.0.0)": ApiConfig(
        host="0.0.0.0", trust_local_network=True, token=OPERATOR_TOKEN
    ),
    "behind_tls_proxy + EARSHOT_TOKEN (127.0.0.1)": ApiConfig(
        host="127.0.0.1", behind_tls_proxy=True, token=OPERATOR_TOKEN
    ),
    "EARSHOT_TOKEN only (127.0.0.1)": ApiConfig(host="127.0.0.1", token=OPERATOR_TOKEN),
}

# setting -> credential -> (Host: 127.0.0.1:4319, Host: earshot:4319); the docs copy it.
# The operator-token rows exist only where a token is configured.
MATRIX = {
    "trust_local_network (0.0.0.0)": {
        "none": (ACCEPTED_DEFAULT, UNTRUSTED_HOST),
        "valid key": (ACCEPTED_KEY, ACCEPTED_KEY),
        "invalid key": (UNAUTHORIZED, UNTRUSTED_HOST),
    },
    "behind_tls_proxy (0.0.0.0)": {
        "none": (UNAUTHORIZED, UNAUTHORIZED),
        "valid key": (ACCEPTED_KEY, ACCEPTED_KEY),
        "invalid key": (UNAUTHORIZED, UNAUTHORIZED),
    },
    "behind_tls_proxy (127.0.0.1)": {
        "none": (UNAUTHORIZED, UNAUTHORIZED),
        "valid key": (ACCEPTED_KEY, ACCEPTED_KEY),
        "invalid key": (UNAUTHORIZED, UNAUTHORIZED),
    },
    "neither (127.0.0.1)": {
        "none": (ACCEPTED_DEFAULT, UNTRUSTED_HOST),
        "valid key": (ACCEPTED_KEY, UNTRUSTED_HOST),
        "invalid key": (UNAUTHORIZED, UNTRUSTED_HOST),
    },
    "trust_local_network + EARSHOT_TOKEN (0.0.0.0)": {
        "none": (UNAUTHORIZED, UNTRUSTED_HOST),
        "valid key": (ACCEPTED_KEY, ACCEPTED_KEY),
        "invalid key": (UNAUTHORIZED, UNTRUSTED_HOST),
        "operator token": (ACCEPTED_DEFAULT, ACCEPTED_DEFAULT),
        "invalid token": (UNAUTHORIZED, UNTRUSTED_HOST),
    },
    "behind_tls_proxy + EARSHOT_TOKEN (127.0.0.1)": {
        "none": (UNAUTHORIZED, UNAUTHORIZED),
        "valid key": (ACCEPTED_KEY, ACCEPTED_KEY),
        "invalid key": (UNAUTHORIZED, UNAUTHORIZED),
        "operator token": (ACCEPTED_DEFAULT, ACCEPTED_DEFAULT),
        "invalid token": (UNAUTHORIZED, UNAUTHORIZED),
    },
    # A configured token makes every request need a credential: no Host check.
    "EARSHOT_TOKEN only (127.0.0.1)": {
        "none": (UNAUTHORIZED, UNAUTHORIZED),
        "valid key": (ACCEPTED_KEY, ACCEPTED_KEY),
        "invalid key": (UNAUTHORIZED, UNAUTHORIZED),
        "operator token": (ACCEPTED_DEFAULT, ACCEPTED_DEFAULT),
        "invalid token": (UNAUTHORIZED, UNAUTHORIZED),
    },
}


def _invalid(credential: str) -> str:
    """The same key ID with a different secret: well formed, but not a valid key."""

    return credential[:-1] + ("A" if credential[-1] != "A" else "B")


@pytest.mark.parametrize("setting", list(SETTINGS))
def test_the_host_rule_matrix_for_otlp_export(tmp_path, setting) -> None:
    store = make_store(tmp_path)
    store.create_project("support", display_name="Support")
    valid = store.issue_api_key("support", label="bot").credential
    credentials = {
        "none": None,
        "valid key": valid,
        "invalid key": _invalid(valid),
        "operator token": OPERATOR_TOKEN,
        "invalid token": _invalid(OPERATOR_TOKEN),
    }
    observed: dict[str, list[tuple[int, str]]] = {}
    traces = itertools.count(1)

    with serve(create_app(store=store, config=SETTINGS[setting])) as port:
        for credential in MATRIX[setting]:
            for host in (LOOPBACK, CONTAINER):
                trace = next(traces)
                reply = post_otlp(
                    port,
                    keyed_request(f"call-{trace}", trace, 1),
                    credential=credentials[credential],
                    host=host,
                )
                if reply.status == 200:
                    landed = catalog_rows(
                        tmp_path,
                        "SELECT project_id FROM spans WHERE trace_id = ?",
                        (trace.to_bytes(16, "big"),),
                    )
                    outcome = (200, landed[0][0] if len(landed) == 1 else f"{len(landed)} rows")
                else:
                    outcome = (reply.status, reply.problem_code())
                observed.setdefault(credential, []).append(outcome)

    assert {key: tuple(value) for key, value in observed.items()} == MATRIX[setting]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/incidents", None),
        ("GET", "/v1/otlp/stats", None),
        ("POST", "/v1/calls:resolve", b'{"session_key": "call-1"}'),
        ("GET", "/v1/live/sessions", None),
    ],
)
def test_the_api_key_exemption_applies_to_every_v1_route(tmp_path, method, path, body) -> None:
    store = make_store(tmp_path)
    valid = store.issue_api_key("default", label="bot").credential
    config = ApiConfig(host="0.0.0.0", trust_local_network=True)
    headers = {"Content-Type": "application/json"}

    with serve(create_app(store=store, config=config)) as port:
        keyed = request(
            port,
            method,
            path,
            body,
            headers={**headers, "Authorization": f"Bearer {valid}"},
            host=CONTAINER,
        )
        anonymous = request(port, method, path, body, headers=headers, host=CONTAINER)

    assert keyed.status in {200, 404}, keyed.body  # resolve: no such call yet
    if keyed.status == 404:
        assert keyed.problem_code() == "EARSHOT_CALL_NOT_FOUND"
    assert (anonymous.status, anonymous.problem_code()) == UNTRUSTED_HOST


def test_the_operator_token_exemption_applies_to_every_v1_route_and_hooks_skip_the_rule(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    config = ApiConfig(host="0.0.0.0", trust_local_network=True, token=OPERATOR_TOKEN)
    token = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}

    with serve(create_app(store=store, config=config)) as port:
        routes = {
            path: request(port, "GET", path, headers=token, host=CONTAINER).status
            for path in ("/v1/incidents", "/v1/otlp/stats", "/v1/live/sessions")
        }
        hook = request(
            port, "POST", "/hooks/v1/connectors/missing", b"{}", headers=token, host=CONTAINER
        )

    assert routes == dict.fromkeys(routes, 200)
    # A hook carries its provider's credential, so the Host rule never applies to it
    # (test_hook_host_exemption): it reaches its connector, here an unknown one.
    assert (hook.status, hook.problem_code()) == (404, "EARSHOT_CONNECTOR_NOT_FOUND")
