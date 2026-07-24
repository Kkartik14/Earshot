"""Server-side allowlist enforcement for browser-capture telemetry.

The ``@earshot/browser`` kernel already allowlists what it copies out of
``getStats()`` and the audio graph. The server repeats that decision from scratch
because the client is not a trust boundary: anything can POST to ``/v1/capture``,
and any in-process SDK capture source is handed the same untrusted payload. Every
governed member below is validated against a fixed shape, and anything else -- a
``base64Certificate``, a DTLS ``fingerprint``, an ``usernameFragment``, a
candidate ``address``/``ip``/``url``, a raw device label -- is dropped BEFORE an
engine sees it, so it is never derived from, never recorded, and never stored.
Drops are counted by the callers and surfaced as coverage; they are refusals, not
silent reshaping.

These helpers are pure and framework-free (no FastAPI, no request models) so the
HTTP endpoint and an in-process capture source enforce byte-for-byte the same
allowlist. The endpoint's request-shaped iterators (size limits, Pydantic bodies)
stay in ``earshot.api`` and call into these primitives.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

# Ceilings for governed numeric stat members and audio-graph seconds. A reading
# beyond these is not a measurement we can honour, so the member is dropped and
# the drop is recorded as coverage rather than stored.
_MAX_CAPTURE_STAT_NUMBER = 1e15
_MAX_CAPTURE_SECONDS = 3600.0
_MAX_CAPTURE_HZ = 1e7

# Opaque within-report references and the client's salted device/sink hashes. A
# raw label or device id cannot match these shapes, so it is dropped rather than
# stored.
_CAPTURE_STAT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/=-]{0,127}$")
_CAPTURE_DEVICE_HASH = re.compile(r"^dev_[0-9a-f]{8}$")
_CAPTURE_SINK_HASH = re.compile(r"^sink_[0-9a-f]{8}$")

# Closed vocabularies for the enum-valued members the engines read. These are
# W3C enumerations, so anything outside them is not a governed reading.
_CAPTURE_MEDIA_KINDS = frozenset({"audio", "video"})
_CAPTURE_CONNECTION_STATES = frozenset(
    {
        "new",
        "checking",
        "connecting",
        "connected",
        "completed",
        "disconnected",
        "failed",
        "closed",
        "frozen",
        "waiting",
        "in-progress",
        "inprogress",
        "succeeded",
    }
)
_CAPTURE_NETWORK_TYPES = frozenset(
    {"bluetooth", "cellular", "ethernet", "wifi", "wimax", "vpn", "unknown"}
)
_CAPTURE_PERMISSION_STATES = frozenset({"granted", "denied", "prompt"})
_CAPTURE_CONTEXT_STATES = frozenset({"running", "suspended", "closed", "interrupted"})

# How each governed stat member is validated. A member absent from this table is
# not governed and is dropped; there is no pass-through path.
_CAPTURE_STAT_MEMBER_KINDS: dict[str, str] = {
    # universal identity/keying members
    "type": "type",
    "id": "stat_id",
    "kind": "media_kind",
    "mediaType": "media_kind",
    "timestamp": "number",
    # inbound-rtp: loss, jitter buffer, concealment, decode/processing pipeline
    "packetsReceived": "number",
    "packetsLost": "number",
    "packetsDiscarded": "number",
    "fecPacketsReceived": "number",
    "jitter": "number",
    "jitterBufferDelay": "number",
    "jitterBufferEmittedCount": "number",
    "jitterBufferTargetDelay": "number",
    "jitterBufferMinimumDelay": "number",
    "jitterBufferFlushes": "number",
    "concealedSamples": "number",
    "silentConcealedSamples": "number",
    "concealmentEvents": "number",
    "insertedSamplesForDeceleration": "number",
    "removedSamplesForAcceleration": "number",
    "totalSamplesReceived": "number",
    "totalProcessingDelay": "number",
    # remote-inbound-rtp / candidate-pair
    "roundTripTime": "number",
    "currentRoundTripTime": "number",
    "state": "connection_state",
    "selected": "bool",
    "nominated": "bool",
    "localCandidateId": "stat_id",
    "candidatePairId": "stat_id",
    # transport
    "iceState": "connection_state",
    "dtlsState": "connection_state",
    "connectionState": "connection_state",
    "selectedCandidatePairId": "stat_id",
    # local-candidate
    "networkType": "network_type",
    # media-playout (RTCAudioPlayoutStats): the render/playout half
    "synthesizedSamplesDuration": "number",
    "synthesizedSamplesEvents": "number",
    "totalSamplesDuration": "number",
    "totalPlayoutDelay": "number",
    "totalSamplesCount": "number",
}

_CAPTURE_UNIVERSAL_STAT_MEMBERS = frozenset({"type", "id", "kind", "timestamp"})

# The exact members each governed stat type may carry. A stat whose ``type`` is
# absent here is dropped whole: certificates (``base64Certificate``), codecs,
# candidates (``address``/``ip``/``port``/``url``/``usernameFragment``),
# peer-connection, media-source and outbound-rtp are all unconsumed and can never
# reach storage by omission.
_CAPTURE_STAT_ALLOWLIST: dict[str, frozenset[str]] = {
    "inbound-rtp": frozenset(
        {
            "mediaType",
            "packetsReceived",
            "packetsLost",
            "packetsDiscarded",
            "fecPacketsReceived",
            "jitter",
            "jitterBufferDelay",
            "jitterBufferEmittedCount",
            "jitterBufferTargetDelay",
            "jitterBufferMinimumDelay",
            "jitterBufferFlushes",
            "concealedSamples",
            "silentConcealedSamples",
            "concealmentEvents",
            "insertedSamplesForDeceleration",
            "removedSamplesForAcceleration",
            "totalSamplesReceived",
            "totalProcessingDelay",
        }
    ),
    "remote-inbound-rtp": frozenset({"roundTripTime"}),
    "candidate-pair": frozenset(
        {
            "state",
            "selected",
            "nominated",
            "localCandidateId",
            "candidatePairId",
            "currentRoundTripTime",
            "roundTripTime",
        }
    ),
    "transport": frozenset({"iceState", "dtlsState", "connectionState", "selectedCandidatePairId"}),
    "local-candidate": frozenset({"networkType"}),
    "media-playout": frozenset(
        {
            "synthesizedSamplesDuration",
            "synthesizedSamplesEvents",
            "totalSamplesDuration",
            "totalPlayoutDelay",
            "totalSamplesCount",
        }
    ),
}

# The audio-graph event vocabulary ``analyze_audio_graph`` dispatches on, and the
# exact members each family may carry. Device identity only ever appears as the
# client's opaque salted hash (``dev_…`` / ``sink_…``); a raw label or id fails
# the hash pattern and is dropped.
_CAPTURE_EVENT_ALLOWLIST: dict[str, dict[str, str]] = {
    **{
        event_type: {"state": "permission_state", "deviceHash": "device_hash"}
        for event_type in ("permission", "permission_denied", "getusermedia")
    },
    **{
        event_type: {"state": "context_state"}
        for event_type in (
            "audiocontext_state",
            "statechange",
            "audiocontext",
            "audiocontextstatechange",
        )
    },
    **{
        event_type: {"sinkHash": "sink_hash"}
        for event_type in ("sink_change", "sinkchange", "output_change")
    },
    **{
        event_type: {"deviceHash": "device_hash", "sinkHash": "sink_hash"}
        for event_type in ("device_change", "devicechange")
    },
    **{
        event_type: {"configured_hz": "hz", "actual_hz": "hz"}
        for event_type in ("sample_rate_mismatch", "samplerate_mismatch", "sample_rate")
    },
    **{
        event_type: {}
        for event_type in ("underrun", "glitch", "dropped_frames", "xrun", "buffer_underrun")
    },
    **{
        event_type: {
            "base_latency_s": "seconds",
            "output_latency_s": "seconds",
            "render_queue_s": "seconds",
        }
        for event_type in ("latency", "audiocontext_latency", "audio_latency")
    },
}


def _capture_number(value: Any, maximum: float) -> float | None:
    """A finite, non-negative reading within ``maximum``, else ``None`` (dropped).

    Booleans are not numbers here (the engines make the same distinction), and a
    negative or non-finite reading is not a measurement we can honour: every
    governed member in the capture vocabulary is a cumulative counter, a duration,
    or a ratio.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > maximum:
        return None
    return number


