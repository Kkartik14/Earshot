"""Phase 3 gate: a server-owned browser call is ONE bundle with two clock domains.

The application already records its server-side STT / LLM / TTS stages on its own
:class:`~earshot.pipeline.PipelineSession`. :class:`BrowserCaptureSource` authors
the browser's drained WebRTC / audio-graph telemetry into that SAME recorder, so
the browser evidence and the server/model/TTS/render evidence land in one
:class:`~earshot.contract.IncidentBundle`. These tests hold the honesty invariants
that make that composition trustworthy:

* one ``close()`` yields one bundle carrying a browser-domain fact AND a
  server-domain fact, and it validates;
* no cross-clock relation is invented -- a browser render event and a server event
  stay honestly incomparable until a real calibration is declared, at which point
  the existing alignment path makes the latency *estimated*;
* browser facts never advance the server-clock turn extent;
* the WebRTC carry threads across batches into the shared recorder (a
  boundary-spanning reconnect is recovered), and an out-of-order batch is declared
  unobserved by the engine rather than reordered;
* the SDK path drops byte-for-byte the same hostile members the HTTP path drops.
"""

from __future__ import annotations

from typing import Any

import pytest

import earshot
from earshot.analysis import _ClockAligner, comparable_delta
from earshot.capture import BrowserCaptureReport, BrowserCaptureSource
from earshot.clock import ManualClock
from earshot.codec import encode_incident_json
from earshot.contract import ClockRelation
from earshot.engines.webrtc import apply_webrtc_stats
from earshot.validation import validate_incident

pytestmark = pytest.mark.unit

# A fixed server wall clock and browser wall origin keep every derived incident a
# deterministic function of the authored facts alone.
START = 1_800_000_000_000_000_000
BROWSER_WALL_ORIGIN_MS = 1_700_000_000_000
CLOCK_ID = "clk_browsercall"

CLOCK_DOMAIN = {
    "id": CLOCK_ID,
    "kind": "browser_monotonic",
    "unit": "ms",
    "uncertaintyMs": 1,
    "wallOriginMs": BROWSER_WALL_ORIGIN_MS,
}


def _inbound(**members: Any) -> dict[str, Any]:
    return {"type": "inbound-rtp", "kind": "audio", **members}


# Two drains of one call. The transport drops in the first and recovers in the
# second, so the reconnect is observable ONLY if the carry threads the outstanding
# down-state across the boundary. The packet-loss delta likewise spans the two.
BATCH_1 = {
    "clockDomain": CLOCK_DOMAIN,
    "snapshots": [
        {
            "timestamp_ms": 1000,
            "stats": {
                "IT": _inbound(packetsReceived=1000, packetsLost=5, jitter=0.010),
                "T": {"type": "transport", "iceState": "disconnected"},
            },
        }
    ],
    "deviceEvents": [{"type": "permission", "timestamp_ms": 1100, "state": "denied"}],
    "coverage": [
        {
            "signal": "webrtc.snapshots",
            "availability": "partial",
            "reason": "buffer_overflow_oldest_dropped",
            "droppedCount": 3,
        }
    ],
}

BATCH_2 = {
    "clockDomain": CLOCK_DOMAIN,
    "snapshots": [
        {
            "timestamp_ms": 2000,
            "stats": {
                "IT": _inbound(packetsReceived=1100, packetsLost=200, jitter=0.050),
                "T": {"type": "transport", "iceState": "connected"},
            },
        }
    ],
}


def _session():
    return earshot.pipeline(
        session_id="t1-owned-call",
        bundle_id="bundle-t1-owned-call",
        clock=ManualClock(wall=START, monotonic=0),
    )


def _record_server_stages(session) -> None:
    """The app's own server-side pipeline: model stages in the server clock domain."""

    with session.turn("conversation") as turn:
        turn.stt("deepgram", ttfb_ms=12.0, final_ms=40.0)
        turn.llm("openai", ttft_ms=50.0)
        turn.tts("cartesia", ttfb_ms=8.0, first_audio_ms=30.0)


def _feed_browser(session, source: BrowserCaptureSource, batches) -> list[BrowserCaptureReport]:
    reports: list[BrowserCaptureReport] = []
    with session.turn("browser-capture") as turn:
        for batch in batches:
            reports.append(source.apply(turn, batch))
    return reports


def _server_domain_point(bundle, server_domain: str):
    for operation in bundle.profile.operations:
        if operation.started_at.clock_domain_id == server_domain:
            return operation.started_at
    raise AssertionError("no server-domain operation found")


def _browser_domain_point(bundle):
    for sample in bundle.profile.quality_samples:
        if sample.sample_window.start.clock_domain_id == CLOCK_ID:
            return sample.sample_window.start
    raise AssertionError("no browser-domain quality sample found")


def _browser_measurements(bundle) -> dict[str, float]:
    values: dict[str, float] = {}
    for sample in bundle.profile.quality_samples:
        if sample.sample_window.start.clock_domain_id != CLOCK_ID:
            continue
        for measurement in sample.measurements:
            values[measurement.name] = measurement.value
    return values


