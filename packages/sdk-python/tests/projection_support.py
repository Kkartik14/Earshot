"""Call projection rows for tests that read calls (the call view API's shapes).

``insert_projection`` writes one ``call_projections`` row, with its ``call_tags``
and ``call_transcripts`` rows, through a raw catalog connection: the rows the
projection job writes in its one mutation. It is fixture data on the real
catalog, not a fake: the reads under test run their production SQL over it. For
tests that need projection values the real job cannot be steered to (exact list
filters, freshness transitions). It seeds the built-in prices first and replaces
any projection the server's own job already wrote, so a served app leaves the
fixture row alone while the call does not change (its stamps are current).

``example_detail`` is a projection's detail as ``call_projections.detail_json``
holds it: the members of the call view's ``projection`` that are not summary
columns. Its values follow the Pipecat 1.5 fixture's measured turns:
turn 1 not measurable behind a recorder discontinuity, turns 2 and 3
measured. They are illustrative, not the goldens the projection job asserts.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from earshot.call_projection import CALL_PROJECTION_VERSION
from earshot.pricing_defaults import BUILTIN_PRICES
from earshot.storage_prices import seed_builtin_prices
from store_support import catalog

# A projection's summary columns, as a projected Pipecat call has them.
SUMMARY_COLUMNS: dict[str, Any] = {
    "agent": "booking-agent",
    "framework": "pipecat",
    "outcome": "completed",
    "started_unix_nano": None,
    "duration_ms": 32486,
    "turn_count": 3,
    "measurable_turns": 2,
    "reported_latency_p50_ms": 1352,
    "reported_latency_max_ms": 1800,
    "audible_gap_p50_ms": 1479,
    "audible_gap_max_ms": 1510,
    "talk_time_user_ms": 8210,
    "talk_time_agent_ms": 8130,
    "overlap_ms": 0,
    "barge_in_count": 0,
    "talk_over_count": 0,
    "long_silence_count": 1,
    "dead_air_ms": 3080,
    "cost_lower_bound_nano_usd": 3_672_825,
    "cost_estimated_nano_usd": 4_169_037,
    "cost_unknown_items": 2,
    "analysis_state": "stored",
}
TRACE = "8d6a3c4f0b1e2d3c4b5a69788796a5b4"


def _span(span_id: str) -> dict[str, str]:
    return {"trace_id": TRACE, "span_id": span_id}


def _turn_span(span_id: str, name: str, role: str, start_ms: int, end_ms: int) -> dict[str, Any]:
    return {**_span(span_id), "name": name, "role": role, "status": "ok", **_ms(start_ms, end_ms)}


def _ms(start_ms: int, end_ms: int) -> dict[str, int]:
    return {"start_ms": start_ms, "end_ms": end_ms}


def _cost(
    status: str,
    operation: str,
    provider: str,
    model: str,
    unit: str | None = None,
    quantity: float | None = None,
    price_id: int | None = None,
    price_nano_usd: int | None = None,
    per_quantity: int | None = None,
) -> dict[str, Any]:
    """A cost item; a priced or estimated one costs its quantity at the price,
    rounded half up to a whole nano-USD."""

    cost = None
    if price_nano_usd is not None and quantity is not None and per_quantity is not None:
        cost = int(Decimal(str(quantity)) * price_nano_usd / per_quantity + Decimal("0.5"))
    return {
        "status": status,
        "operation": operation,
        "provider": provider,
        "model": model,
        "unit": unit,
        "quantity": quantity,
        "price_id": price_id,
        "price_nano_usd": price_nano_usd,
        "per_quantity": per_quantity,
        "cost_nano_usd": cost,
        "span": None,
    }


def example_detail() -> dict[str, Any]:
    """A projection's ``detail_json`` members (the frozen call view shape)."""

    gap = {
        "status": "measured",
        "value_ms": None,
        "uncertainty_ms": None,
        "bound_ms": None,
        "user_end_ms": None,
        "agent_start_ms": None,
        "reason": None,
        "basis": "anchors_checked",
    }
    return {
        "axis_origin_unix_nano": "1800000000000000000",
        "axis_origin": "recording_start",
        "media": [
            {
                "source": "recording",
                "recording_ids": ["conv-1"],
                "content_path": "/v1/recordings/conv-1/content",
                "content_type": "audio/wav",
                "peaks_path": "/v1/recordings/conv-1/peaks",
                "offset_ms": 0,
                "duration_ms": 32486,
                "channels": [{"index": 0, "speaker": "user"}, {"index": 1, "speaker": "agent"}],
                "analysis_state": "stored",
                "analysis_error_code": None,
            }
        ],
        "turns": [
            {
                "index": 0,
                "turn_id": "turn-1",
                **_ms(1310, 7420),
                "reported_latency_ms": 1800,
                "audible_gap": {
                    **gap,
                    "status": "not_measurable",
                    "uncertainty_ms": 20,
                    "bound_ms": 3080,
                    "user_end_ms": 3880,
                    "agent_start_ms": 6960,
                    "reason": "recorder_timeline_discontinuity",
                },
                "spans": [
                    _turn_span("1000000000000001", "stt", "stt", 1100, 3900),
                    _turn_span("1000000000000002", "llm", "llm", 3950, 4810),
                    _turn_span("1000000000000003", "tts", "tts", 4820, 6400),
                ],
            },
            {
                "index": 1,
                "turn_id": "turn-2",
                **_ms(11810, 19710),
                "reported_latency_ms": 1352,
                "audible_gap": {
                    **gap,
                    "value_ms": 1510,
                    "uncertainty_ms": 30,
                    "user_end_ms": 15240,
                    "agent_start_ms": 16750,
                },
                "spans": [_turn_span("1000000000000004", "llm", "llm", 15300, 16100)],
            },
            {
                "index": 2,
                "turn_id": "turn-3",
                **_ms(22590, 28770),
                "reported_latency_ms": 1334,
                "audible_gap": {
                    **gap,
                    "value_ms": 1448,
                    "uncertainty_ms": 30,
                    "user_end_ms": 25472,
                    "agent_start_ms": 26920,
                },
                "spans": [_turn_span("1000000000000005", "llm", "llm", 25500, 26300)],
            },
        ],
        "signals": {
            "one_timeline": True,
            "talk_time_user_ms": 8210,
            "talk_time_agent_ms": 8130,
            "overlap_ms": 0,
            "barge_in_count": 0,
            "talk_over_count": 0,
            "long_silence_count": 1,
            "dead_air_ms": 3080,
        },
        "cost_items": [
            _cost("priced", "llm", "groq", "openai/gpt-oss-20b", "input_token", 495, 7, 75, 1),
            _cost("priced", "llm", "groq", "openai/gpt-oss-20b", "output_token", 319, 8, 300, 1),
            {
                **_cost("not_reported", "llm", "groq", "openai/gpt-oss-20b"),
                "span": _span("1000000000000006"),
            },
            {
                **_cost("not_reported", "llm", "groq", "openai/gpt-oss-20b"),
                "span": _span("1000000000000007"),
            },
            _cost("priced", "tts", "cartesia", "sonic-3", "character", 118, 12, 30_000, 1),
            _cost(
                "estimated",
                "stt",
                "deepgram",
                "nova-3-general",
                "audio_second",
                32.486,
                3,
                7_700_000,
                60,
            ),
        ],
        "transcript": [
            {
                "line_no": 0,
                "speaker": "user",
                "text": "Hi. I would like to book a table for two people tomorrow evening.",
                "timing": "measured",
                **_ms(1310, 3880),
                "alignment_uncertain": False,
                "span": _span("1000000000000001"),
            },
            {
                "line_no": 1,
                "speaker": "agent",
                "text": "Sure, what time would you like to come in tomorrow evening?",
                "timing": "measured",
                **_ms(6960, 9920),
                "alignment_uncertain": True,
                "span": _span("1000000000000002"),
            },
        ],
    }


