"""The real LiveKit done-when e2e: one real LiveKit Agents 1.8 call into a real Earshot.

A real ``earshot serve`` process runs on a fresh Postgres database
(``EARSHOT_TEST_POSTGRES_URL``) with a fresh correlation key, and keeps recordings
in the test S3 server (``EARSHOT_TEST_S3_URL``) through ``EARSHOT_BLOB_URL=s3://…``.
A project and its API key are made with the ``earshot`` CLI while it runs.
``examples/livekit_otlp/run_call.py --upload`` then runs one real headless call
(Silero VAD, Deepgram, Groq, Cartesia; LiveKit's own ``RecorderIO`` records it) in
the LiveKit venv ``EARSHOT_LIVEKIT_PYTHON`` names: LiveKit 1.8 needs OTel and
openai versions Earshot's own lock cannot take, so the agent runs in its own venv
(``examples/livekit_otlp/requirements.txt``, with Earshot installed for
``earshot.client``). It is configured only by ``OTEL_EXPORTER_OTLP_ENDPOINT`` and
``OTEL_EXPORTER_OTLP_HEADERS``; it creates its stereo recording when the session
starts and uploads it from ``on_session_end``.

Asserted:
- one call for the session key, closed, whose latest revision closed on the end
  signal and is final;
- the stereo Ogg Opus recording is stored: the uploaded bytes, ``mixed`` with
  ``channel_speakers`` ``["user", "agent"]``, longer than 30 s;
- every exported span is stored, none rejected, conflicted or discarded;
- the transcript is the played text: one agent line per ``agent_turn`` (never a
  discarded preemptive reply), and the caller's words in the user lines;
- once the server's own analysis and projection jobs have run, the call view passes
  ``assert_call_view``: the recording's analysis stored, at least one turn's audible
  gap at the agent measured within (0, 15 s) with a ± of at most 100 ms and every
  other turn not measurable with its reason, a cost lower bound above 0 with a
  costed item from each provider, and the transcript placed on the recording.

The call is headless (Tier A: no LiveKit server, no WebRTC), so it proves the
plumbing from LiveKit's recorder to the call view, not the accuracy of a gap
measured on a real room's audio.

Provider keys come from the environment (``DEEPGRAM_API_KEY``, ``GROQ_API_KEY``,
``CARTESIA_API_KEY``, ``CARTESIA_VOICE_ID``). Without them, the LiveKit venv or the
S3 server the test skips; with ``EARSHOT_REQUIRE_REAL_PROVIDERS=1`` it fails
instead. Secret values never appear in a failure message. Do not run it with
``--showlocals``: its locals hold the keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from call_done_when import (
    Earshot,
    assert_call_view,
    read_rows,
    script_words,
    wait_until_projected,
    wait_until_settled,
)
from server_process import ROOT, ServerProcess
from store_support import postgres_cluster

pytestmark = [pytest.mark.real_providers, pytest.mark.e2e, pytest.mark.slow]

EXAMPLE = ROOT / "examples" / "livekit_otlp" / "run_call.py"
PROVIDER_ENV = ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID")
LIVEKIT_PYTHON_ENV = "EARSHOT_LIVEKIT_PYTHON"
REQUIRED = os.environ.get("EARSHOT_REQUIRE_REAL_PROVIDERS") == "1"
PROJECT = "livekit-smoke"
CALL_TIMEOUT_S = 300
LOSS_COUNTERS = ("rejected_spans", "span_conflicts", "parked_traces_discarded")


def _unavailable(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"EARSHOT_REQUIRE_REAL_PROVIDERS=1, but {reason}", pytrace=False)
    pytest.skip(reason)


def _redacted(text: str, hidden: Mapping[str, str]) -> str:
    for name, value in hidden.items():
        if value:
            text = text.replace(value, f"[{name}]")
    return text


def _earshot_cli(environment: Mapping[str, str], *arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "earshot.cli", *arguments],
        cwd=ROOT,
        env=dict(environment),
        capture_output=True,
        text=True,
        timeout=60,
    )
    # stdout may hold an API key: only stderr is ever shown.
    assert completed.returncode == 0, (arguments[:2], completed.stderr[-2000:])
    return json.loads(completed.stdout)


def _livekit_python() -> Path:
    configured = os.environ.get(LIVEKIT_PYTHON_ENV)
    if not configured:
        _unavailable(f"{LIVEKIT_PYTHON_ENV} names no LiveKit 1.8 venv python")
    python = Path(configured)
    probe = subprocess.run(
        [str(python), "-c", "import livekit.agents, earshot.client"],
        capture_output=True,
        timeout=60,
    )
    if probe.returncode != 0:
        _unavailable(f"{LIVEKIT_PYTHON_ENV} lacks examples/livekit_otlp/requirements.txt")
    return python


def test_a_real_livekit_call_becomes_one_final_call_with_its_stereo_recording(
    tmp_path, request
) -> None:
    missing = [name for name in PROVIDER_ENV if not os.environ.get(name)]
    if missing:
        _unavailable(f"provider env is missing: {', '.join(missing)}")
    livekit_python = _livekit_python()
    try:
        s3 = request.getfixturevalue("s3_test_server")
    except pytest.skip.Exception as skipped:
        _unavailable(str(skipped))

    data_dir, out = tmp_path / "data", tmp_path / "call"
    database_url = postgres_cluster().database_for(tmp_path / "catalog", empty=True)
    correlation_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    store_environment = {
        "EARSHOT_DATA_DIR": str(data_dir),
        "EARSHOT_DATABASE_URL": database_url,
        "EARSHOT_CORRELATION_KEY": correlation_key,
    }
    prefix = f"livekit-smoke-{secrets.token_hex(6)}"
    server = ServerProcess.start(
        data_dir,
        tmp_path / "serve.log",
        entry_point="serve",
        EARSHOT_BLOB_URL=s3.blob_url(prefix),
        AWS_CONFIG_FILE=str(tmp_path / "no-aws-config"),
        AWS_SHARED_CREDENTIALS_FILE=str(tmp_path / "no-aws-credentials"),
        **store_environment,
        **s3.environment(),
    )
    hidden: dict[str, str] = {name: os.environ[name] for name in PROVIDER_ENV}
    hidden.update(EARSHOT_CORRELATION_KEY=correlation_key, S3_SECRET=s3.secret_access_key)
    try:
        cli_environment = {
            **{key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")},
            **store_environment,
            "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
        }
        _earshot_cli(cli_environment, "project", "create", PROJECT, "--display-name", "LiveKit")
        credential = _earshot_cli(
            cli_environment, "api-key", "issue", "--project", PROJECT, "--label", "livekit-e2e"
        )["credential"]
        hidden["EARSHOT_API_KEY"] = credential
        earshot = Earshot(
            port=server.port,
            project_id=PROJECT,
            catalog=database_url,
            read_blob=lambda key: s3.admin.get_object(Bucket=s3.bucket, Key=f"{prefix}/{key}")[
                "Body"
            ].read(),
            credential=credential,
        )

        session_key = f"livekit-otlp-e2e-{secrets.token_hex(8)}"
        example_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("EARSHOT_", "OTEL_", "AWS_", "LIVEKIT_", "PYTHON"))
        }
        example_environment.update(
            OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{server.port}",
            OTEL_EXPORTER_OTLP_HEADERS=f"Authorization=Bearer%20{credential}",
        )
        call_log = tmp_path / "call.log"
        with call_log.open("wb") as log:
            try:
                returncode: int | None = subprocess.run(
                    [
                        *(str(livekit_python), str(EXAMPLE), "--upload"),
                        *("--out", str(out), "--session-key", session_key),
                    ],
                    cwd=ROOT,
                    env=example_environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=CALL_TIMEOUT_S,
                ).returncode
            except subprocess.TimeoutExpired:
                returncode = None
        if returncode != 0:
            tail = _redacted(call_log.read_text(errors="replace")[-4000:], hidden)
            pytest.fail(f"run_call.py exit {returncode}\n{tail}", pytrace=False)

        manifest = json.loads((out / "manifest.json").read_text())
        uploaded = (out / "recording.ogg").read_bytes()
        call_id = earshot.json("POST", "/v1/calls:resolve", {"session_key": session_key})["call_id"]
        detail = wait_until_settled(earshot, call_id, recordings=1)
        project = (PROJECT,)

        # One call for the key; its latest revision closed on the end signal, final.
        assert read_rows(
            database_url,
            "SELECT call_id, session_key, state, dirty FROM calls WHERE project_id = ?",
            project,
        ) == [(call_id, session_key, "closed", 0)]
        latest = detail["revisions"][-1]
        assert (latest["close_reason"], latest["finality"]) == ("end_signal", "final")
        assert detail["materialize_error"] is None

        # The stereo recording: the uploaded bytes, with its channel map.
        [recording] = detail["recordings"]
        digest = hashlib.sha256(uploaded).hexdigest()
        assert {
            key: recording[key]
            for key in ("state", "speaker", "channel_speakers", "content_type", "sha256", "size")
        } == {
            "state": "stored",
            "speaker": "mixed",
            "channel_speakers": ["user", "agent"],
            "content_type": "audio/ogg",
            "sha256": digest,
            "size": len(uploaded),
        }
        assert recording["started_at_unix_nano"] == manifest["recording"]["started_at_unix_nano"]
        assert recording["duration_ms"] > 30_000
        [(blob_key,)] = read_rows(
            database_url,
            "SELECT blob_key FROM recordings WHERE project_id = ? AND call_id = ?",
            (PROJECT, call_id),
        )
        assert hashlib.sha256(earshot.read_blob(blob_key)).hexdigest() == digest
        content = earshot.call("GET", f"/v1/recordings/{recording['recording_id']}/content")
        assert (content.status, content.media_type, content.body) == (200, "audio/ogg", uploaded)

        # Every exported span is stored, and none was lost.
        [(stored_spans, agent_turns)] = read_rows(
            database_url,
            "SELECT count(*), sum(CASE WHEN name = 'agent_turn' THEN 1 ELSE 0 END) "
            "FROM spans WHERE project_id = ?",
            project,
        )
        assert stored_spans == manifest["exported_span_count"] == detail["span_count"]
        stats = earshot.json("GET", "/v1/otlp/stats")
        assert {name: stats[name] for name in LOSS_COUNTERS} == dict.fromkeys(LOSS_COUNTERS, 0)
        projected = wait_until_projected(earshot, call_id)
    finally:
        server.stop()

    summary = {
        "call_id": call_id,
        "session_key": session_key,
        "revisions": detail["revisions"],
        "recording": {key: recording[key] for key in ("size", "duration_ms", "sha256")},
        "spans": {"exported": manifest["exported_span_count"], "stored": stored_spans},
        "transcript": [(line["speaker"], line["text"]) for line in detail["transcript"]],
    }
    (tmp_path / "livekit_smoke_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))

    # The transcript is the text the call played: one agent line per agent_turn (a
    # discarded preemptive reply is never a line), and the caller's words.
    agent_lines = [line["text"] for line in detail["transcript"] if line["speaker"] == "agent"]
    user_text = " ".join(line["text"] for line in detail["transcript"] if line["speaker"] == "user")
    assert len(agent_lines) == agent_turns, detail["transcript"]
    assert all(text.strip() for text in agent_lines)
    heard = script_words(user_text) & script_words(" ".join(manifest["caller_script"]))
    assert len(heard) >= 2, detail["transcript"]

    # The call view. Kept before the checks, so a failing one can be read afterwards.
    (tmp_path / "call_view.json").write_text(json.dumps(projected, indent=2) + "\n")
    view = assert_call_view(projected, manifest["providers"])
    print(json.dumps({"call_view": view}, indent=2))
