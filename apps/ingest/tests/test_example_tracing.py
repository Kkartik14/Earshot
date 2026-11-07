"""The Pipecat example's in-memory span counter participates in provider flushes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pipecat.audio.vad.silero")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "pipecat_otlp"
sys.path.insert(0, str(EXAMPLE))

from run_call import SpanCounter  # noqa: E402


def test_span_counter_does_not_fail_provider_flush() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(SpanCounter())

    with provider.get_tracer(__name__).start_as_current_span("example-call"):
        pass

    try:
        assert provider.force_flush() is True
        assert len(exporter.get_finished_spans()) == 1
    finally:
        provider.shutdown()
