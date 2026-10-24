"""Conformance and the LiveKit ``metadata_only`` search: a real LiveKit 1.8 call, real server.

**Conformance.** The real capture is sent over HTTP to a real server
(``POST /v1/traces``); its maintenance loop's closer turns the call into revision 1.
The same spans, as the OpenTelemetry SDK hands them to a span processor, go straight
into ``LiveKitAdapter.consume_span`` on a recorder with the revision's capture policy
and clock domain. Both bundles project the same turn facts.

**Privacy.** Under ``metadata_only`` no LiveKit content reaches storage: not the
session key, the user's words, the agent's played or discarded replies, the system
prompt, or the room name. The search covers every byte of the catalog, every file
under the store's directory, and the revision itself; the same search under
``full`` finds every needle (the control).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from adapter_conformance import assert_capture_conforms, assert_same_turn_facts
from captured_livekit_otlp import (
    LIVEKIT_SESSION_KEY,
    livekit_requests,
    livekit_spans,
    readable_span,
)
from earshot.adapters.livekit import LiveKitAdapter
from earshot.api import create_app
from earshot.call_materializer import call_clock_domain, capture_policy
from earshot.clock import ManualClock
from earshot.codec import decode_incident_protobuf, encode_incident_json
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.storage_calls import CallTimings
from otlp_support import post_otlp, serve
from store_support import catalog_rows, make_store, stored_needles

pytestmark = pytest.mark.integration

USER_WORDS = (
    b"Hi. I would like to book a table for two people tomorrow evening.",
    b"Seven thirty works for us. Could we sit somewhere quiet near the window?",
    b"Perfect. That is everything I needed. Thank you very much. Goodbye.",
)
PLAYED = (
    b"Sure, I'd be happy to arrange that.",
    b"near the window. We'll reserve a quiet spot for you.",
    b"You're welcome! See you tomorrow at 7:30. Have a great day!",
)
DISCARDED = (
    b"Looking forward to seeing you!",
    b"All set. See you tomorrow at 7:30. Enjoy your evening!",
)
NEEDLES = [
    LIVEKIT_SESSION_KEY.encode(),
    *USER_WORDS,
    *PLAYED,
    *DISCARDED,
    b"You are the host at a small restaurant",  # the system prompt
    # The agent session's room name (the other is a prefix of service.name), as a span
    # stores it: a 7-byte protobuf string. Bare, the word also matches the built-in
    # prices' source URL (console.groq.com), which is no call content.
    b"\x07console",
]


def _served_revision(tmp_path: Path, preset: str):
    """Replay the capture into a real server and wait for revision 1: (store, call, bundle)."""

    store = make_store(tmp_path)
    store.set_capture_preset("default", preset)
    app = create_app(store=store, call_timings=CallTimings(end_grace_seconds=0.05))
    with serve(app) as port:
        for request in livekit_requests():
            assert post_otlp(port, request.SerializeToString()).status == 200
        deadline = time.monotonic() + 15
        while not catalog_rows(tmp_path, "SELECT 1 FROM call_revisions"):
            assert time.monotonic() < deadline, "the server's closer made no revision"
            time.sleep(0.05)
    [(call_id, bundle_id, finality)] = catalog_rows(
        tmp_path, "SELECT call_id, bundle_id, finality FROM call_revisions"
    )
    assert finality == "final"
    payload = store.get_artifact(bundle_id)[1]
    return store, call_id, decode_incident_protobuf(payload), payload


def _in_process_bundle(call_id: str):
    spans = livekit_spans()
    recorder = IncidentRecorder(
        session_id="in-process",
        bundle_id="in-process-call",
        clock=ManualClock(wall=min(span.span.start_time_unix_nano for span in spans)),
        config=RecorderConfig(
            clock_domain_id=call_clock_domain(call_id), capture_policy=capture_policy("full")
        ),
    )
    adapter = LiveKitAdapter(recorder)
    for span in spans:  # in export order, as a span processor sees them end
        adapter.consume_span(readable_span(span))
    return recorder.close()


def test_the_server_path_derives_the_turn_facts_the_in_process_adapter_derives(tmp_path) -> None:
    store, call_id, served, _ = _served_revision(tmp_path, "full")

    in_process = assert_capture_conforms(
        lambda: _in_process_bundle(call_id), require_omission=False
    )

    assert [adapter.name for adapter in served.profile.manifest.adapters] == ["earshot.livekit"]
    assert_same_turn_facts(served, in_process)
    assert len(store.list_turn_facts(session_id=call_id)) == 3


def _files_containing(root: Path, needles: list[bytes]) -> set[bytes]:
    found: set[bytes] = set()
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            found |= {needle for needle in needles if needle in data}
    return found


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_metadata_only_stores_no_livekit_content_anywhere(tmp_path, preset) -> None:
    store, _, served, payload = _served_revision(tmp_path, preset)
    revision = payload + encode_incident_json(served)
    store.close()  # the search reads the database files, WAL included

    in_catalog = stored_needles(tmp_path, NEEDLES)
    on_disk = _files_containing(tmp_path, NEEDLES)
    in_revision = {needle for needle in NEEDLES if needle in revision}

    if preset == "metadata_only":
        assert in_catalog == on_disk == in_revision == set()
        assert catalog_rows(tmp_path, "SELECT session_key, span_count FROM calls") == [(None, 58)]
    else:  # the control: the same search finds every needle when it is stored
        assert in_catalog == set(NEEDLES)
        # The revision keeps what was played and said, never LLM input history or the
        # generations LiveKit discarded as text of a turn (they stay in llm spans).
        assert set(PLAYED) | set(USER_WORDS) <= in_revision
