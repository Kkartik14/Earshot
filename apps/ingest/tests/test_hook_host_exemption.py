"""Connector hooks are exempt from the loopback-Host check; ``/v1`` is not (AC1.8).

A tunnel to a laptop (cloudflared, ngrok) forwards the public ``Host``. The Host
check stops DNS rebinding against routes that answer without a credential; a hook
carries its own provider credential (a signature or a shared secret), which a
rebinding page does not have. So ``/hooks/v1/`` is exempt, under every setting,
and every ``/v1`` route keeps the outcome ``test_otlp_host_rule`` pins for it.

Every cell is a real request to a real server.
"""

from __future__ import annotations

import pytest

from earshot.api import create_app
from otlp_support import request, serve
from store_support import make_store
from test_otlp_host_rule import MATRIX, SETTINGS

pytestmark = pytest.mark.integration

TUNNEL = "quiet-river-1234.trycloudflare.com"
LOOPBACK = "127.0.0.1:4319"
CONNECTOR_NOT_FOUND = (404, "EARSHOT_CONNECTOR_NOT_FOUND")


def _as_status(outcome: tuple[int, str]) -> tuple[int, str | None]:
    """A matrix outcome for an anonymous request as a status and problem code."""

    status, detail = outcome
    return (status, None) if status == 200 else (status, detail)


@pytest.mark.parametrize("setting", list(SETTINGS))
def test_hooks_pass_any_host_and_v1_routes_keep_the_host_rule(tmp_path, setting) -> None:
    store = make_store(tmp_path)
    store.issue_api_key("default", label="bot")  # remote settings refuse to start without one
    observed_hooks = []
    observed_v1 = []

    with serve(create_app(store=store, config=SETTINGS[setting])) as port:
        for host in (LOOPBACK, TUNNEL):
            hook = request(
                port,
                "POST",
                "/hooks/v1/connectors/connector_unknown_0001",
                b"{}",
                headers={"Content-Type": "application/json"},
                host=host,
            )
            observed_hooks.append((hook.status, hook.problem_code()))
            v1 = request(port, "GET", "/v1/otlp/stats", host=host)
            observed_v1.append((v1.status, None if v1.status == 200 else v1.problem_code()))

    assert observed_hooks == [CONNECTOR_NOT_FOUND, CONNECTOR_NOT_FOUND]
    # The anonymous column of the OTLP matrix: the tunnel Host stands in for any
    # non-loopback Host there.
    assert observed_v1 == [_as_status(outcome) for outcome in MATRIX[setting]["none"]]
