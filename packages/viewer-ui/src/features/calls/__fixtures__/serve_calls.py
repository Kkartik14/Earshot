"""A real Earshot server holding calls, for the viewer's call views to read.

Nothing here simulates Earshot. The server is the real app (``create_app``) on a
real uvicorn server over a real SQLite catalog and a local blob store:

- the two real captures (Pipecat 1.5 and LiveKit 1.8, ``fixtures/captured``) are
  recorded through ``CallStore.record_spans``, the receiver's storage call; the
  Pipecat call carries an ``earshot.agent.name`` and ``earshot.call.tags``, as a
  host would set them;
- their recordings are uploaded over HTTP, and the server's own analysis job
  computes their peaks (the Pipecat mono pair also gets its stereo rendition);
- the project prices its TTS model with ``POST /v1/prices``;
- 55 more calls make the list page twice; the first bills one LLM request, so
  its cost is complete.

The Pipecat call and the billed call are closed by the real ``Closer`` and
projected by the server's own projection job (the script waits for
``projection_state: current``). The LiveKit call and the other 54 stay open, so
they are not projected (the call view then shows the LiveKit span transcript).

``serve_calls.py`` prints one JSON line (the port and the IDs) when the server is
ready and serves until its standard input closes. ``serve_calls.py --write-peaks
DIR`` writes the two peaks files the server generated into ``DIR`` and exits.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[6]
for path in (ROOT / "packages" / "sdk-python" / "src", ROOT / "packages" / "sdk-python" / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue  # noqa: E402

from captured_otlp import CAPTURED_SESSION_KEY, captured_manifest, captured_requests  # noqa: E402
from earshot.api import create_app  # noqa: E402
from earshot.blobstore import LocalBlobStore  # noqa: E402
from earshot.call_capture import spans_from_request  # noqa: E402
from earshot.clock import ManualClock  # noqa: E402
from earshot.storage_calls import CallStore, CallTimings  # noqa: E402
from earshot.storage_closer import Closer  # noqa: E402
from earshot.storage_recordings import RecordingStore  # noqa: E402
from otlp_support import make_request, make_span, request, serve, string_attribute  # noqa: E402
from store_support import make_store  # noqa: E402

SECOND = 1_000_000_000
# After the wall clock, so the server's own closer and reapers act only when this
# script advances the clock.
START = 1_800_000_000 * SECOND
PIPECAT = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5"
LIVEKIT = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8"
BULK_CALLS = 55
PEAKS_FILES = {
    "livekit": "livekit-1.8.stereo.peaks.dat",
    "pipecat": "pipecat-1.5.rendition.peaks.dat",
}


def _livekit_manifest() -> dict[str, Any]:
    return json.loads((LIVEKIT / "manifest.json").read_text(encoding="utf-8"))


def _livekit_requests() -> list[ExportTraceServiceRequest]:
    return [
        ExportTraceServiceRequest.FromString((LIVEKIT / entry["file"]).read_bytes())
        for entry in _livekit_manifest()["requests"]
    ]


def _record(
    calls: CallStore, clock: ManualClock, requests: list[Any], *, step: int = 5 * SECOND
) -> str:
    """Record the requests ``step`` apart (5 s, as the real exporter sent them); the
    call's ID. A step of 0 keeps a call with an end marker from coming due."""

    call_ids: set[str] = set()
    for item in requests:
        call_ids.update(calls.record_spans("default", spans_from_request(item)).call_ids)
        clock.advance(step)
    [call_id] = call_ids
    return call_id


def _int_attribute(key: str, value: int) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(int_value=value))


def _bulk_call(calls: CallStore, clock: ManualClock, index: int, *, billed: bool = False) -> str:
    """A one-span call; a ``billed`` one is a Pipecat LLM request that reported its
    usage (1000 input and 100 output tokens of a model with a built-in price)."""

    key = f"bulk-{index:02d}"
    attributes = [string_attribute("earshot.session.id", key)]
    if billed:
        attributes += [
            string_attribute("gen_ai.provider.name", "groq"),
            string_attribute("gen_ai.request.model", "openai/gpt-oss-20b"),
            _int_attribute("gen_ai.usage.input_tokens", 1000),
            _int_attribute("gen_ai.usage.output_tokens", 100),
        ]
    span = make_span(1000 + index, 1, start=clock.wall, attributes=attributes)
    calls.record_spans("default", spans_from_request(make_request([span])))
    clock.advance(SECOND)
    return calls.call_id("default", key, 0)


def _labelled(requests: list[ExportTraceServiceRequest]) -> list[ExportTraceServiceRequest]:
    """The capture, with the agent name and tags a host sets on its spans."""

    span = requests[0].resource_spans[0].scope_spans[0].spans[0]
    span.attributes.append(string_attribute("earshot.agent.name", "booking-agent"))
    tags = span.attributes.add(key="earshot.call.tags").value.array_value.values
    tags.add(string_value="vip")
    tags.add(string_value="refund")
    return requests