def _coverage(bundle) -> set[tuple[str, str, str | None]]:
    return {(note.signal, note.availability, note.reason) for note in bundle.profile.coverage}


def test_one_call_one_bundle_two_clock_domains() -> None:
    session = _session()
    server_domain = session.clock_domain_id
    _record_server_stages(session)
    source = BrowserCaptureSource()
    _feed_browser(session, source, [BATCH_1, BATCH_2])

    bundle = session.close()

    # One close, one bundle, and it validates.
    assert validate_incident(bundle).ok, validate_incident(bundle)

    # Both clock domains coexist in the ONE bundle.
    domains = {domain.clock_domain_id for domain in bundle.profile.clock_domains}
    assert server_domain in domains
    assert CLOCK_ID in domains

    # A server-domain fact (a model stage) AND a browser-domain fact (a derived
    # WebRTC scalar) are both present -- authored into the same recorder.
    server_ops = [
        op for op in bundle.profile.operations if op.started_at.clock_domain_id == server_domain
    ]
    assert any(op.operation_name in {"stt", "llm", "tts"} for op in server_ops)
    assert "packet_loss_ratio" in _browser_measurements(bundle)

    # No relation between the two clocks was invented.
    assert bundle.profile.clock_relations == ()


def test_a_declared_calibration_makes_cross_clock_latency_estimated() -> None:
    session = _session()
    server_domain = session.clock_domain_id
    _record_server_stages(session)
    source = BrowserCaptureSource()
    with session.turn("browser-capture") as turn:
        source.apply(turn, BATCH_1)
        source.apply(turn, BATCH_2)
    # The app declares a REAL calibration between the browser and server clocks.
    # The offset dwarfs the browser/server wall gap so the aligned instant lands
    # after the server anchor (a non-negative, honestly estimated latency).
    relation = ClockRelation(
        relation_id="rel-browser-server",
        from_clock_domain_id=CLOCK_ID,
        to_clock_domain_id=server_domain,
        offset_nano="200000000000000000",
        uncertainty_nano="500",
        method="browser_http_roundtrip",
    )
    session.register_clock_relation(relation)
    bundle = session.close()

    assert validate_incident(bundle).ok, validate_incident(bundle)
    assert bundle.profile.clock_relations == (relation,)

    server_point = _server_domain_point(bundle, server_domain)
    browser_point = _browser_domain_point(bundle)
    aligner = _ClockAligner(bundle.profile.clock_relations)

    # With no relation it is still refused; with the declared calibration it is
    # available but only ever ESTIMATED, and it carries the calibration's own bound.
    assert comparable_delta(server_point, browser_point).limitation == "cross_clock_domain"
    aligned = comparable_delta(server_point, browser_point, aligner)
    assert aligned.availability == "available"
    assert aligned.confidence == "estimated"
    assert aligned.nanoseconds is not None and aligned.nanoseconds >= 0
    assert aligned.uncertainty is not None and aligned.uncertainty >= 500


def test_browser_facts_never_advance_the_server_clock_turn_extent() -> None:
    session = _session()
    source = BrowserCaptureSource()
    with session.turn("browser-capture") as turn:
        # Browser timestamps are 1000-2000 ms, but they carry their own clock
        # domain, so the server-clock turn extent stays at zero -- nothing here was
        # a server-clock observation.
        source.apply(turn, BATCH_1)
        source.apply(turn, BATCH_2)
        assert turn._max_ms == 0.0
    bundle = session.close()
    assert validate_incident(bundle).ok


def test_the_carry_recovers_a_boundary_spanning_reconnect() -> None:
    session = _session()
    source = BrowserCaptureSource()
    first, second = _feed_browser(session, source, [BATCH_1, BATCH_2])
    bundle = session.close()

    # The down-state was seen in batch 1 and the recovery in batch 2. Only the
    # threaded carry makes that reconnect observable across the drain boundary.
    assert second.webrtc.reconnected is True
    # And the boundary packet-loss delta (received +100, lost +195) was differenced
    # exactly once, at the later snapshot's coordinate.
    assert _browser_measurements(bundle)["packet_loss_ratio"] == pytest.approx(195 / 295)

    # Contrast: batch 2 analysed alone -- without the carry -- never saw the drop,
    # so it reports no reconnect. That is the evidence the carry recovers.
    assert first.webrtc.reconnected is False
    fresh = apply_webrtc_stats(_Discard(), BATCH_2["snapshots"])
    assert fresh.reconnected is False


class _Discard:
    """A sink that accepts and forgets every fact -- for a carry-free control run."""

    def record_measurement(self, *a: Any, **k: Any) -> None: ...
    def record_event(self, *a: Any, **k: Any) -> None: ...
    def record_operation(self, *a: Any, **k: Any) -> None: ...
    def record_coverage(self, *a: Any, **k: Any) -> None: ...
    def record_omission(self, *a: Any, **k: Any) -> None: ...
    def register_clock_domain(self, *a: Any, **k: Any) -> None: ...