def _capture_enum(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _capture_stat_member(kind: str, value: Any) -> Any | None:
    """Validate one governed stat member by its declared kind, or drop it."""

    if kind == "number":
        return _capture_number(value, _MAX_CAPTURE_STAT_NUMBER)
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "media_kind":
        return _capture_enum(value, _CAPTURE_MEDIA_KINDS)
    if kind == "connection_state":
        return _capture_enum(value, _CAPTURE_CONNECTION_STATES)
    if kind == "network_type":
        return _capture_enum(value, _CAPTURE_NETWORK_TYPES)
    if kind == "stat_id":
        # Opaque within-report references only; they key a lookup and are never
        # persisted, but the shape is still constrained so nothing else can ride in.
        if isinstance(value, str) and _CAPTURE_STAT_ID.fullmatch(value):
            return value
        return None
    return None


def _sanitize_capture_stat(stat: Mapping[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """Allowlist one ``RTCStats``-shaped bag; ``None`` drops the stat whole."""

    stat_type = stat.get("type")
    if not isinstance(stat_type, str):
        return None, 0
    allowed = _CAPTURE_STAT_ALLOWLIST.get(stat_type)
    if allowed is None:
        return None, 0  # a stat type no engine consumes never reaches storage
    members: dict[str, Any] = {"type": stat_type}
    dropped = 0
    for key, value in stat.items():
        if key == "type":
            continue
        if key not in _CAPTURE_UNIVERSAL_STAT_MEMBERS and key not in allowed:
            dropped += 1
            continue
        accepted = _capture_stat_member(_CAPTURE_STAT_MEMBER_KINDS.get(key, ""), value)
        if accepted is None:
            dropped += 1
            continue
        members[key] = accepted
    # A stat that names a media kind we cannot read is not audio evidence: drop it
    # whole rather than let the engine treat an unknown kind as audio.
    for key in ("kind", "mediaType"):
        if key in stat and key not in members:
            return None, dropped
    return members, dropped


def _capture_event_member(kind: str, value: Any) -> Any | None:
    if kind == "seconds":
        return _capture_number(value, _MAX_CAPTURE_SECONDS)
    if kind == "hz":
        return _capture_number(value, _MAX_CAPTURE_HZ)
    if kind == "permission_state":
        return _capture_enum(value, _CAPTURE_PERMISSION_STATES)
    if kind == "context_state":
        return _capture_enum(value, _CAPTURE_CONTEXT_STATES)
    if kind == "device_hash":
        # Only the client's opaque, per-session salted hash shape. A raw label or
        # device id cannot match it, so it is dropped rather than stored.
        if isinstance(value, str) and _CAPTURE_DEVICE_HASH.fullmatch(value):
            return value
        return None
    if kind == "sink_hash":
        if isinstance(value, str) and _CAPTURE_SINK_HASH.fullmatch(value):
            return value
        return None
    return None
