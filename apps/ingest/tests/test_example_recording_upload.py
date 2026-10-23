"""The Pipecat example's recordings client against a real Earshot server.

``examples/pipecat_otlp/earshot_upload.py`` is what ``run_call.py --upload`` uses:
it creates both tracks' recordings at the call's first audio and uploads the WAVs
after the run. Here it talks to the real app on a real uvicorn server, with a
real project API key, a real catalog and both blob backends.
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import wave
from pathlib import Path

import pytest

from earshot.api import create_app
from otlp_support import request, serve
from store_support import catalog_rows, make_store

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "pipecat_otlp"
sys.path.insert(0, str(EXAMPLE))

from earshot_upload import (  # noqa: E402
    EarshotRecordings,
    UploadError,
    authorization_header,
    recording_id_for,
)

pytestmark = pytest.mark.integration

STARTED_AT = 1_800_000_000_123_456_789
RECORDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _write_wav(path: Path, seconds: float, tone: int) -> Path:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(bytes([tone, 0]) * int(16_000 * seconds))
    path.write_bytes(buffer.getvalue())
    return path


@pytest.mark.parametrize(
    ("conversation_id", "speaker", "expected"),
    [
        ("pipecat-otlp-7f1c0d6e", "user", "pipecat-otlp-7f1c0d6e.user"),
        ("pipecat-otlp-7f1c0d6e", "agent", "pipecat-otlp-7f1c0d6e.agent"),
        ("call 42/é", "user", None),
        ("-leading", "agent", None),
        ("x" * 300, "user", None),
    ],
)
def test_a_recording_id_is_derived_from_the_conversation_and_the_speaker(
    conversation_id, speaker, expected
) -> None:
    derived = recording_id_for(conversation_id, speaker)

    assert RECORDING_ID.fullmatch(derived)
    assert derived.endswith(f".{speaker}")
    assert derived == recording_id_for(conversation_id, speaker)  # stable across retries
    if expected is not None:
        assert derived == expected
    assert recording_id_for("x" * 300, "user") != recording_id_for("x" * 299 + "y", "user")


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"EARSHOT_API_KEY": "earshot_sk_a"}, "Bearer earshot_sk_a"),
        (
            {"OTEL_EXPORTER_OTLP_HEADERS": "x-tenant=blue, Authorization=Bearer%20earshot_sk_b"},
            "Bearer earshot_sk_b",
        ),
        (
            {
                "EARSHOT_API_KEY": "earshot_sk_a",
                "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20earshot_sk_b",
            },
            "Bearer earshot_sk_a",
        ),
        ({"OTEL_EXPORTER_OTLP_HEADERS": "x-tenant=blue"}, None),
        ({}, None),
    ],
)
def test_the_api_key_comes_from_earshot_api_key_or_the_otlp_headers(environment, expected) -> None:
    assert authorization_header(environment) == expected


def test_both_tracks_are_created_at_first_audio_and_uploaded_after_the_call(
    tmp_path, blob_backend
) -> None:
    store = make_store(tmp_path)
    key = store.issue_api_key("default", label="example").credential
    tracks = {
        "user": _write_wav(tmp_path / "user.wav", 1.25, 7),
        "agent": _write_wav(tmp_path / "agent.wav", 0.5, 9),
    }

    with serve(create_app(store=store, blob_store=blob_backend.store)) as port:
        client = EarshotRecordings(f"http://127.0.0.1:{port}/", authorization=f"Bearer {key}")
        created = client.create_tracks("conv-1", started_at_unix_nano=STARTED_AT)
        retried = client.create_tracks("conv-1", started_at_unix_nano=STARTED_AT)
        uploaded = client.upload_tracks("conv-1", tracks)
        again = client.upload_tracks("conv-1", tracks)
        resolved = request(
            port,
            "POST",
            "/v1/calls:resolve",
            b'{"session_key": "conv-1"}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        ).json()["call_id"]

    assert {speaker: body["state"] for speaker, body in created.items()} == {
        "user": "pending",
        "agent": "pending",
    }
    assert retried == created
    for speaker, path in tracks.items():
        body = uploaded[speaker]
        assert body["recording_id"] == f"conv-1.{speaker}"
        assert (body["state"], body["sha256"]) == (
            "stored",
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        assert body["speaker"] == speaker and body["call_id"] == resolved
        assert body["started_at_unix_nano"] == str(STARTED_AT)
        assert again[speaker]["sha256"] == body["sha256"]
    assert catalog_rows(tmp_path, "SELECT recording_count FROM calls") == [(2,)]
    assert len(blob_backend.objects()) == 2


def test_a_refused_request_is_reported_by_status_and_code_without_the_key(tmp_path) -> None:
    store = make_store(tmp_path)
    store.issue_api_key("default", label="example")

    with serve(create_app(store=store)) as port:
        client = EarshotRecordings(
            f"http://127.0.0.1:{port}", authorization="Bearer earshot_sk_not-a-real-key"
        )
        with pytest.raises(UploadError) as refused:
            client.create_tracks("conv-1", started_at_unix_nano=STARTED_AT)

    assert "401" in str(refused.value) and "EARSHOT_UNAUTHORIZED" in str(refused.value)
    assert "not-a-real-key" not in str(refused.value)