def test_an_out_of_order_batch_is_declared_unobserved_not_reordered() -> None:
    session = _session()
    source = BrowserCaptureSource()
    # A second batch whose snapshot precedes the first makes that step
    # non-monotonic. The engine drops the interval and notes coverage -- the same
    # honest handling a non-monotonic snapshot inside one batch gets. It is never
    # silently reordered into a positive delta.
    earlier = {
        "clockDomain": CLOCK_DOMAIN,
        "snapshots": [
            {
                "timestamp_ms": 500,
                "stats": {"IT": _inbound(packetsReceived=2000, packetsLost=300)},
            }
        ],
    }
    with session.turn("browser-capture") as turn:
        source.apply(turn, BATCH_1)
        report = source.apply(turn, earlier)
    bundle = session.close()

    assert validate_incident(bundle).ok
    reasons = {(note.signal, note.reason) for note in report.webrtc.coverage}
    assert ("webrtc.snapshot_order", "non_monotonic_snapshot") in reasons
    assert ("webrtc.snapshot_order", "not_observed", "non_monotonic_snapshot") in _coverage(bundle)


def test_a_batch_from_a_different_clock_domain_is_refused() -> None:
    session = _session()
    source = BrowserCaptureSource()
    other = {**BATCH_2, "clockDomain": {**CLOCK_DOMAIN, "id": "clk_other_call"}}
    with session.turn("browser-capture") as turn:
        source.apply(turn, BATCH_1)
        # A different clock-domain id is a different continuous timeline (a reload
        # mints a new one). Splicing it in would fabricate continuity, so it is a
        # loud refusal, not a silent merge.
        with pytest.raises(ValueError, match="different clock domain"):
            source.apply(turn, other)


def test_client_and_rejection_coverage_are_authored_with_counts() -> None:
    session = _session()
    _feed_browser(session, BrowserCaptureSource(), [BATCH_1, BATCH_2])
    bundle = session.close()

    # The browser's own coverage is namespaced so it can never mask a server note,
    # and its counted loss survives into the artifact as a real dropped_count.
    note = next(
        n
        for n in bundle.profile.coverage
        if n.signal == "browser.webrtc.snapshots" and n.reason == "buffer_overflow_oldest_dropped"
    )
    assert note.dropped_count == 3


CERTIFICATE_SENTINEL = "SENTINELbase64Certificate=="
FINGERPRINT_SENTINEL = "AA:BB:CC:SENTINELFINGERPRINT"
UFRAG_SENTINEL = "SENTINELufrag"
ADDRESS_SENTINEL = "203.0.113.77"
LABEL_SENTINEL = "SENTINEL Headset (Bluetooth)"

HOSTILE_BATCH = {
    "clockDomain": CLOCK_DOMAIN,
    "snapshots": [
        {
            "timestamp_ms": 1000,
            "stats": {
                "CERT": {
                    "type": "certificate",
                    "base64Certificate": CERTIFICATE_SENTINEL,
                    "fingerprint": FINGERPRINT_SENTINEL,
                },
                "LC": {
                    "type": "local-candidate",
                    "networkType": "wifi",
                    "address": ADDRESS_SENTINEL,
                    "ip": ADDRESS_SENTINEL,
                    "port": 51234,
                    "url": f"stun:{ADDRESS_SENTINEL}:3478",
                    "usernameFragment": UFRAG_SENTINEL,
                    "relatedAddress": ADDRESS_SENTINEL,
                },
                "IT": _inbound(packetsReceived=10, packetsLost=0),
            },
        }
    ],
    "deviceEvents": [
        {
            "type": "permission",
            "timestamp_ms": 1000,
            "state": "granted",
            "label": LABEL_SENTINEL,
            "deviceId": LABEL_SENTINEL,
            "deviceHash": LABEL_SENTINEL,
        }
    ],
}


def test_the_sdk_path_drops_the_same_hostile_members_as_the_http_path() -> None:
    session = _session()
    source = BrowserCaptureSource()
    with session.turn("browser-capture") as turn:
        report = source.apply(turn, HOSTILE_BATCH)
    bundle = session.close()

    # The counts match the HTTP allowlist exactly: the certificate stat dropped
    # whole, six host-identifying candidate members, three device members.
    assert report.dropped_stats == 1
    assert report.dropped_stat_members == 6
    assert report.dropped_device_members == 3

    # None of the host-identifying material reaches the artifact.
    served = encode_incident_json(bundle).decode("utf-8")
    for sentinel in (
        CERTIFICATE_SENTINEL,
        FINGERPRINT_SENTINEL,
        UFRAG_SENTINEL,
        ADDRESS_SENTINEL,
        LABEL_SENTINEL,
    ):
        assert sentinel not in served

    # The refusals are ledgered as coverage, exactly as the HTTP path records them.
    coverage = _coverage(bundle)
    assert ("capture.stats", "partial", "non_governed_stat_dropped") in coverage
    assert ("capture.stat_members", "partial", "non_governed_member_dropped") in coverage
    assert ("capture.device_event_members", "partial", "non_governed_member_dropped") in coverage
