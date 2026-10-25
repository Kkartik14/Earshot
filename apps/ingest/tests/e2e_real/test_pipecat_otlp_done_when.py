"""The done-when e2e: one real Pipecat call into a real Earshot on Postgres and S3.

A real ``earshot serve`` process runs on a fresh Postgres database
(``EARSHOT_TEST_POSTGRES_URL``, by default ``earshot-dev-postgres``) with a fresh
correlation key, and keeps recordings in the test S3 server
(``EARSHOT_TEST_S3_URL``, by default ``earshot-dev-minio``) through
``EARSHOT_BLOB_URL=s3://…``. A project and its API key are made with the
``earshot`` CLI while the server runs. ``examples/pipecat_otlp/run_call.py
--upload`` then runs one real call (Deepgram, Groq, Cartesia), configured only by
``OTEL_EXPORTER_OTLP_ENDPOINT`` and ``OTEL_EXPORTER_OTLP_HEADERS``; it uploads one
stereo WAV (channel 0 user, channel 1 agent) with ``earshot.client``. The call must
pass ``call_done_when``'s checks against the example's own manifest (its tee span
count, its ``started_at``, the WAV it uploaded), the recording must keep its channel
map, and the caller's words must come back in the user transcript. Then the server's
own analysis and projection jobs run, and the call view must pass
``assert_call_view``: the recording's analysis stored, at least one turn's audible
gap at the agent measured within (0, 15 s) with a ± of at most 100 ms and every
other turn not measurable with its reason, a cost lower bound above 0 with a costed
item from each provider, and the transcript placed on the recording.

Provider keys come from the environment (``DEEPGRAM_API_KEY``, ``GROQ_API_KEY``,
``CARTESIA_API_KEY``, ``CARTESIA_VOICE_ID``). Without them, or without the
example's requirements or the S3 server, the test skips; with
``EARSHOT_REQUIRE_REAL_PROVIDERS=1`` (the release gate) it fails instead.

A failing call is classified. When no provider span (``stt``, ``llm``, ``tts``)
reached Earshot, it is reported as INFRASTRUCTURE: a provider or network error
before the call's first span (Pipecat still exports its ``conversation`` root at
the end, and VAD-driven ``turn`` spans need no provider). The gate may re-run
that once and must record it. Anything else is a CALL failure.
Secret values never appear in a failure message: the example's log tail is shown
with every key replaced by its name. Do not run this test with ``--showlocals``
(``-l``): its locals hold the keys.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping

import pytest

from call_done_when import (
    Earshot,
    SentCall,
    assert_call_view,
    assert_done_when,
    read_rows,
    script_words,
    wait_until_projected,
)
from server_process import ROOT, ServerProcess
from store_support import postgres_cluster

pytestmark = [pytest.mark.real_providers, pytest.mark.e2e, pytest.mark.slow]

EXAMPLE = ROOT / "examples" / "pipecat_otlp" / "run_call.py"
PROVIDER_ENV = ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID")
EXAMPLE_MODULES = ("pipecat", "deepgram", "opentelemetry.exporter.otlp.proto.http")
REQUIRED = os.environ.get("EARSHOT_REQUIRE_REAL_PROVIDERS") == "1"
PROJECT = "done-when"
CALL_TIMEOUT_S = 300


def _unavailable(reason: str) -> None:
    """Skip, or fail when the release gate requires this test to run."""

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


def test_a_real_pipecat_call_becomes_one_closed_call_with_its_recordings(tmp_path, request) -> None:
    missing = [name for name in PROVIDER_ENV if not os.environ.get(name)]
    if missing:
        _unavailable(f"provider env is missing: {', '.join(missing)}")
    absent = [name for name in EXAMPLE_MODULES if importlib.util.find_spec(name) is None]
    if absent:
        _unavailable(
            f"examples/pipecat_otlp/requirements.txt is not installed ({', '.join(absent)})"
        )
    try:
        s3 = request.getfixturevalue("s3_test_server")
    except pytest.skip.Exception as skipped:
        _unavailable(str(skipped))

    began = time.monotonic()
    data_dir, out = tmp_path / "data", tmp_path / "call"
    database_url = postgres_cluster().database_for(tmp_path / "catalog", empty=True)
    correlation_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    store_environment = {
        "EARSHOT_DATA_DIR": str(data_dir),
        "EARSHOT_DATABASE_URL": database_url,
        "EARSHOT_CORRELATION_KEY": correlation_key,
    }
    prefix = f"done-when-{secrets.token_hex(6)}"
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
    server_ready_s = time.monotonic() - began
    hidden: dict[str, str] = {name: os.environ[name] for name in PROVIDER_ENV}
    hidden.update(EARSHOT_CORRELATION_KEY=correlation_key, S3_SECRET=s3.secret_access_key)
    try:
        # The operator's CLI, while the server runs, on the same catalog.
        cli_environment = {
            **{key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")},
            **store_environment,
            "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
        }
        _earshot_cli(cli_environment, "project", "create", PROJECT, "--display-name", "Pipecat E2E")
        credential = _earshot_cli(
            cli_environment, "api-key", "issue", "--project", PROJECT, "--label", "pipecat-e2e"
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

        # The bot: configured only by the standard exporter variables.
        conversation_id = f"pipecat-otlp-e2e-{secrets.token_hex(8)}"
        example_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("EARSHOT_", "OTEL_", "AWS_"))
        }
        example_environment.update(
            OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{server.port}",
            OTEL_EXPORTER_OTLP_HEADERS=f"Authorization=Bearer%20{credential}",
        )
        call_log = tmp_path / "call.log"
        call_began = time.monotonic()
        with call_log.open("wb") as log:
            try:
                returncode: int | None = subprocess.run(
                    [
                        *(sys.executable, str(EXAMPLE), "--upload"),
                        *("--out", str(out), "--conversation-id", conversation_id),
                    ],
                    cwd=ROOT,
                    env=example_environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=CALL_TIMEOUT_S,
                ).returncode
            except subprocess.TimeoutExpired:
                returncode = None
        call_s = time.monotonic() - call_began
        if returncode != 0:
            [(provider_spans,)] = read_rows(
                database_url,
                "SELECT count(*) FROM spans WHERE project_id = ? AND scope_name = 'pipecat'",
                (PROJECT,),
            )
            tail = _redacted(call_log.read_text(errors="replace")[-4000:], hidden)
            kind = (
                "INFRASTRUCTURE failure (a provider or network error before the first span: "
                "no stt, llm or tts span reached Earshot; the gate may re-run once and must "
                "record it)"
                if provider_spans == 0
                else f"CALL failure after {provider_spans} provider spans reached Earshot"
            )
            pytest.fail(f"{kind}: run_call.py exit {returncode}\n{tail}", pytrace=False)

        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["conversation_id"] == conversation_id
        sent = SentCall(
            session_key=conversation_id,
            exported_span_count=manifest["exported_span_count"],
            started_at_unix_nano=int(manifest["started_at_unix_nano"]),
            tracks={"mixed": (out / manifest["tracks"]["stereo"]).read_bytes()},
        )
        settle_began = time.monotonic()
        settled = assert_done_when(earshot, sent)
        settle_s = time.monotonic() - settle_began
        projected = wait_until_projected(earshot, settled.detail["call_id"])
        project_s = time.monotonic() - settle_began - settle_s
    finally:
        server.stop()

    detail = settled.detail
    [recording] = detail["recordings"]
    assert recording["channel_speakers"] == ["user", "agent"]
    user_text = " ".join(line["text"] for line in detail["transcript"] if line["speaker"] == "user")
    heard = script_words(user_text) & script_words(" ".join(manifest["caller_script"]))
    assert len(heard) >= 2, detail["transcript"]
    # Kept before the checks, so a failing call view can be read afterwards.
    (tmp_path / "call_view.json").write_text(json.dumps(projected, indent=2) + "\n")
    view = assert_call_view(projected, manifest["providers"])

    summary = {
        "call_id": detail["call_id"],
        "conversation_id": conversation_id,
        "seconds": {
            "server_ready": round(server_ready_s, 2),
            "call_and_upload": round(call_s, 2),
            "settle_after_upload": round(settle_s, 2),
            "projected_after_settle": round(project_s, 2),
            "total": round(time.monotonic() - began, 2),
        },
        "spans": {
            "exported_tee": manifest["exported_span_count"],
            "exported_by_name": manifest["exported_span_names"],
            "stored": detail["span_count"],
        },
        "revisions": detail["revisions"],
        "recordings": {
            item["speaker"]: {key: item[key] for key in ("size", "duration_ms", "sha256")}
            for item in detail["recordings"]
        },
        "first_stt_minus_started_at_s": round(settled.first_stt_offset_nano / 1e9, 3),
        "transcript": [(line["speaker"], line["text"]) for line in detail["transcript"]],
        "providers": manifest["providers"],
        "call_view": view,
    }
    (tmp_path / "done_when_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
