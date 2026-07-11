from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from earshot.adapters import LiveKitAdapter, PipecatAdapter
from earshot.analysis import analyze_incident
from earshot.codec import PROTOBUF_MEDIA_TYPE, decode_incident_protobuf, encode_incident_protobuf
from earshot.contract import TimePoint
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import validate_incident
from incident_factory import make_valid_bundle

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _request(url: str, *, method: str = "GET", data: bytes | None = None, headers=None):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    return urllib.request.urlopen(request, timeout=3)


def _start_server(port: int, data_dir: Path) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "packages" / "sdk-python" / "src")
    environment["EARSHOT_DATA_DIR"] = str(data_dir)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.ingest.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"ingest server exited early\nstdout={stdout!r}\nstderr={stderr!r}"
            )
        try:
            with _request(f"http://127.0.0.1:{port}/readyz") as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.05)
    process.terminate()
    process.wait(timeout=3)
    raise AssertionError("ingest server did not become ready")


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def test_real_process_ingest_persists_across_restart(tmp_path) -> None:
    port = _free_port()
    data_dir = tmp_path / "persisted"
    bundle = make_valid_bundle()
    payload = encode_incident_protobuf(bundle)
    server = _start_server(port, data_dir)
    try:
        with _request(
            f"http://127.0.0.1:{port}/v1/incidents",
            method="POST",
            data=payload,
            headers={"Content-Type": PROTOBUF_MEDIA_TYPE},
        ) as response:
            assert response.status == 201
    finally:
        _stop_server(server)

    server = _start_server(port, data_dir)
    try:
        with _request(
            f"http://127.0.0.1:{port}/v1/incidents/bundle-1",
            headers={"Accept": PROTOBUF_MEDIA_TYPE},
        ) as response:
            assert response.status == 200
            assert decode_incident_protobuf(response.read()).profile == bundle.profile
        with _request(f"http://127.0.0.1:{port}/v1/incidents/bundle-1/analysis") as response:
            analysis = json.load(response)
            assert analysis["analysis"]["projections"]["session_id"] == "session-1"
    finally:
        _stop_server(server)


def _semantic_projection(bundle) -> dict[str, object]:
    render = next(item for item in bundle.profile.coverage if item.signal == "client.render")
    analysis = analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    turn = analysis.projections["turns"][0]
    operations = {item.operation_id: item for item in bundle.profile.operations}
    interruption_phases = {
        phase: sum(
            item.event_name == f"earshot.interruption.{phase}" for item in bundle.profile.events
        )
        for phase in ("accepted", "detected", "ignored")
    }
    return {
        "core_operation_names": sorted(
            {
                item.operation_name
                for item in bundle.profile.operations
                if item.operation_name in {"stt", "llm", "tts"}
            }
        ),
        "render_availability": render.availability,
        "provenance_available": all(
            item.evidence is not None for item in bundle.profile.operations
        ),
        "capture_classes": sorted({item.capture_class for item in bundle.profile.operations}),
        "interruption_phases": interruption_phases,
        "turn_correlated_response_operations": sorted(
            {
                operations[operation_id].operation_name
                for operation_id in turn["operation_ids"]
                if operations[operation_id].operation_name in {"llm", "tts"}
            }
        ),
        "latency_bases": {
            "first_token": turn["metrics"]["first_token_latency"]["basis"],
            "generated": turn["metrics"]["generated_response_latency"]["basis"],
            "response": turn["metrics"]["response_latency"]["basis"],
        },
    }


def test_pipecat_and_livekit_goldens_normalize_to_equivalent_voice_semantics() -> None:
    golden = ROOT / "fixtures" / "golden"
    pipecat_values = json.loads((golden / "pipecat_spans.json").read_text())
    livekit_values = json.loads((golden / "livekit_metrics.json").read_text())
    expected = json.loads((golden / "expected_semantics.json").read_text())

    pipecat_recorder = IncidentRecorder(
        session_id="pipecat-session",
        config=RecorderConfig(clock_domain_id="server-clock"),
    )
    pipecat = PipecatAdapter(pipecat_recorder, framework_version="golden")
    for span in pipecat_values:
        # Fixture session ownership is supplied by the recorder; clocks and OTel
        # identity remain the framework's original facts.
        pipecat.consume_span(span)
    pipecat_bundle = pipecat_recorder.close()

    livekit_recorder = IncidentRecorder(
        session_id="livekit-session",
        config=RecorderConfig(clock_domain_id="server-clock"),
    )
    livekit = LiveKitAdapter(livekit_recorder, framework_version="golden")
    for item in livekit_values:
        if "metric" in item:
            livekit.consume_metric(
                item["metric"], observed_at=TimePoint.model_validate(item["observed_at"])
            )
        else:
            livekit.consume_interruption_event(item["event"])
    livekit_bundle = livekit_recorder.close()

    assert validate_incident(pipecat_bundle).ok
    assert validate_incident(livekit_bundle).ok
    assert _semantic_projection(pipecat_bundle) == expected
    assert _semantic_projection(livekit_bundle) == expected
    assert _semantic_projection(pipecat_bundle) == _semantic_projection(livekit_bundle)

    pipecat_turn = next(
        operation
        for operation in pipecat_bundle.profile.operations
        if operation.attributes.get("earshot.framework.operation.name") == "turn"
    )
    assert pipecat_turn.operation_name == "framework_operation"
    assert all(
        int(pipecat_turn.started_at.monotonic_time_nano or "0")
        <= int(operation.started_at.monotonic_time_nano or "0")
        and int(operation.ended_at.monotonic_time_nano or "0")
        <= int(pipecat_turn.ended_at.monotonic_time_nano or "0")
        for operation in pipecat_bundle.profile.operations
        if operation.operation_id != pipecat_turn.operation_id
    )

    pipecat_analysis = analyze_incident(
        pipecat_bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    livekit_analysis = analyze_incident(
        livekit_bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000002000000000",
    )
    assert (
        pipecat_analysis.projections["turns"][0]["metrics"]["first_token_latency"]["availability"]
        == "not_observed"
    )
    assert (
        livekit_analysis.projections["turns"][0]["metrics"]["first_token_latency"]["availability"]
        == "available"
    )
    assert (
        next(
            operation
            for operation in livekit_bundle.profile.operations
            if operation.operation_name == "stt"
        ).turn_id
        is None
    )
