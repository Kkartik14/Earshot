"""ASGI entry point for ``uvicorn apps.ingest.app:app``.

Configuration comes from the environment through ``earshot.server_config``, the
same parser and assembly ``earshot serve`` uses, so the two entry points serve the
same app for the same environment (``docs/configuration.md`` lists every
variable). ``uvicorn`` owns the listener: its ``--host``/``--port`` decide where it
binds, while ``EARSHOT_HOST`` describes that listener to the app's access rules.

The server lock is taken before the store opens: ``K_server`` with
``EARSHOT_DATABASE_URL`` set to a ``postgresql://`` URL, else ``.server.lock`` in
the SQLite data directory. A second server or a second worker
(``--workers 2``) is refused with ``EARSHOT_DATABASE_IN_USE``; an invalid
setting is refused with a message that names the variable. Both leave with
uvicorn's startup-failure status, which stops uvicorn's worker supervisor
instead of having it restart the refused worker forever.
"""

from __future__ import annotations

import sys

from earshot.server_config import ConfigurationError, load_server_settings, open_server
from earshot.storage import DatabaseInUseError

# uvicorn's ``STARTUP_FAILURE`` status: its multiprocess supervisor stops the whole
# server when a worker exits with it, rather than restarting that worker.
STARTUP_FAILURE_EXIT_CODE = 3

try:
    SETTINGS = load_server_settings()
    app, SERVER_LOCK = open_server(SETTINGS)
except (ConfigurationError, DatabaseInUseError) as error:
    print(f"earshot: {error}", file=sys.stderr, flush=True)
    raise SystemExit(STARTUP_FAILURE_EXIT_CODE) from None
