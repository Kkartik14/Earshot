"""Measure Month 2's kept gating numbers (final-plan §9, M19 as trimmed). Not run in CI.

``view``: a 60-minute synthetic call in the Pipecat shape and in the LiveKit shape
(360 turns, every ``llm`` request carrying the whole conversation so far, as both
frameworks export it), recorded through ``CallStore.record_spans``, closed by the
real ``Closer`` into revision 1 (timed: the materialization), then projected by the
real ``CallProjector`` (timed). Its recordings are real rows with a stored analysis
whose speech runs are synthetic (one user and one agent run per turn): the
analysis's own cost is §9.2's, measured by WS2. The revision bundle is checked for
a declared truncation (``recorder_capture_truncated``). A real server then serves
``GET /v1/calls/{id}``, timed over ``--reads`` reads (p50, p95), with its body size
raw and gzipped. Thresholds: no truncation; view ≤ 1 MB gzipped; p95 ≤ 500 ms.

``search``: ``--calls`` projected calls (20 transcript lines each), written straight
into the catalog, then ``GET /v1/calls?q=…`` through ``CallReads.list_calls``:
a rare word, a common word, and ``q`` with filters, each timed over ``--reads``.
Threshold (M19, measured at 10k here; 50k is deferred): ≤ 1 s.

``reprice``: ``--calls`` projected calls with the Pipecat fixture's usage; one
project price insert makes them stale; the projection job reprices them all
(timed), extrapolated linearly to 100k. Threshold: 100k in ≤ 10 min.

    .venv/bin/python scripts/bench_call_view.py view search reprice
"""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "tests"))

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span  # noqa: E402

from earshot.blobstore import LocalBlobStore, put_stream  # noqa: E402
from earshot.call_capture import spans_from_request  # noqa: E402
from earshot.call_projection import CALL_PROJECTION_VERSION, compact_json  # noqa: E402
from earshot.clock import ManualClock  # noqa: E402
from earshot.codec import decode_incident_protobuf  # noqa: E402
from earshot.pricing_defaults import BUILTIN_PRICES  # noqa: E402
from earshot.storage import IncidentStore  # noqa: E402
from earshot.storage_call_projections import CallProjector  # noqa: E402
from earshot.storage_call_reads import CallFilters, CallReads  # noqa: E402
from earshot.storage_calls import CallStore, CallTimings  # noqa: E402
from earshot.storage_closer import Closer  # noqa: E402
from earshot.storage_prices import NewPrice, PriceStore  # noqa: E402
from earshot.storage_recordings import RecordingManifest, RecordingStore  # noqa: E402

SECOND = 1_000_000_000
MS = 1_000_000
START = 1_800_000_000 * SECOND
TURNS = 360
TURN = 10 * SECOND
HOUR_MS = TURNS * 10_000
KEY = "bench-60-minutes"


def _value(attribute, value) -> None:
    if isinstance(value, bool):
        attribute.value.bool_value = value
    elif isinstance(value, int):
        attribute.value.int_value = value
    elif isinstance(value, float):
        attribute.value.double_value = value
    else:
        attribute.value.string_value = value


def _span(span_id: int, name: str, start: int, end: int, parent: int | None, **attributes) -> Span:
    span = Span(
        trace_id=(7).to_bytes(16, "big"),
        span_id=span_id.to_bytes(8, "big"),
        name=name,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
    )
    if parent is not None:
        span.parent_span_id = parent.to_bytes(8, "big")
    for key, value in attributes.items():
        item = span.attributes.add()
        item.key = key.replace("__", ".")
        _value(item, value)
    return span


def _request(spans: list[Span], scope: str, **resource) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    for key, value in {"service.name": "bench-agent", **resource}.items():
        attribute = resource_spans.resource.attributes.add()
        attribute.key = key
        attribute.value.string_value = value
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = scope
    scope_spans.spans.extend(spans)
    return request


def _lines(turn: int) -> tuple[str, str]:
    user = f"user line {turn}: " + "I would like to change the booking please " * 2
    agent = f"agent reply {turn}: " + "certainly, I can help you with that today " * 3
    return user, agent


