"""A real Earshot server for the "Upload a call" form to write to.

Nothing here simulates Earshot. The server is the real app (``create_app``) on a
real uvicorn server over a real SQLite catalog and a local blob store, with the
default project's ``full`` capture preset; the form's requests create recordings,
upload their audio and attach transcripts exactly as a browser's would.

A test changes the project's capture preset (to show the form's ``metadata_only``
answers) through a second loopback port: ``POST /preset/<preset>`` calls the store's
own ``set_capture_preset``, as the operator CLI does. That port serves nothing else.

The script prints one JSON line (``port``, ``control_port``) when it is ready and
serves until its standard input closes; it then removes its temporary directory.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[6]
for path in (ROOT / "packages" / "sdk-python" / "src", ROOT / "packages" / "sdk-python" / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from earshot.api import create_app  # noqa: E402
from earshot.blobstore import LocalBlobStore  # noqa: E402
from earshot.call_capture import CAPTURE_PRESETS  # noqa: E402
from earshot.storage import IncidentStore  # noqa: E402
from otlp_support import serve  # noqa: E402


def _control_server(store: IncidentStore) -> ThreadingHTTPServer:
    class Control(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            prefix, _, preset = self.path.partition("/preset/")
            if prefix or preset not in CAPTURE_PRESETS:
                self.send_error(404)
                return
            store.set_capture_preset("default", preset)
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), Control)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="earshot-viewer-upload-"))
    try:
        store = IncidentStore(work / "data")
        app = create_app(store=store, blob_store=LocalBlobStore(work / "blobs"))
        control = _control_server(store)
        thread = threading.Thread(target=control.serve_forever, daemon=True)
        thread.start()
        try:
            with serve(app) as port:
                ready = {"port": port, "control_port": control.server_address[1]}
                print(json.dumps(ready), flush=True)
                sys.stdin.read()
        finally:
            control.shutdown()
            control.server_close()
            store.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
