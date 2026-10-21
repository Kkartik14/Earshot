"""A server that drops off the network frees its Postgres locks in seconds, not hours.

Both Earshot locks are session advisory locks: the server lock (one server per
database) and the write lock (one mutation at a time). Postgres keeps such a lock
until it notices that the session's client is gone. A client whose node was lost
or whose network was cut sends nothing, so Postgres notices only through TCP
keepalives, and the operating-system default starts probing after two hours. The
replacement server would crash-loop on ``EARSHOT_DATABASE_IN_USE`` (or wait on
the write lock) all that time.

Everything here is real: Postgres 17 and the server image on a Docker network.
The lock holder is frozen by disconnecting its container from that network: it
keeps its sockets but can no longer send or answer anything, as after a node
loss. A second server must then take the lock within 30 s.
"""

from __future__ import annotations

import base64
import secrets
import time

import pytest
from install_support import (
    DockerScope,
    docker_scope,
    published_port,
    quick_start_image,
    server_ready_inside,
    start_postgres,
    wait_for,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

TAKEOVER_BOUND_S = 30.0
SERVER_ENVIRONMENT = {"EARSHOT_HOST": "0.0.0.0", "EARSHOT_TRUST_LOCAL_NETWORK": "true"}
SERVER_SECRETS = ("EARSHOT_DATABASE_URL", "EARSHOT_CORRELATION_KEY", "EARSHOT_TOKEN")
WRITE_LOCK_HOLDER = """
import os, time
from earshot.storage_postgres import PostgresDatabase
database = PostgresDatabase(os.environ["EARSHOT_DATABASE_URL"], pool_size=2)
with database.write_lock():
    print("held", flush=True)
    time.sleep(3600)
"""


def _frozen_holder_setup(scope: DockerScope, *, publish: bool = False) -> tuple[str, str, str]:
    """The image, a network and Postgres on it; the server secrets in ``scope``."""

    image = quick_start_image()
    network = scope.network("freeze")
    postgres = start_postgres(scope, network, publish=publish)
    scope.secrets.update(
        {
            "EARSHOT_DATABASE_URL": (
                f"postgresql://earshot:{scope.secrets['POSTGRES_PASSWORD']}@postgres:5432/earshot"
            ),
            "EARSHOT_CORRELATION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
            "EARSHOT_TOKEN": secrets.token_hex(32),
        }
    )
    return image, network, postgres


def _start_server(scope: DockerScope, name: str, image: str, network: str) -> str:
    return scope.start(
        name,
        image,
        "serve",
        network=network,
        environment=SERVER_ENVIRONMENT,
        secret_names=SERVER_SECRETS,
    )


def _until_ready_or_exited(scope: DockerScope, container: str) -> bool:
    """Wait until the server is ready (True) or has exited (False)."""

    while True:
        if server_ready_inside(scope, container):
            return True
        status, _ = scope.state(container)
        if status == "exited":
            return False
        time.sleep(0.25)


def test_a_second_server_takes_the_server_lock_within_30_s_of_the_holder_freezing() -> None:
    with docker_scope() as scope:
        image, network, _ = _frozen_holder_setup(scope)
        holder = _start_server(scope, "holder", image, network)
        wait_for("the holder to be ready", lambda: server_ready_inside(scope, holder), timeout=60)

        # The lock is really held: while the holder runs, a second server is refused.
        refused = _start_server(scope, "refused", image, network)
        assert not _until_ready_or_exited(scope, refused), scope.logs(refused)
        assert scope.state(refused)[1] == 2
        assert "EARSHOT_DATABASE_IN_USE" in scope.logs(refused)
        scope.remove(refused)

        scope.docker("network", "disconnect", network, holder)
        frozen_at = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            second = _start_server(scope, f"second-{attempt}", image, network)
            if _until_ready_or_exited(scope, second):
                break
            assert "EARSHOT_DATABASE_IN_USE" in scope.logs(second)
            scope.remove(second)
            assert time.monotonic() - frozen_at < TAKEOVER_BOUND_S, (
                f"no second server took the lock within {TAKEOVER_BOUND_S} s ({attempt} attempts)"
            )
            time.sleep(1.0)
        elapsed = time.monotonic() - frozen_at

    assert elapsed <= TAKEOVER_BOUND_S, elapsed


def test_a_frozen_holder_of_the_write_lock_releases_it_within_30_s() -> None:
    from earshot.storage import StorageBusyError
    from earshot.storage_postgres import PostgresDatabase

    with docker_scope() as scope:
        image, network, postgres = _frozen_holder_setup(scope, publish=True)
        holder = scope.start(
            "holder",
            image,
            "-c",
            WRITE_LOCK_HOLDER,
            network=network,
            secret_names=["EARSHOT_DATABASE_URL"],
            entrypoint="python",
        )
        wait_for("the write lock to be held", lambda: "held" in scope.logs(holder), timeout=60)
        url = (
            f"postgresql://earshot:{scope.secrets['POSTGRES_PASSWORD']}@127.0.0.1:"
            f"{published_port(scope, postgres, 5432)}/earshot"
        )
        database = PostgresDatabase(url, pool_size=2)
        try:
            # The lock is really held: a one-second budget runs out.
            with (
                pytest.raises(StorageBusyError),
                database.write_lock(deadline=time.perf_counter() + 1.0),
            ):
                pass

            scope.docker("network", "disconnect", network, holder)
            frozen_at = time.monotonic()
            with database.write_lock(deadline=time.perf_counter() + TAKEOVER_BOUND_S):
                elapsed = time.monotonic() - frozen_at
        finally:
            database.close()

    assert elapsed <= TAKEOVER_BOUND_S, elapsed
