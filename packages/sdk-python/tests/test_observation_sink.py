"""Gate: the capture boundary is a protocol, not the concrete pipeline recorder.

A browser, native, or backend collector must be able to receive engine-derived
facts without a ``PipelineSession`` anywhere in the process. These tests author
through a sink that implements *only* :class:`~earshot.observation.ObservationSink`
-- any engine reaching for a recorder-specific method (``record_stage``, ``stt``,
``turn_id``) fails with ``AttributeError`` rather than passing quietly -- and then
prove the seam is behaviour-preserving: replaying the captured calls onto a real
``TurnRecorder`` yields a byte-identical incident to authoring on it directly.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import earshot
from earshot.clock import ManualClock
from earshot.codec import encode_incident_json
from earshot.contract import ClockDomain
from earshot.engines import BrowserClockDomain
from earshot.engines.device import apply_audio_graph
from earshot.engines.webrtc import apply_webrtc_stats
from earshot.observation import ObservationSink
from earshot.pipeline import TurnRecorder
from earshot.privacy import CaptureClass
from earshot.validation import validate_incident

pytestmark = pytest.mark.unit

START = 1_800_000_000_000_000_000

SNAPSHOTS = [
    {
        "timestamp_ms": 1000,
        "stats": {
            "IT": {
                "type": "inbound-rtp",
                "kind": "audio",
                "packetsReceived": 1000,
                "packetsLost": 5,
                "jitter": 0.010,
            },
            "T": {"type": "transport", "iceState": "connected"},
        },
    },
    {
        "timestamp_ms": 2000,
        "stats": {
            "IT": {
                "type": "inbound-rtp",
                "kind": "audio",
                "packetsReceived": 1100,
                "packetsLost": 200,
                "jitter": 0.050,
            },
            "T": {"type": "transport", "iceState": "disconnected"},
        },
    },
]

DEVICE_EVENTS = [
    {"type": "permission", "timestamp_ms": 4000, "state": "denied"},
    {"type": "latency", "timestamp_ms": 4200, "baseLatency": 0.01, "outputLatency": 0.08},
    {"type": "underrun", "timestamp_ms": 4400},
]

# A cumulative counter that moved backwards: the interval is dropped, and the only
# fact the engine authors is a coverage note.
COUNTER_RESET_SNAPSHOTS = [
    {"timestamp_ms": 1000, "stats": {"IT": {"type": "inbound-rtp", "kind": "audio"}}},
    {
        "timestamp_ms": 2000,
        "stats": {
            "IT": {
                "type": "inbound-rtp",
                "kind": "audio",
                "packetsReceived": 10,
                "packetsLost": 1,
            }
        },
    },
    {
        "timestamp_ms": 3000,
        "stats": {
            "IT": {"type": "inbound-rtp", "kind": "audio", "packetsReceived": 5, "packetsLost": 0}
        },
    },
]


class CollectorSink:
    """A capture source's own sink: it implements the protocol and nothing else.

    This is the shape a browser/native/backend collector would ship -- it holds no
    turn, no session, no clock, and cannot mint an operation. Every authored call
    is retained verbatim so a test can replay it or compare two runs.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def record_measurement(self, name: str, value: float, **kwargs: Any) -> None:
        self.calls.append(("record_measurement", (name, value), kwargs))

    def record_event(self, name: str, **kwargs: Any) -> None:
        self.calls.append(("record_event", (name,), kwargs))

    def record_coverage(self, signal: str, availability: str, reason: str | None = None) -> None:
        self.calls.append(("record_coverage", (signal, availability, reason), {}))

    def record_omission(
        self,
        field_name: str,
        *,
        capture_class: str | CaptureClass,
        reason: str = "adapter_payload_omitted",
    ) -> None:
        self.calls.append(
            ("record_omission", (field_name,), {"capture_class": capture_class, "reason": reason})
        )

    def register_clock_domain(self, domain: ClockDomain) -> None:
        self.calls.append(("register_clock_domain", (domain,), {}))

    def replay(self, sink: ObservationSink) -> None:
        """Author every captured call onto another sink, in the captured order."""

        for verb, arguments, keywords in self.calls:
            getattr(sink, verb)(*arguments, **keywords)


def _verbs(sink: CollectorSink) -> list[str]:
    return [verb for verb, _, _ in sink.calls]


def _applied(snapshots: list[dict[str, Any]]) -> CollectorSink:
    sink = CollectorSink()
    apply_webrtc_stats(sink, snapshots)
    return sink


# -- the engines author through the protocol, not through a recorder -----------