def pipecat_requests() -> list[ExportTraceServiceRequest]:
    history: list[dict[str, str]] = []
    requests = []
    for turn in range(TURNS):
        t = START + turn * TURN
        base = 1 + turn * 10
        user, agent = _lines(turn)
        history.append({"role": "user", "content": user})
        common = {"conversation.id": KEY}
        requests.append(
            _request(
                [
                    _span(
                        base,
                        "turn",
                        t,
                        t + 9 * SECOND,
                        None,
                        **common,
                        **{"turn.number": turn + 1, "turn.user_bot_latency_seconds": 1.4},
                    )
                ],
                "pipecat.turn",
            )
        )
        requests.append(
            _request(
                [
                    _span(
                        base + 1,
                        "stt",
                        t + 1 * SECOND,
                        t + 3500 * MS,
                        base,
                        transcript=user,
                        **{
                            "gen_ai.provider.name": "deepgram",
                            "gen_ai.request.model": "nova-3-general",
                        },
                    ),
                    _span(
                        base + 2,
                        "llm",
                        t + 3600 * MS,
                        t + 4500 * MS,
                        base,
                        input=json.dumps(history),
                        output=agent,
                        **{
                            "gen_ai.provider.name": "groq",
                            "gen_ai.request.model": "openai/gpt-oss-20b",
                            "gen_ai.usage.input_tokens": 100 + 40 * turn,
                            "gen_ai.usage.output_tokens": 40,
                        },
                    ),
                    _span(
                        base + 3,
                        "tts",
                        t + 4600 * MS,
                        t + 8 * SECOND,
                        base,
                        text=agent,
                        **{
                            "gen_ai.provider.name": "cartesia",
                            "gen_ai.request.model": "sonic-3",
                            "metrics.character_count": len(agent),
                            "metrics.ttfb": 0.3,
                        },
                    ),
                ],
                "pipecat",
            )
        )
        history.append({"role": "assistant", "content": agent})
    requests.append(
        _request(
            [
                _span(
                    999_999,
                    "conversation",
                    START,
                    START + TURNS * TURN,
                    None,
                    **{"conversation.id": KEY},
                )
            ],
            "pipecat.turn",
        )
    )
    return requests


def livekit_requests() -> list[ExportTraceServiceRequest]:
    history: list[dict[str, str]] = []
    requests = []
    key = {"earshot.session.id": KEY}
    for turn in range(TURNS):
        t = START + turn * TURN
        base = 1 + turn * 20
        user, agent = _lines(turn)
        history.append({"role": "user", "content": user})
        speech = f"speech_{turn:06d}"
        spans = [
            _span(
                base,
                "user_turn",
                t + 900 * MS,
                t + 3700 * MS,
                None,
                **key,
                **{"lk.user_transcript": user},
            ),
            _span(base + 1, "user_speaking", t + 1 * SECOND, t + 3500 * MS, base, **key),
            _span(
                base + 2,
                "agent_turn",
                t + 3600 * MS,
                t + 8200 * MS,
                None,
                **key,
                **{"lk.response.text": agent, "lk.e2e_latency": 1.5, "lk.speech_id": speech},
            ),
            _span(base + 3, "llm_node", t + 3600 * MS, t + 4300 * MS, base + 2, **key),
            _span(
                base + 4,
                "llm_request",
                t + 3600 * MS,
                t + 4300 * MS,
                base + 3,
                **key,
                **{
                    "gen_ai.provider.name": "groq",
                    "gen_ai.request.model": "openai/gpt-oss-20b",
                    "gen_ai.usage.input_tokens": 100 + 40 * turn,
                    "gen_ai.usage.output_tokens": 40,
                    "lk.chat_ctx": json.dumps(history),
                },
            ),
            _span(base + 5, "tts_node", t + 4300 * MS, t + 8 * SECOND, base + 2, **key),
            _span(
                base + 6,
                "tts_request",
                t + 4300 * MS,
                t + 8 * SECOND,
                base + 5,
                **key,
                **{
                    "lk.tts_metrics": json.dumps(
                        {
                            "characters_count": len(agent),
                            "metadata": {"model_provider": "cartesia", "model_name": "sonic-3"},
                        }
                    )
                },
            ),
            _span(base + 7, "agent_speaking", t + 5 * SECOND, t + 8 * SECOND, base + 2, **key),
        ]
        requests.append(_request(spans, "livekit-agents"))
        history.append({"role": "assistant", "content": agent})
    requests.append(
        _request(
            [
                _span(
                    999_999,
                    "session_close",
                    START + TURNS * TURN,
                    START + TURNS * TURN + MS,
                    None,
                    **key,
                )
            ],
            "livekit-agents",
        )
    )
    return requests


