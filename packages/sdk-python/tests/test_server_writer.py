"""The server-side journal collector releases entries after live handoff."""

from __future__ import annotations

import gc
import weakref

import pytest

from earshot.checkpoint.server_writer import ServerJournalWriter
from earshot.privacy import CaptureClass, CapturePolicy

pytestmark = pytest.mark.unit


def test_taken_journal_entries_are_released_without_losing_sequence() -> None:
    writer = ServerJournalWriter()
    writer.open_journal(
        producer_name="earshot.test",
        producer_version="1",
        bundle_id="bundle-1",
        session_id="session-1",
        clock_domain_id="clock-1",
        started_wall=1,
        started_mono=1,
        manual_trace_id="0" * 32,
        capture_policy=CapturePolicy.metadata_only(),
        max_records=100,
        max_capture_bytes=1024,
        max_raw_otlp_bytes=0,
        max_value_bytes=1024,
    )
    assert len(writer.take_new()) == 1

    writer.append_limit(
        reason="test",
        kind="event",
        capture_class=CaptureClass.METADATA,
        estimated_bytes=1,
        whole_record=False,
        freeze=False,
    )
    handed_off = writer.take_new()
    reference = weakref.ref(handed_off[0])
    assert writer.take_new() == []
    del handed_off
    gc.collect()

    assert reference() is None
    assert writer.status().last_sequence == 2
