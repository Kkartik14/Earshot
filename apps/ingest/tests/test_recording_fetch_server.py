"""A real server fetches a provider recording: both entry points, the replay path.

Each of Earshot's two entry points serves the test's store as an operator would
run it. A signed Retell ``call_ended`` names its recording on Retell's documented
storage host; in test mode (``EARSHOT_TEST_MODE=1`` with
``EARSHOT_TEST_FETCH_ORIGIN``) the server's fetch job sends that request to a local
plain-HTTP stub instead, the replay path for a hosted provider. The allowlist,
the URL rules and the job all run as in production, and the audio lands.
"""

from __future__ import annotations

import http.client
import json
import time

import pytest

import test_retell_connector as retell
from connector_support import SECRETS, signed_headers
from fetch_support import AudioServer, Reply, wav_bytes
from server_process import ServerProcess
from store_support import catalog_rows, make_store

pytestmark = pytest.mark.integration

SECRET_ENV = "RETELL_FETCH_SERVER_TEST_SECRET"
ENDPOINT = "retell_fetch_0001"
RETELL_HOST = "retellai.s3.us-west-2.amazonaws.com"
RETELL_PATH = f"/{retell.CALL_ID}/recording.wav"
AUDIO = wav_bytes(0.5)


def _deliver(port: int) -> dict:
    payload = retell._call("call_ended")
    payload["call"]["recording_url"] = f"https://{RETELL_HOST}{RETELL_PATH}?sig=replay"
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = dict(
        (name.decode(), value.decode())
        for name, value in signed_headers("retell", body, at_seconds=int(time.time()))
    )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", f"/hooks/v1/connectors/{ENDPOINT}", body=body, headers=headers)
        response = connection.getresponse()
        assert response.status == 200, response.read()
        return json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize("entry_point", ["uvicorn", "serve"])
def test_a_server_fetches_a_delivered_recording_in_test_mode(tmp_path, entry_point) -> None:
    path = tmp_path / "server"
    store = make_store(path)
    store.create_connector(
        "default", provider="retell", secret_ref=f"env:{SECRET_ENV}", endpoint_id=ENDPOINT
    )
    store.close()
    with AudioServer(None) as stub:
        stub.route(RETELL_HOST, RETELL_PATH, Reply(body=[AUDIO]))
        server = ServerProcess.start(
            path,
            tmp_path / "server.log",
            entry_point=entry_point,
            EARSHOT_TEST_MODE="1",
            EARSHOT_TEST_FETCH_ORIGIN=f"http://127.0.0.1:{stub.port}",
            **{SECRET_ENV: SECRETS["retell"]},
        )
        try:
            delivered = _deliver(server.port)
            deadline = time.monotonic() + 30
            while catalog_rows(path, "SELECT state FROM recordings") != [("stored",)]:
                assert time.monotonic() < deadline, (tmp_path / "server.log").read_text()
                time.sleep(0.1)
        finally:
            server.stop()

    assert delivered["disposition"] == "applied"
    assert catalog_rows(path, "SELECT state, url, error_code FROM recording_fetches") == [
        ("stored", None, None)
    ]
    assert catalog_rows(path, "SELECT size FROM recordings") == [(len(AUDIO),)]
    [seen] = stub.seen()
    assert (seen.host, seen.path) == (RETELL_HOST, f"{RETELL_PATH}?sig=replay")