def _runs(start_s: float, end_s: float) -> list[dict]:
    return [
        {
            "start_ms": int((turn * 10 + start_s) * 1000),
            "end_ms": int((turn * 10 + end_s) * 1000),
            "start_sigma_ms": 20,
            "end_sigma_ms": 20,
            "vad_start_ms": int((turn * 10 + start_s) * 1000),
            "vad_end_ms": int((turn * 10 + end_s) * 1000),
        }
        for turn in range(TURNS)
    ]


def _channel(index: int, speaker: str) -> dict:
    agent = speaker == "agent"
    zero_runs = (
        [
            {"start_ms": turn * 10_000 - 1_950 if turn else 0, "end_ms": turn * 10_000 + 4_950}
            for turn in range(TURNS)
        ]
        if agent
        else []
    )
    return {
        "index": index,
        "speaker": speaker,
        "noise_floor_dbfs": -66.0,
        "threshold_dbfs": -50.0,
        "speech_ms": TURNS * (3000 if agent else 2500),
        "runs": _runs(5.0, 8.0) if agent else _runs(1.0, 3.5),
        "zero_runs": zero_runs,
    }


def _result(speakers: list[str]) -> str:
    return json.dumps(
        {
            "algorithm_version": 1,
            "source": {"duration_ms": HOUR_MS, "channels": len(speakers)},
            "channels": [_channel(index, speaker) for index, speaker in enumerate(speakers)],
        }
    )


def _store_recording(
    store, recordings, recording_id: str, manifest: dict, speakers: list[str]
) -> None:
    recordings.create("default", RecordingManifest(recording_id=recording_id, **manifest))
    attempt = recordings.begin_upload("default", recording_id)
    try:
        sha256, size = put_stream(recordings.blobs, attempt.blob_key, [b"RIFF" * 16])
        recordings.finish_upload(
            "default", recording_id, attempt.blob_key, sha256=sha256, size=size, duration_ms=HOUR_MS
        )
    finally:
        recordings.end_upload("default", attempt.blob_key)
    with store._connect() as connection:  # fixture: a stored analysis with synthetic runs
        connection.execute(
            "UPDATE audio_analysis SET state = 'stored', algorithm_version = 1, "
            "peaks_blob_key = ?, result_json = ? WHERE recording_id = ?",
            (f"analysis/default/{recording_id}-peaks.dat", _result(speakers), recording_id),
        )
        connection.commit()


def _quantiles(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "p50_ms": round(statistics.median(ordered) * 1000, 1),
        "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)] * 1000, 1),
        "max_ms": round(ordered[-1] * 1000, 1),
    }