def _wait_current(port: int, call_id: str, *, seconds: float = 120) -> None:
    """Until the server's projection job has projected the call, analyses included."""

    deadline = time.monotonic() + seconds
    while True:
        detail = request(port, "GET", f"/v1/calls/{call_id}").json()
        projection = detail["projection"]
        if (
            detail["projection_state"] == "current"
            and projection is not None
            and projection["analysis_state"] in ("none", "stored")
        ):
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{call_id} is not projected: {detail['projection_state']}")
        time.sleep(0.2)


def _digest(body: bytes) -> str:
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


def _upload(port: int, manifest: dict[str, Any], audio: bytes) -> None:
    created = request(
        port,
        "POST",
        "/v1/recordings",
        json.dumps(manifest).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert created.status == 201, (created.status, created.body[:300])
    stored = request(
        port,
        "PUT",
        created.json()["upload_path"],
        audio,
        headers={"Content-Type": manifest["content_type"], "Content-Digest": _digest(audio)},
    )
    assert stored.status == 200, (stored.status, stored.body[:300])


def _wait_for(port: int, path: str, *, seconds: float = 120) -> bytes:
    """The body of ``path`` once the server's analysis has stored it."""

    deadline = time.monotonic() + seconds
    while True:
        reply = request(port, "GET", path)
        if reply.status == 200:
            return reply.body
        if time.monotonic() > deadline:
            raise AssertionError(f"{path}: {reply.status} {reply.body[:300]!r}")
        time.sleep(0.2)


def _first_span(requests: list[Any], name: str) -> Any:
    for item in requests:
        for span in spans_from_request(item):
            if span.span.name == name:
                return span.span
    raise AssertionError(f"the capture has no {name} span")


def build(work: Path, *, keep_serving: bool, peaks_dir: Path | None) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(work, clock=clock)
    blobs = LocalBlobStore(work / "blobs")
    calls = CallStore(store)
    pipecat_call = _record(calls, clock, _labelled(captured_requests()))
    billed_call = _bulk_call(calls, clock, 0, billed=True)
    app = create_app(store=store, blob_store=blobs)

    with serve(app) as port:
        pipecat_audio = captured_manifest()["audio"]
        pipecat_start = int(pipecat_audio["started_at_unix_nano"])
        for speaker in ("user", "agent"):
            _upload(
                port,
                {
                    "recording_id": f"pipecat.{speaker}",
                    "session_key": CAPTURED_SESSION_KEY,
                    "speaker": speaker,
                    "content_type": "audio/wav",
                    "started_at_unix_nano": str(pipecat_start),
                },
                (PIPECAT / pipecat_audio[speaker]["file"]).read_bytes(),
            )
        pipecat_peaks = _wait_for(port, "/v1/recordings/pipecat.user/rendition/peaks")
        # The project's own price for its TTS model (it wins over the built-in one).
        price = request(
            port,
            "POST",
            "/v1/prices",
            json.dumps(
                {
                    "provider": "cartesia",
                    "model": "sonic-3",
                    "unit": "character",
                    "price_nano_usd": 30_000,
                    "effective_from_unix_nano": "0",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert price.status == 201, (price.status, price.body[:300])
        # Close the Pipecat call (with its analysed recordings) and the billed call.
        clock.advance(CallTimings().long_quiet_nano)
        recordings = RecordingStore(store, calls=calls, blobs=blobs)
        closer = Closer(store, calls=calls, recordings=recordings)
        while closer.close_next() is not None:
            pass
        # The rest stay open: never projected. The LiveKit call's evidence all lands
        # at one instant, so its end marker never comes due on the frozen clock.
        for index in range(1, BULK_CALLS):
            _bulk_call(calls, clock, index)
        livekit_call = _record(calls, clock, _livekit_requests(), step=0)
        livekit = _livekit_manifest()
        _upload(
            port,
            {
                "recording_id": "livekit.stereo",
                "session_key": livekit["provenance"]["session_key"],
                "speaker": "mixed",
                "channel_speakers": livekit["audio"]["channel_speakers"],
                "content_type": "audio/ogg",
                "started_at_unix_nano": livekit["audio"]["started_at_unix_nano"],
            },
            (LIVEKIT / livekit["audio"]["file"]).read_bytes(),
        )
        peaks = {
            "livekit": _wait_for(port, "/v1/recordings/livekit.stereo/peaks"),
            "pipecat": pipecat_peaks,
        }
        if peaks_dir is not None:
            for name, body in peaks.items():
                (peaks_dir / PEAKS_FILES[name]).write_bytes(body)
        if not keep_serving:
            return
        for call_id in (pipecat_call, billed_call):
            _wait_current(port, call_id)

        llm = _first_span(captured_requests(), "llm")
        ready = {
            "port": port,
            "pipecat_call_id": pipecat_call,
            "livekit_call_id": livekit_call,
            "complete_cost_call_id": billed_call,
            "call_count": BULK_CALLS + 2,
            "llm_span": {"trace_id": llm.trace_id.hex(), "span_id": llm.span_id.hex()},
        }
        print(json.dumps(ready), flush=True)
        sys.stdin.read()


def main(argv: list[str]) -> int:
    peaks_dir = Path(argv[argv.index("--write-peaks") + 1]) if "--write-peaks" in argv else None
    work = Path(tempfile.mkdtemp(prefix="earshot-viewer-calls-"))
    try:
        build(work, keep_serving=peaks_dir is None, peaks_dir=peaks_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