def _scalar(connection: Any, sql: str, parameters: tuple[Any, ...]) -> int:
    return int(connection.execute(sql, parameters).fetchone()[0])


def insert_projection(
    path: str | Path,
    call_id: str,
    *,
    project_id: str = "default",
    revision: int = 1,
    columns: dict[str, Any] | None = None,
    tags: Sequence[str] = (),
    transcript: Sequence[tuple[str, str]] = (),
    detail: dict[str, Any] | None = None,
    stamps: dict[str, int] | None = None,
) -> None:
    """Write a current projection of ``call_id`` (its stamps equal the call's and
    the prices' versions now, unless ``stamps`` overrides them), with ``tags`` and
    ``transcript`` lines ``(speaker, text)``, one second apart."""

    values = {**SUMMARY_COLUMNS, **(columns or {})}
    with catalog(path) as connection:
        seed_builtin_prices(connection, BUILTIN_PRICES, now=1)
        connection.execute(
            "DELETE FROM call_projections WHERE project_id = ? AND call_id = ?",
            (project_id, call_id),
        )
        current = {
            "source_seq": _scalar(
                connection,
                "SELECT projection_seq FROM calls WHERE project_id = ? AND call_id = ?",
                (project_id, call_id),
            ),
            "price_version": _scalar(
                connection,
                "SELECT price_version FROM projects WHERE project_id = ?",
                (project_id,),
            ),
            "builtin_price_version": _scalar(
                connection, "SELECT version FROM builtin_price_version WHERE singleton = 1", ()
            ),
            "projection_version": CALL_PROJECTION_VERSION,
            **(stamps or {}),
        }
        row = {
            "project_id": project_id,
            "call_id": call_id,
            "revision": revision,
            **current,
            "input_digest": "0" * 64,
            **values,
            "provider_cost_nano_usd": None,
            "provider_cost_basis": None,
            "updated_unix_nano": 1,
            "usage_json": "[]",
            "detail_json": json.dumps(
                example_detail() if detail is None else detail, separators=(",", ":")
            ),
        }
        connection.execute(
            f"INSERT INTO call_projections({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        for tag in tags:
            connection.execute(
                "INSERT INTO call_tags(project_id, call_id, tag) VALUES (?, ?, ?)",
                (project_id, call_id, tag),
            )
        for line_no, (speaker, text) in enumerate(transcript):
            connection.execute(
                "INSERT INTO call_transcripts(project_id, call_id, line_no, speaker, timing, "
                "start_ms, end_ms, text) VALUES (?, ?, ?, ?, 'measured', ?, ?, ?)",
                (project_id, call_id, line_no, speaker, line_no * 1000, line_no * 1000 + 900, text),
            )