def test_engines_author_onto_a_sink_that_is_not_a_recorder() -> None:
    sink = CollectorSink()

    webrtc = apply_webrtc_stats(sink, SNAPSHOTS)
    device = apply_audio_graph(sink, DEVICE_EVENTS)

    assert not isinstance(sink, TurnRecorder)
    assert webrtc.measurements and device.events
    # Only governed fact verbs were used; no stage/operation authoring leaked in.
    assert set(_verbs(sink)) <= {
        "record_measurement",
        "record_event",
        "record_coverage",
        "record_omission",
        "register_clock_domain",
    }
    assert "record_measurement" in _verbs(sink)
    assert "record_event" in _verbs(sink)


def test_a_sink_missing_a_protocol_verb_fails_loudly() -> None:
    class SinkWithoutCoverage:
        """Every protocol verb but ``record_coverage``."""

        def record_measurement(self, name: str, value: float, **fact: Any) -> None: ...

        def record_event(self, name: str, **fact: Any) -> None: ...

        def record_omission(self, field_name: str, **fact: Any) -> None: ...

        def register_clock_domain(self, domain: ClockDomain) -> None: ...

    # A counter that moved backwards is dropped and noted as coverage. That note is
    # a real dependency, not decoration: an incomplete sink must break rather than
    # silently swallow the engine's explicit *unknown*.
    assert _verbs(_applied(COUNTER_RESET_SNAPSHOTS)) == ["record_coverage"]
    with pytest.raises(AttributeError):
        apply_webrtc_stats(SinkWithoutCoverage(), COUNTER_RESET_SNAPSHOTS)


def test_a_browser_clock_domain_is_declared_through_the_sink() -> None:
    sink = CollectorSink()
    clock = BrowserClockDomain(clock_domain_id="clk_collector", wall_origin_unix_nano=START)

    apply_webrtc_stats(sink, SNAPSHOTS, clock_domain=clock)

    # The domain is declared first, so no fact ever references an undeclared clock.
    assert _verbs(sink)[0] == "register_clock_domain"
    domain = sink.calls[0][1][0]
    assert isinstance(domain, ClockDomain)
    assert domain.clock_domain_id == "clk_collector"


# -- the seam is behaviour-preserving ------------------------------------------


def _incident_through(applier) -> str:
    # A fixed identity and a manual clock make the encoded incident a byte-exact
    # function of the authored facts alone, except for the session's own
    # per-process clock-domain id, which is normalized away.
    session = earshot.pipeline(
        session_id="sink-parity",
        bundle_id="bundle-sink-parity",
        clock=ManualClock(wall=START, monotonic=0),
    )
    with session.turn("turn-parity") as turn:
        applier(turn)
    domain_id = session.clock_domain_id
    bundle = session.close()
    assert validate_incident(bundle).ok
    return encode_incident_json(bundle).decode("utf-8").replace(domain_id, "server-clock-domain")


def test_authoring_via_a_custom_sink_records_exactly_what_the_recorder_records() -> None:
    collector = CollectorSink()
    apply_webrtc_stats(collector, SNAPSHOTS)
    apply_audio_graph(collector, DEVICE_EVENTS)

    direct = _incident_through(
        lambda turn: (
            apply_webrtc_stats(turn, SNAPSHOTS),
            apply_audio_graph(turn, DEVICE_EVENTS),
        )
    )
    replayed = _incident_through(collector.replay)

    # Same facts, same order, same bytes: the sink is a seam, not a translation.
    assert replayed == direct


def test_repeated_derivations_author_identical_calls() -> None:
    first, second = CollectorSink(), CollectorSink()
    clock = BrowserClockDomain(clock_domain_id="clk_deterministic")
    for sink in (first, second):
        apply_webrtc_stats(sink, SNAPSHOTS, clock_domain=clock)
        apply_audio_graph(sink, DEVICE_EVENTS, clock_domain=clock)

    assert first.calls == second.calls


# -- the pipeline recorder keeps satisfying the protocol -----------------------


def _protocol_members() -> tuple[str, ...]:
    return tuple(
        name
        for name, member in vars(ObservationSink).items()
        if not name.startswith("_") and inspect.isfunction(member)
    )


def test_turn_recorder_satisfies_the_observation_sink_protocol() -> None:
    members = _protocol_members()
    assert set(members) == {
        "record_measurement",
        "record_event",
        "record_coverage",
        "record_omission",
        "register_clock_domain",
    }

    for name in members:
        declared = inspect.signature(getattr(ObservationSink, name))
        implemented = inspect.signature(getattr(TurnRecorder, name))
        # Structural satisfaction is only real if a caller written against the
        # protocol can make every call the recorder accepts, by name and default.
        assert declared.parameters == implemented.parameters, name


def test_the_protocol_excludes_pipeline_turn_bookkeeping() -> None:
    # ``record_stage`` mints an operation id and advances the turn cursor. Leaving
    # it out is what lets a fact-only collector implement the seam at all.
    assert "record_stage" not in _protocol_members()
    assert hasattr(TurnRecorder, "record_stage")