def bench_view(shape: str, *, reads: int) -> dict:
    from earshot.api import create_app
    from otlp_support import request, serve

    with tempfile.TemporaryDirectory(prefix="earshot-bench-view-") as directory:
        path = Path(directory)
        clock = ManualClock(wall=START + TURNS * TURN, monotonic=START)
        store = IncidentStore(path, clock=clock)
        calls = CallStore(store)
        blobs = LocalBlobStore(path / "blobs")
        recordings = RecordingStore(store, calls=calls, blobs=blobs)
        PriceStore(store).seed_builtins(BUILTIN_PRICES)
        requests = pipecat_requests() if shape == "pipecat" else livekit_requests()
        started = time.perf_counter()
        for item in requests:
            calls.record_spans("default", spans_from_request(item))
        recorded_s = time.perf_counter() - started
        call_id = calls.call_id("default", KEY, 0)
        base = {"session_key": KEY, "started_at_unix_nano": str(START)}
        if shape == "pipecat":
            for speaker in ("user", "agent"):
                _store_recording(
                    store,
                    recordings,
                    f"bench.{speaker}",
                    {**base, "speaker": speaker, "content_type": "audio/wav"},
                    [speaker],
                )
        else:
            _store_recording(
                store,
                recordings,
                "bench.stereo",
                {
                    **base,
                    "speaker": "mixed",
                    "channel_speakers": ("user", "agent"),
                    "content_type": "audio/ogg",
                },
                ["user", "agent"],
            )
        clock.advance(CallTimings().long_quiet_nano)
        started = time.perf_counter()
        closed = Closer(store, calls=calls, recordings=recordings).close_next()
        materialized_s = time.perf_counter() - started
        assert closed is not None and closed.revision == 1
        record, payload = store.get_artifact(f"{call_id}.r1", project_id="default")
        bundle_json = decode_incident_protobuf(payload).model_dump_json()
        truncated = bundle_json.count("recorder_capture_truncated")
        started = time.perf_counter()
        assert CallProjector(store).project_call("default", call_id)
        projected_s = time.perf_counter() - started
        with serve(create_app(store=store, blob_store=blobs)) as port:
            body = request(port, "GET", f"/v1/calls/{call_id}").body
            timings = []
            for _ in range(reads):
                begun = time.perf_counter()
                reply = request(port, "GET", f"/v1/calls/{call_id}")
                timings.append(time.perf_counter() - begun)
                assert reply.status == 200
        detail = json.loads(body)
        projection = detail["projection"]
        store.close()
        return {
            "shape": shape,
            "turns": TURNS,
            "spans": sum(
                len(s.spans) for r in requests for rs in r.resource_spans for s in rs.scope_spans
            ),
            "record_spans_s": round(recorded_s, 2),
            "materialize_s": round(materialized_s, 2),
            "bundle_bytes": record.size_bytes,
            "declared_truncations": truncated,
            "project_s": round(projected_s, 2),
            "turns_projected": len(projection["turns"]),
            "measured_turns": projection["measurable_turns"],
            "transcript_lines": len(projection["transcript"]),
            "month_1_transcript_lines": len(detail["transcript"]),
            "view_bytes": len(body),
            "view_gzip_bytes": len(gzip.compress(body)),
            "reads": reads,
            **_quantiles(timings),
        }


def _bulk_projections(store: IncidentStore, count: int, *, usage: list | None = None) -> None:
    """``count`` closed calls, each with a projection and 20 transcript lines (fixture rows)."""

    words = ["booking", "refund", "window", "invoice", "delivery", "upgrade", "cancel", "table"]
    with store._connect() as connection:
        connection.begin_write()
        for index in range(count):
            call_id = f"call_{index:032x}"
            first = START + index * SECOND
            connection.execute(
                "INSERT INTO calls(project_id, call_id, key_fingerprint, key_source, "
                "generation, state, first_evidence_unix_nano, last_evidence_unix_nano, dirty, "
                "latest_revision, created_unix_nano) "
                "VALUES ('default', ?, ?, 'trace_id', 0, 'closed', ?, ?, 0, 1, ?)",
                (call_id, f"{index:064x}", first, first, first),
            )
            connection.execute(
                "INSERT INTO call_revisions(project_id, call_id, revision, bundle_id, "
                "close_reason, finality, span_count, recording_count, created_unix_nano) "
                "VALUES ('default', ?, 1, ?, 'end_signal', 'final', 0, 0, ?)",
                (call_id, f"{call_id}.r1", first),
            )
            connection.execute(
                "INSERT INTO call_projections(project_id, call_id, revision, source_seq, "
                "price_version, builtin_price_version, projection_version, input_digest, agent, "
                "outcome, turn_count, measurable_turns, reported_latency_p50_ms, "
                "audible_gap_p50_ms, cost_lower_bound_nano_usd, cost_unknown_items, "
                "analysis_state, updated_unix_nano, usage_json, detail_json) "
                "VALUES ('default', ?, 1, 0, 0, 1, ?, ?, ?, 'completed', 3, 2, ?, ?, ?, 0, "
                "'stored', 1, ?, ?)",
                (
                    call_id,
                    CALL_PROJECTION_VERSION,
                    f"{index:064x}",
                    f"agent-{index % 5}",
                    800 + index % 1000,
                    900 + index % 1200,
                    1000 + index,
                    compact_json(usage or []),
                    compact_json({"cost_items": []}),
                ),
            )
            connection.executemany(
                "INSERT INTO call_transcripts(project_id, call_id, line_no, speaker, timing, "
                "start_ms, end_ms, text) VALUES ('default', ?, ?, ?, 'measured', ?, ?, ?)",
                [
                    (
                        call_id,
                        line,
                        "user" if line % 2 == 0 else "agent",
                        line * 1000,
                        line * 1000 + 900,
                        f"line {line} about the {words[(index + line) % len(words)]} "
                        + ("and the rare word zanzibar " if index % 997 == 0 and line == 3 else "")
                        + "for this customer call, please",
                    )
                    for line in range(20)
                ],
            )
        connection.commit()


def bench_search(calls: int, *, reads: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="earshot-bench-search-") as directory:
        store = IncidentStore(directory)
        PriceStore(store).seed_builtins(BUILTIN_PRICES)
        _bulk_projections(store, calls)
        reads_ = CallReads(store)
        cases = {
            "rare word (q=zanzibar)": CallFilters(q="zanzibar"),
            "common word (q=window)": CallFilters(q="window"),
            "q + agent + latency": CallFilters(
                q="refund", agent="agent-3", max_reported_latency_ms=1500
            ),
            "max_cost (excluded_incomplete count)": CallFilters(max_cost_nano_usd=5000),
        }
        result = {"calls": calls, "transcript_lines": calls * 20}
        for name, filters in cases.items():
            timings = []
            for _ in range(reads):
                begun = time.perf_counter()
                page = reads_.list_calls("default", filters=filters, limit=50)
                timings.append(time.perf_counter() - begun)
            result[name] = {"matched_on_page": len(page.items), **_quantiles(timings)}
        store.close()
        return result


def bench_reprice(calls: int) -> dict:
    sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "tests"))
    usage = [
        {
            "kind": "reported",
            "operation": "llm",
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "unit": "input_token",
            "quantity_milli": 495_000,
            "span": None,
        },
        {
            "kind": "reported",
            "operation": "llm",
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "unit": "output_token",
            "quantity_milli": 319_000,
            "span": None,
        },
        {
            "kind": "reported",
            "operation": "tts",
            "provider": "cartesia",
            "model": "sonic-3",
            "unit": "character",
            "quantity_milli": 118_000,
            "span": None,
        },
        {
            "kind": "estimate",
            "operation": "stt",
            "provider": "deepgram",
            "model": "nova-3-general",
            "unit": "audio_second",
            "quantity_milli": 32_486,
            "span": None,
        },
    ]
    with tempfile.TemporaryDirectory(prefix="earshot-bench-reprice-") as directory:
        store = IncidentStore(directory)
        PriceStore(store).seed_builtins(BUILTIN_PRICES)
        _bulk_projections(store, calls, usage=usage)
        projector = CallProjector(store)
        PriceStore(store).add(
            "default",
            NewPrice("groq", "openai/gpt-oss-20b", "output_token", 400, 1, 0, None, None),
        )
        started = time.perf_counter()
        repriced = 0
        while True:
            done = projector.run_pending(limit=500)
            repriced += done.projected
            if not done.more:
                break
        elapsed = time.perf_counter() - started
        store.close()
        return {
            "calls": calls,
            "repriced": repriced,
            "seconds": round(elapsed, 2),
            "extrapolated_100k_minutes": round(elapsed * (100_000 / calls) / 60, 2),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("benches", nargs="+", choices=("view", "search", "reprice"))
    parser.add_argument("--reads", type=int, default=50)
    parser.add_argument("--calls", type=int, default=10_000)
    arguments = parser.parse_args()
    for bench in arguments.benches:
        if bench == "view":
            for shape in ("pipecat", "livekit"):
                print(json.dumps(bench_view(shape, reads=arguments.reads)), flush=True)
        elif bench == "search":
            print(json.dumps(bench_search(arguments.calls, reads=arguments.reads)), flush=True)
        else:
            print(json.dumps(bench_reprice(arguments.calls)), flush=True)


if __name__ == "__main__":
    main()
