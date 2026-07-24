"""FastAPI application for the local, immutable Earshot incident store."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import zlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .analysis import ANALYZER_VERSION
from .browser_session import BrowserSessionStore
from .checkpoint import AssemblyError, JournalUnreadableError, assemble_incident
from .codec import (
    JSON_MEDIA_TYPE,
    PROTOBUF_MEDIA_TYPE,
    IncidentCodecError,
    IncidentDepthError,
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from .connectors import (
    DeliveryError,
    DeliveryTooLargeError,
    HostedProviderIngestion,
    RawProviderDelivery,
)
from .contract import DerivedAnalysis, IncidentBundle, IncidentBundleJson
from .engines.base import BrowserClockDomain
from .engines.device import apply_audio_graph
from .engines.webrtc import apply_webrtc_stats
from .explanation import IncidentExplanation, explain_incident
from .exporters.registry import export_incident, exporter_names, get_exporter
from .live import (
    END_FINAL_ARTIFACT_STORED,
    END_SEALED,
    EVENT_HEARTBEAT,
    LIVE_LIMITATIONS,
    SOURCE_CHECKPOINT,
    CheckpointFramesInvalidError,
    CheckpointSequenceError,
    LiveCapacityError,
    LiveSessionRegistry,
    SessionNotLiveError,
    SessionNotSealableError,
    TailCapacityError,
    make_event,
    render_sse,
)
from .pipeline import pipeline
from .privacy import ExportPolicyError, assert_export_allowed
from .query import compare_incidents, detect_contradictions
from .storage import (
    DEFAULT_PROJECT_ID,
    TURN_METRIC_LIMITATIONS,
    ArtifactCorruptionError,
    IncidentConflictError,
    IncidentNotFoundError,
    IncidentPurgedError,
    IncidentStore,
    InvalidCursorError,
    StorageError,
    StoredAnalysis,
)
from .validation import (
    IncidentValidationError,
    ValidationIssue,
    validate_derived_analysis,
    validate_incident,
)
from .versions import API_VERSION

Analyzer = Callable[..., DerivedAnalysis]
_VIEWER_SESSION_COOKIE = "earshot_session"
_CSRF_HEADER = "x-earshot-csrf"
_PROJECT_HEADER = "x-earshot-project-id"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CHECKPOINT_MEDIA_TYPE = "application/vnd.earshot.checkpoint+frames"
SSE_MEDIA_TYPE = "text/event-stream"


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiIssue(ApiModel):
    code: str
    path: list[str | int]
    message: str
    severity: str


class ProblemDetail(ApiModel):
    code: str
    message: str
    issues: list[ApiIssue] | None = None


class ProblemResponse(ApiModel):
    error: ProblemDetail


class ConnectorProblemDetail(ProblemDetail):
    retryable: bool


class ConnectorProblemResponse(ApiModel):
    error: ConnectorProblemDetail


class HealthResponse(ApiModel):
    status: str


class BrowserSessionResponse(ApiModel):
    project_id: str
    csrf_token: str
    expires_in_seconds: int


class BrowserSessionStatusResponse(ApiModel):
    authenticated: bool
    authentication_required: bool
    project_id: str
    csrf_token: str | None
    expires_in_seconds: int | None


class ValidateResponse(ApiModel):
    valid: bool
    bundle_id: str
    session_id: str
    canonical_sha256: str
    warnings: list[ApiIssue]


class IncidentRecordResponse(ApiModel):
    project_id: str
    bundle_id: str
    session_id: str
    schema_version: str
    digest: str
    size_bytes: int
    status: str
    finality: str
    completeness: str
    framework: str | None
    created_at_unix_nano: str
    ingested_at_unix_nano: str


class IngestResponse(IncidentRecordResponse):
    created: bool
    warnings: list[ApiIssue]


class IncidentPageResponse(ApiModel):
    items: list[IncidentRecordResponse]
    next_cursor: str | None


class LiveSessionResponse(ApiModel):
    """One conversation still being written. Deliberately not an incident.

    Every field here is an observation about the *journal*, never a verdict about
    the session. ``close_observed`` is the only thing that can say the producer
    finished, and until it is true nothing downstream may treat this session as
    complete.
    """

    session_id: str
    bundle_id: str
    journal_id: str
    source: Literal["journal", "checkpoint"]
    state: Literal["live", "stale", "finalized", "abandoned"]
    last_sequence: int
    available_from_sequence: int
    last_append_unix_nano: str
    close_observed: bool
    journal_complete: bool
    sealable: bool


class LiveSessionPageResponse(ApiModel):
    items: list[LiveSessionResponse]
    # Stated, never omitted: a reader has to be told which questions this
    # collection structurally cannot answer.
    limitations: list[str]
    following_journal_directory: bool


class CheckpointAcceptedResponse(ApiModel):
    journal_id: str
    accepted_through: int
    accepted_records: int
    state: Literal["live", "stale", "finalized", "abandoned"]
    sealable: bool


class LiveSealResponse(ApiModel):
    """The artifact an operator explicitly materialized from a live buffer."""

    bundle_id: str
    session_id: str
    created: bool
    finality: str
    completeness: str
    close_observed: bool
    last_sequence: int
    torn_tail_bytes: int
    journal_complete: bool
    unfinished_operations: int


class StoredAnalysisResponse(ApiModel):
    bundle_id: str
    analyzer_version: str
    input_digest: str
    generated_at_unix_nano: str
    analysis: DerivedAnalysis


class TurnMetricGroupResponse(ApiModel):
    group: str
    availability: str
    basis: str
    confidence: str
    limitation: str | None
    turn_count: int
    available_count: int
    average_ms: float | None
    minimum_ms: float | None
    maximum_ms: float | None
    p50_ms: float | None
    p95_ms: float | None


class TurnMetricSummaryResponse(ApiModel):
    """Fleet percentiles for one metric, bounded by the population they come from.

    Only ``final`` incidents are aggregated. A provisional artifact -- one
    recovered from a crash, or sealed while its session was still open -- covers
    an unknown fraction of its conversation, so pooling its turns would move
    these values without anything on them saying why.

    That exclusion is declared rather than performed quietly: ``incident_count``
    is what the groups cover and ``withheld_incident_count`` is what they refuse,
    so an empty ``groups`` beside a non-zero ``withheld_incident_count`` reads as
    "not aggregated", never as "measured zero".
    """

    metric: str
    group_by: str
    groups: list[TurnMetricGroupResponse]
    incident_count: int
    withheld_incident_count: int
    withheld_turn_count: int
    # Stated, never omitted: a reader has to be told which questions these
    # numbers structurally cannot answer.
    limitations: list[str]


class ConnectorDeliveryResponse(ApiModel):
    receipt_id: str
    disposition: Literal["applied", "replayed", "ignored"]
    bundle_id: str | None
    canonical_sha256: str | None


class ContradictionResponse(ApiModel):
    """One evidence-linked contradiction, exactly as ``query.detect_contradictions``
    reports it. Every field names real evidence; no source payload is surfaced."""

    kind: str
    summary: str
    evidence_ids: list[str]
    boundary: str | None = None
    turn_id: str | None = None
    subject: str | None = None


class IncidentContradictionsResponse(ApiModel):
    """Contradictions found in one incident, bound to the analysis that found them.

    An empty ``contradictions`` list means detection ran against ``input_digest``
    and found none. It never stands in for "analysis unavailable": that case is a
    ``404 EARSHOT_ANALYSIS_NOT_AVAILABLE`` instead of an empty answer.
    """

    bundle_id: str
    analyzer_version: str
    input_digest: str
    contradictions: list[ContradictionResponse]


class ComparedDiagnosisResponse(ApiModel):
    code: str
    boundary: str
    turn_ids: list[str]
    diagnosis_id: str
    evidence_ids: list[str]


class TurnMetricDeltaResponse(ApiModel):
    turn_id: str
    metric: str
    unit: str
    known_good_value: int | float
    incident_value: int | float
    delta: int | float


class TurnMetricAvailabilityChangeResponse(ApiModel):
    """A metric whose comparability changed, reported instead of a fabricated delta."""

    turn_id: str
    metric: str
    known_good_availability: str
    incident_availability: str
    comparable: bool


class CoverageGapResponse(ApiModel):
    signal: str
    availability: str
    reason: str | None = None


class UnmatchedTurnsResponse(ApiModel):
    only_in_incident: list[str]
    only_in_known_good: list[str]


class IncidentComparisonResponse(ApiModel):
    """A structured diff of one incident against a known-good incident.

    Both sides are named by bundle id and pinned by the digest their analysis was
    derived from, so the reader can tell exactly what was compared. A latency delta
    appears only where both sides are available in the same unit; every other case
    is an availability change, never an invented number.
    """

    bundle_id: str
    known_good_bundle_id: str
    analyzer_version: str
    input_digest: str
    known_good_input_digest: str
    diagnoses_added: list[ComparedDiagnosisResponse]
    diagnoses_removed: list[ComparedDiagnosisResponse]
    turn_metric_deltas: list[TurnMetricDeltaResponse]
    turn_metric_availability_changes: list[TurnMetricAvailabilityChangeResponse]
    unmatched_turns: UnmatchedTurnsResponse
    coverage_gaps_new: list[CoverageGapResponse]
    coverage_gaps_removed: list[CoverageGapResponse]
    contradictions_new: list[ContradictionResponse]


class IncidentExportResponse(ApiModel):
    """One incident projected through a named exporter in the exporter registry.

    ``format`` is the registered exporter name and ``destination`` is the export
    destination a capture policy must permit for that projection to run, so the
    document is always accompanied by the governance decision that released it.
    """

    bundle_id: str
    digest: str
    format: str
    destination: str
    document: dict[str, Any]


# ---------------------------------------------------------------------------
# Browser capture transport (POST /v1/capture)
# ---------------------------------------------------------------------------
#
# The request body is the ``CapturePayload`` the @earshot/browser capture kernel
# drains (see ``packages/browser/src/types.ts``), so its envelope keys are the
# browser's camelCase names while the response stays in this API's snake_case.
#
# The wire format carries its OWN version (``captureVersion``) independently of
# the ``/v1`` path so the client and server can evolve the payload without a new
# route; an unsupported version is a specific, clean client error.

CAPTURE_PROTOCOL_VERSION = 1

# The browser clock the payload's raw ``timestamp_ms`` readings belong to. Only a
# monotonic browser clock is accepted: those readings are recorded in their own
# ClockDomain, never rebased onto the server clock.
_CAPTURE_CLOCK_KIND = "browser_monotonic"

# Bounds on the individual time values, chosen so every derived nanosecond value
# (monotonic reading, and wall origin + reading) stays inside the contract's
# uint64 ``DecimalNano`` domain instead of overflowing during recording.
_MAX_CAPTURE_TIMESTAMP_MS = 1e10  # ~115 days of monotonic browser uptime
_MAX_CAPTURE_WALL_ORIGIN_MS = 1e13  # ~year 2286 in Unix milliseconds
_MAX_CAPTURE_UNCERTAINTY_MS = 1e6

# Ceilings for governed numeric stat members and audio-graph seconds. A reading
# beyond these is not a measurement we can honour, so the member is dropped and
# the drop is recorded as coverage rather than stored.
_MAX_CAPTURE_STAT_NUMBER = 1e15
_MAX_CAPTURE_SECONDS = 3600.0
_MAX_CAPTURE_HZ = 1e7

# Identifiers and labels the payload may carry. Everything the payload can place
# in the stored incident is constrained to one of these shapes.
_CAPTURE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
_CAPTURE_LABEL_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
_CAPTURE_TRACEPARENT_PATTERN = r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
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


class CaptureTraceContextRequest(ApiModel):
    """The session's W3C trace-context: random correlation handles only."""

    traceparent: str = Field(pattern=_CAPTURE_TRACEPARENT_PATTERN)
    traceId: str = Field(pattern=r"^[0-9a-f]{32}$")
    spanId: str = Field(pattern=r"^[0-9a-f]{16}$")


class CaptureClockDomainRequest(ApiModel):
    """The browser clock every ``timestamp_ms`` in this payload was read from.

    Its identity is what keeps browser readings out of the server clock domain:
    the facts derived here are recorded against ``id`` at their raw readings, and
    no calibration to the server clock is invented.
    """

    id: str = Field(pattern=_CAPTURE_ID_PATTERN)
    kind: Literal["browser_monotonic"]
    unit: Literal["ms"]
    uncertaintyMs: float = Field(ge=0.0, le=_MAX_CAPTURE_UNCERTAINTY_MS)
    wallOriginMs: float | None = Field(default=None, ge=0.0, le=_MAX_CAPTURE_WALL_ORIGIN_MS)


class CaptureSnapshotRequest(ApiModel):
    """One ``RTCPeerConnection.getStats()`` snapshot: stat id -> member bag.

    Members are NOT trusted as sent: the server independently allowlists them
    (see ``_sanitize_capture_stats``) before anything reaches an engine.
    """

    timestamp_ms: float = Field(ge=0.0, le=_MAX_CAPTURE_TIMESTAMP_MS)
    stats: dict[str, dict[str, Any]]


class CaptureDeviceEventRequest(BaseModel):
    """One audio-graph/device lifecycle event: ``{type, timestamp_ms, ...members}``.

    Extra members are accepted by the parser and then dropped by the server
    allowlist, so an unknown member is refused rather than stored.
    """

    model_config = ConfigDict(extra="allow")

    type: str = Field(pattern=_CAPTURE_LABEL_PATTERN)
    timestamp_ms: float = Field(ge=0.0, le=_MAX_CAPTURE_TIMESTAMP_MS)


class CaptureCoverageRequest(ApiModel):
    """One explicit gap the client recorded rather than dropping it silently."""

    signal: str = Field(pattern=_CAPTURE_LABEL_PATTERN)
    availability: Literal["available", "partial", "not_observed"]
    reason: str = Field(pattern=_CAPTURE_LABEL_PATTERN)
    droppedCount: int | None = Field(default=None, ge=0, le=2**31 - 1)


class CaptureRequest(ApiModel):
    """The versioned browser capture payload, exactly as ``drain()`` emits it."""

    captureVersion: int = Field(ge=1, le=2**31 - 1)
    sessionId: str = Field(pattern=_CAPTURE_ID_PATTERN)
    clockDomain: CaptureClockDomainRequest
    traceContext: CaptureTraceContextRequest | None = None
    snapshots: list[CaptureSnapshotRequest] = Field(default_factory=list)
    deviceEvents: list[CaptureDeviceEventRequest] = Field(default_factory=list)
    coverage: list[CaptureCoverageRequest] = Field(default_factory=list)


class CaptureAcceptedResponse(IncidentRecordResponse):
    """The incident one capture batch became, plus what the server refused.

    The ``rejected_*`` counters are the server-side allowlist's own report: they
    say how much of the payload was dropped before it could be stored, so a
    client can see that its batch was trimmed instead of silently reshaped.
    """

    created: bool
    capture_version: int
    trace_id: str | None
    accepted_snapshots: int
    accepted_device_events: int
    accepted_coverage: int
    rejected_stats: int
    rejected_stat_members: int
    rejected_device_events: int
    rejected_device_members: int


@dataclass(frozen=True, slots=True)
class _CaptureRejections:
    """What the server-side allowlist refused to accept from one payload."""

    stats: int = 0
    stat_members: int = 0
    device_events: int = 0
    device_members: int = 0


_ERROR_RESPONSES = {
    status: {"model": ProblemResponse}
    for status in (400, 401, 403, 404, 409, 410, 413, 415, 422, 429, 500, 503)
}

_CONNECTOR_ERROR_RESPONSES = {
    status: {
        "model": ProblemResponse | ConnectorProblemResponse,
        **(
            {
                "headers": {
                    "Retry-After": {
                        "description": "Whole seconds before the delivery should be retried.",
                        "schema": {"type": "integer", "minimum": 1},
                    }
                }
            }
            if status in {429, 503}
            else {}
        ),
    }
    for status in (400, 401, 404, 409, 413, 415, 429, 500, 503)
}

_INCIDENT_REQUEST_BODY = {
    "parameters": [
        {
            "name": "Content-Encoding",
            "in": "header",
            "required": False,
            "schema": {
                "type": "string",
                "enum": ["identity", "gzip"],
                "default": "identity",
            },
        },
        {
            "name": "X-Earshot-Project-Id",
            "in": "header",
            "required": False,
            "description": (
                "SDK assertion checked against the project selected by the credential."
            ),
            "schema": {"type": "string", "minLength": 1, "maxLength": 64},
        },
    ],
    "requestBody": {
        "required": True,
        "content": {
            JSON_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/IncidentBundleJson"}},
            "application/json": {"schema": {"$ref": "#/components/schemas/IncidentBundleJson"}},
            PROTOBUF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
            "application/x-protobuf": {"schema": {"type": "string", "format": "binary"}},
        },
    },
}


_CAPTURE_REQUEST_BODY = {
    "parameters": [
        {
            "name": "X-Earshot-Project-Id",
            "in": "header",
            "required": False,
            "description": (
                "SDK assertion checked against the project selected by the credential."
            ),
            "schema": {"type": "string", "minLength": 1, "maxLength": 64},
        },
    ],
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/CaptureRequest"}},
        },
    },
}


_CHECKPOINT_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "description": (
            "A contiguous run of plaintext checkpoint frames from one journal, "
            "starting at the header frame or at the sequence the server last "
            "accepted. Encrypted journals cannot be uploaded: the server holds no key."
        ),
        "content": {CHECKPOINT_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    },
}


_TAIL_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "A server-sent event stream of admitted journal facts in journal order. "
            "Event names are open, record, operation_open, limit, exhausted, finalize, "
            "replay_truncated, reset, overflow, heartbeat and end. Every record-bearing event "
            "carries id: <journal_id>:<sequence>, so a dropped connection resumes with "
            "Last-Event-ID. No analysis, diagnosis, or turn metric appears on this "
            "stream: derived analysis binds to the digest of a finished artifact and "
            "this session has none."
        ),
        "content": {SSE_MEDIA_TYPE: {"schema": {"type": "string"}}},
    },
    **_ERROR_RESPONSES,
}


@dataclass(frozen=True, slots=True)
class ApiConfig:
    host: str = "127.0.0.1"
    token: str | None = None
    max_body_bytes: int = 16 * 1024 * 1024
    max_connector_body_bytes: int = 2 * 1024 * 1024
    max_connector_deliveries_per_minute: int = 120
    # Browser capture batches are metadata-only and drained periodically, so they
    # are bounded far below an incident bundle. Every bound is explicit and
    # enforced before any client value reaches an engine.
    max_capture_body_bytes: int = 1024 * 1024
    max_capture_snapshots: int = 512
    max_capture_device_events: int = 512
    max_capture_coverage: int = 64
    max_capture_stats_per_snapshot: int = 128
    # Checkpoint uploads are small, frequent batches from a live producer, so
    # they are bounded far below an incident bundle and far below a capture batch.
    max_checkpoint_body_bytes: int = 1024 * 1024
    max_json_depth: int = 64
    default_page_size: int = 50
    analyzer_version: str = ANALYZER_VERSION
    behind_tls_proxy: bool = False
    # Opt-in for a single-machine container: permits an unauthenticated
    # non-loopback bind on the promise that the listener is confined to a
    # trusted boundary (e.g. `docker run -p 127.0.0.1:PORT`). Off by default.
    trust_local_network: bool = False
    viewer_session_capacity: int = 256
    viewer_session_ttl_seconds: int = 8 * 60 * 60

    def __post_init__(self) -> None:
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if self.max_connector_body_bytes < 1:
            raise ValueError("max_connector_body_bytes must be positive")
        if self.max_connector_deliveries_per_minute < 1:
            raise ValueError("max_connector_deliveries_per_minute must be positive")
        if self.max_capture_body_bytes < 1:
            raise ValueError("max_capture_body_bytes must be positive")
        if self.max_capture_snapshots < 1:
            raise ValueError("max_capture_snapshots must be positive")
        if self.max_capture_device_events < 1:
            raise ValueError("max_capture_device_events must be positive")
        if self.max_capture_coverage < 1:
            raise ValueError("max_capture_coverage must be positive")
        if self.max_capture_stats_per_snapshot < 1:
            raise ValueError("max_capture_stats_per_snapshot must be positive")
        if self.max_checkpoint_body_bytes < 1:
            raise ValueError("max_checkpoint_body_bytes must be positive")
        if self.max_json_depth < 1:
            raise ValueError("max_json_depth must be positive")
        if self.viewer_session_capacity < 1:
            raise ValueError("viewer_session_capacity must be positive")
        if self.viewer_session_ttl_seconds < 1:
            raise ValueError("viewer_session_ttl_seconds must be positive")
        if (
            not _is_loopback(self.host)
            and not self.behind_tls_proxy
            and not self.trust_local_network
        ):
            raise ValueError("a non-loopback listener requires an explicitly trusted TLS proxy")


def _remote_access(settings: ApiConfig) -> bool:
    """Whether the listener is reachable beyond the loopback interface."""
    return not _is_loopback(settings.host) or settings.behind_tls_proxy


def _authentication_required(settings: ApiConfig) -> bool:
    """Single source of truth for whether ``/v1`` demands a credential.

    Loopback — or an explicitly trusted local network (a loopback-mapped
    container) — with no configured token serves anonymously; every other
    binding requires a bearer key or a viewer session. Middleware enforcement,
    the ``/v1/auth/session`` gate, and the generated OpenAPI security all read
    from this one predicate so they cannot drift.
    """
    return (
        _remote_access(settings) and not settings.trust_local_network
    ) or settings.token is not None


class ApiProblem(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        issues: list[dict[str, object]] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.issues = issues
        super().__init__(message)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_header_is_loopback(host_header: str) -> bool:
    candidate = host_header.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        hostname = candidate[1:closing] if closing > 0 else ""
    else:
        hostname = candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate
    hostname = hostname.rstrip(".").lower()
    return hostname == "testserver" or _is_loopback(hostname)


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _response_media_type(request: Request) -> str:
    accept = request.headers.get("accept", JSON_MEDIA_TYPE).lower()
    if PROTOBUF_MEDIA_TYPE in accept or "application/x-protobuf" in accept:
        return PROTOBUF_MEDIA_TYPE
    if JSON_MEDIA_TYPE not in accept and "application/json" in accept:
        return "application/json"
    return JSON_MEDIA_TYPE


def _issue_dict(issue: ValidationIssue) -> dict[str, object]:
    return {
        "code": issue.code,
        "path": list(issue.path),
        # The internal message may mention source identifiers. API errors remain
        # useful through stable code/path without reflecting incident values.
        "message": "incident violates an Earshot contract invariant",
        "severity": issue.severity,
    }


async def _read_body(request: Request, maximum: int, *, subject: str = "incident") -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise ApiProblem(413, "EARSHOT_BODY_TOO_LARGE", f"{subject} body exceeds limit")
        except ValueError as error:
            raise ApiProblem(
                400,
                "EARSHOT_INVALID_CONTENT_LENGTH",
                "invalid Content-Length header",
            ) from error

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise ApiProblem(413, "EARSHOT_BODY_TOO_LARGE", f"{subject} body exceeds limit")
    if not body:
        raise ApiProblem(400, "EARSHOT_EMPTY_BODY", f"{subject} body is empty")
    return bytes(body)


def _decompress_gzip(payload: bytes, maximum: int) -> bytes:
    """Decode one strict gzip member without allocating beyond the body limit."""

    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = bytearray(decompressor.decompress(payload, maximum + 1))
        if len(decoded) > maximum or decompressor.unconsumed_tail:
            raise ApiProblem(
                413,
                "EARSHOT_BODY_TOO_LARGE",
                "incident body exceeds limit after decompression",
            )
        decoded.extend(decompressor.flush(maximum + 1 - len(decoded)))
    except zlib.error as error:
        raise ApiProblem(
            400,
            "EARSHOT_MALFORMED_GZIP",
            "incident gzip body is malformed",
        ) from error
    if len(decoded) > maximum:
        raise ApiProblem(
            413,
            "EARSHOT_BODY_TOO_LARGE",
            "incident body exceeds limit after decompression",
        )
    if not decompressor.eof or decompressor.unused_data or not decoded:
        raise ApiProblem(
            400,
            "EARSHOT_MALFORMED_GZIP",
            "incident gzip body is malformed",
        )
    return bytes(decoded)


async def _read_connector_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise DeliveryTooLargeError
        except ValueError as error:
            raise ApiProblem(
                400,
                "EARSHOT_INVALID_CONTENT_LENGTH",
                "invalid Content-Length header",
            ) from error

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise DeliveryTooLargeError
    if not body:
        raise ApiProblem(400, "EARSHOT_EMPTY_BODY", "connector body is empty")
    return bytes(body)


def _strict_json_preflight(payload: bytes, maximum_depth: int, *, subject: str = "incident") -> Any:
    """Parse JSON under the strict rules the store depends on; return the value.

    Duplicate object keys, non-finite constants, and over-deep nesting are all
    refused here with stable codes so no decoder downstream has to be defensive
    about them. The parsed value is returned so a caller that needs the raw
    document (rather than a typed decode) does not parse it twice.
    """

    class DuplicateKey(ValueError):
        pass

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in values:
            if key in output:
                raise DuplicateKey
            output[key] = value
        return output

    def reject_constant(_: str) -> None:
        raise ValueError

    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except DuplicateKey as error:
        raise ApiProblem(
            400,
            "EARSHOT_DUPLICATE_JSON_KEY",
            f"{subject} JSON contains a duplicate object key",
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ApiProblem(400, "EARSHOT_MALFORMED_JSON", f"{subject} JSON is malformed") from error

    stack: list[tuple[object, int]] = [(parsed, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > maximum_depth:
            raise ApiProblem(
                400,
                "EARSHOT_JSON_TOO_DEEP",
                f"{subject} JSON nesting exceeds limit",
            )
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return parsed


def _decode_request(payload: bytes, content_type: str, config: ApiConfig) -> IncidentBundle:
    try:
        if content_type in {JSON_MEDIA_TYPE, "application/json"}:
            _strict_json_preflight(payload, config.max_json_depth)
            return decode_incident_json(
                payload,
                max_profile_depth=config.max_json_depth,
            )
        if content_type in {PROTOBUF_MEDIA_TYPE, "application/x-protobuf"}:
            return decode_incident_protobuf(
                payload,
                max_profile_depth=config.max_json_depth,
            )
    except IncidentDepthError as error:
        raise ApiProblem(
            400,
            "EARSHOT_JSON_TOO_DEEP",
            "incident JSON nesting exceeds limit",
        ) from error
    except IncidentCodecError as error:
        if isinstance(error.__cause__, IncidentValidationError):
            raise ApiProblem(
                422,
                "EARSHOT_INVALID_INCIDENT",
                "incident does not satisfy the Earshot contract",
                issues=[_issue_dict(issue) for issue in error.__cause__.report.errors],
            ) from error
        # Codec errors can contain provider-originated identifiers. Keep the API
        # response stable and non-reflective instead of echoing exception text.
        raise ApiProblem(
            422,
            "EARSHOT_INVALID_INCIDENT",
            "incident does not satisfy the Earshot contract",
        ) from error
    raise ApiProblem(415, "EARSHOT_UNSUPPORTED_MEDIA_TYPE", "unsupported incident media type")


# -- browser capture: independent server-side enforcement ----------------------
#
# The @earshot/browser kernel already allowlists what it copies out of
# ``getStats()``. The server repeats that decision from scratch because the
# client is not a trust boundary: anything can POST here. Every member below is
# validated against a governed shape, and anything else -- a `base64Certificate`,
# a DTLS `fingerprint`, an `usernameFragment`, a candidate `address`/`ip`/`url`,
# a device label -- is dropped BEFORE an engine sees it, so it is never derived
# from, never recorded, and never stored. Drops are counted and surfaced as
# coverage; they are refusals, not silent reshaping.


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


def _sanitize_capture_snapshots(
    snapshots: list[CaptureSnapshotRequest],
    *,
    max_stats: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return engine-ready snapshots plus (dropped stats, dropped members)."""

    cleaned: list[dict[str, Any]] = []
    dropped_stats = 0
    dropped_members = 0
    for snapshot in snapshots:
        if len(snapshot.stats) > max_stats:
            raise ApiProblem(
                413,
                "EARSHOT_CAPTURE_TOO_LARGE",
                "capture snapshot exceeds the stat count limit",
            )
        stats: dict[str, dict[str, Any]] = {}
        for stat_id, stat in snapshot.stats.items():
            if not _CAPTURE_STAT_ID.fullmatch(stat_id) or not isinstance(stat, dict):
                dropped_stats += 1
                continue
            members, dropped = _sanitize_capture_stat(stat)
            dropped_members += dropped
            if members is None:
                dropped_stats += 1
                continue
            stats[stat_id] = members
        cleaned.append({"timestamp_ms": snapshot.timestamp_ms, "stats": stats})
    return cleaned, dropped_stats, dropped_members


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


def _sanitize_capture_events(
    events: list[CaptureDeviceEventRequest],
) -> tuple[list[dict[str, Any]], int, int]:
    """Return engine-ready device events plus (dropped events, dropped members)."""

    cleaned: list[dict[str, Any]] = []
    dropped_events = 0
    dropped_members = 0
    for event in events:
        event_type = event.type.lower()
        allowed = _CAPTURE_EVENT_ALLOWLIST.get(event_type)
        if allowed is None:
            dropped_events += 1  # an event type no engine dispatches on
            continue
        payload: dict[str, Any] = {
            "type": event_type,
            "timestamp_ms": event.timestamp_ms,
        }
        for key, value in (event.model_extra or {}).items():
            kind = allowed.get(key)
            accepted = None if kind is None else _capture_event_member(kind, value)
            if accepted is None:
                dropped_members += 1
                continue
            payload[key] = accepted
        cleaned.append(payload)
    return cleaned, dropped_events, dropped_members


def _capture_coverage_signal(signal: str) -> str:
    """Namespace a client-declared signal so it can never mask a server one.

    The browser's coverage is the browser's claim about what IT could observe.
    Recording it under its own prefix keeps it real coverage while stopping a
    payload from overwriting a note an engine derived server-side.
    """

    return signal if signal.startswith("browser.") else f"browser.{signal}"


def _capture_bundle_id(project_id: str, fingerprint: Mapping[str, Any]) -> str:
    """A bundle id derived from the sanitized batch, so a retry is not a duplicate.

    The transport may re-send a batch it never learned the fate of. Deriving the
    identity from the batch's own content means the second delivery resolves to
    the incident the first one created instead of a second copy of the evidence.
    """

    digest = hashlib.sha256(
        json.dumps(
            {"project_id": project_id, **fingerprint},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"capture-{digest[:32]}"


def _capture_issue(item: Mapping[str, Any]) -> dict[str, object]:
    # Stable code and path only. The path is the caller's own field location and
    # is truncated; no payload value is reflected back.
    return {
        "code": "EARSHOT_INVALID_CAPTURE_FIELD",
        "path": [str(part)[:64] for part in item.get("loc", ())],
        "message": "capture field is invalid",
        "severity": "error",
    }


def _decode_capture_request(parsed: Any, config: ApiConfig) -> CaptureRequest:
    """Decode one capture payload: version first, then bounds, then shape.

    The version gate runs before anything else so a client on a newer (or older)
    wire format gets that specific answer instead of a pile of field errors about
    a schema it was never targeting.
    """

    if not isinstance(parsed, dict):
        raise ApiProblem(
            400,
            "EARSHOT_MALFORMED_CAPTURE",
            "capture payload must be a JSON object",
        )
    version = parsed.get("captureVersion")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ApiProblem(
            400,
            "EARSHOT_CAPTURE_VERSION_REQUIRED",
            "capture payload must declare an integer captureVersion",
        )
    if version != CAPTURE_PROTOCOL_VERSION:
        raise ApiProblem(
            400,
            "EARSHOT_UNSUPPORTED_CAPTURE_VERSION",
            (
                "capture protocol version is not supported; this server accepts "
                f"version {CAPTURE_PROTOCOL_VERSION}"
            ),
        )
    for field, limit in (
        ("snapshots", config.max_capture_snapshots),
        ("deviceEvents", config.max_capture_device_events),
        ("coverage", config.max_capture_coverage),
    ):
        value = parsed.get(field)
        if isinstance(value, list) and len(value) > limit:
            raise ApiProblem(
                413,
                "EARSHOT_CAPTURE_TOO_LARGE",
                f"capture payload exceeds the {field} count limit",
            )
    try:
        return CaptureRequest.model_validate(parsed)
    except ValidationError as error:
        raise ApiProblem(
            422,
            "EARSHOT_INVALID_CAPTURE",
            "capture payload does not satisfy the capture contract",
            issues=[_capture_issue(item) for item in error.errors()[:20]],
        ) from error


def _build_capture_incident(
    capture: CaptureRequest,
    snapshots: list[dict[str, Any]],
    events: list[dict[str, Any]],
    rejections: _CaptureRejections,
    *,
    bundle_id: str,
) -> IncidentBundle:
    """Turn one sanitized capture batch into a governed incident.

    The browser clock is declared as its own domain and every derived fact is
    placed in it at its RAW browser reading -- never rebased onto the server
    clock -- so the analyzer keeps refusing cross-clock latency until a real
    ``ClockRelation`` is supplied.

    Two honest limitations of this projection, stated rather than papered over:
    the client's ``droppedCount`` has no field on the v1alpha1 ``Coverage``
    record (the gap is recorded, the count is returned in the response only), and
    the incident inherits the pipeline's ``client.render not_observed`` note
    because a capture batch yields render-path *quality* signals, not per-turn
    render boundaries.
    """

    clock = capture.clockDomain
    domain = BrowserClockDomain(
        clock_domain_id=clock.id,
        kind=_CAPTURE_CLOCK_KIND,
        observer="browser",
        uncertainty_nano=int(clock.uncertaintyMs * 1_000_000),
        wall_origin_unix_nano=(
            None if clock.wallOriginMs is None else int(clock.wallOriginMs * 1_000_000)
        ),
    )
    session = pipeline(
        session_id=capture.sessionId,
        bundle_id=bundle_id,
        framework="browser_capture",
        producer_name="earshot.capture_api",
    )
    with session.turn("browser-capture") as turn:
        for note in capture.coverage:
            turn.record_coverage(
                _capture_coverage_signal(note.signal),
                note.availability,
                note.reason,
            )
        for signal, reason, count in (
            ("capture.stats", "non_governed_stat_dropped", rejections.stats),
            ("capture.stat_members", "non_governed_member_dropped", rejections.stat_members),
            ("capture.device_events", "non_governed_event_dropped", rejections.device_events),
            (
                "capture.device_event_members",
                "non_governed_member_dropped",
                rejections.device_members,
            ),
        ):
            if count > 0:
                turn.record_coverage(signal, "partial", reason)
        apply_webrtc_stats(turn, snapshots, clock_domain=domain)
        apply_audio_graph(turn, events, clock_domain=domain)
    return session.close()


_ProjectionT = TypeVar("_ProjectionT")


def _derived_projection(compute: Callable[[], _ProjectionT]) -> _ProjectionT:
    """Run a ``query`` projection, turning an unbound analysis into a clean refusal.

    The query surface refuses to answer about one incident from an analysis derived
    from different evidence (``DerivedAnalysis.input_sha256`` mismatch) and raises.
    That is a state conflict between the stored artifact and the stored analysis,
    not a server fault, so it surfaces as ``409`` rather than an unhandled ``500``.
    """

    try:
        return compute()
    except ValueError as error:
        raise ApiProblem(
            409,
            "EARSHOT_ANALYSIS_BINDING_MISMATCH",
            "stored analysis is not derived from this incident's evidence",
        ) from error


def _analysis_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True, warnings=False)
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value


_SPA_RESERVED_PREFIXES = (
    "v1/",
    "hooks/",
    "healthz",
    "readyz",
    "openapi.json",
    "docs",
    "redoc",
)


def _resolve_web_dir(web_dir: str | Path | None) -> Path | None:
    """Locate a built single-page viewer to serve alongside the API, if any.

    Precedence: explicit argument, ``EARSHOT_WEB_DIR``, then a ``web`` directory
    packaged next to this module. Returns ``None`` when no ``index.html`` exists,
    so the API runs headless without a bundled UI.
    """
    candidate: str | Path | None = web_dir or os.environ.get("EARSHOT_WEB_DIR")
    if candidate is None:
        packaged = Path(__file__).resolve().parent / "web"
        candidate = packaged if packaged.is_dir() else None
    if candidate is None:
        return None
    root = Path(candidate).expanduser().resolve()
    return root if (root / "index.html").is_file() else None


def _mount_spa(app: FastAPI, web_root: Path) -> None:
    """Serve the viewer: hashed assets are cached by the static handler, client
    routes fall back to ``index.html``, and API prefixes are never shadowed."""
    assets = web_root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    def index() -> FileResponse:
        return FileResponse(
            web_root / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa(spa_path: str) -> Response:
        if spa_path.startswith(_SPA_RESERVED_PREFIXES):
            return JSONResponse(
                {"error": {"code": "EARSHOT_NOT_FOUND", "message": "not found"}},
                status_code=404,
            )
        if spa_path:
            candidate = (web_root / spa_path).resolve()
            within = candidate == web_root or web_root in candidate.parents
            if within and candidate.is_file():
                return FileResponse(candidate)
        return index()


def create_app(
    *,
    store: IncidentStore | None = None,
    data_dir: str | Path = ".earshot",
    analyzer: Analyzer | None = None,
    config: ApiConfig | None = None,
    connector_ingestion: HostedProviderIngestion | None = None,
    web_dir: str | Path | None = None,
    live_registry: LiveSessionRegistry | None = None,
) -> FastAPI:
    settings = config or ApiConfig()
    repository = store or IncidentStore(data_dir)
    remote_access = _remote_access(settings)
    if (
        remote_access
        and not settings.trust_local_network
        and not settings.token
        and not repository.has_active_api_keys()
    ):
        raise ValueError("remote access requires a bearer token or an active project API key")
    # A registry always exists so remote checkpoint ingestion works out of the
    # box and the live routes are always describable in the contract. Following a
    # local checkpoint directory is separate, and stays an explicit opt-in
    # because reading one is a decision about where session evidence lives.
    live = live_registry or LiveSessionRegistry()
    live.start()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Stop following journals on shutdown, and tell subscribers why."""

        live.start()
        try:
            yield
        finally:
            live.close()

    app = FastAPI(
        title="Earshot local ingest",
        version=API_VERSION,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.store = repository
    app.state.config = settings
    app.state.analyzer = analyzer
    app.state.connector_ingestion = connector_ingestion or HostedProviderIngestion(
        repository,
        max_body_bytes=settings.max_connector_body_bytes,
        max_json_depth=settings.max_json_depth,
        max_deliveries_per_minute=settings.max_connector_deliveries_per_minute,
    )
    app.state.browser_sessions = BrowserSessionStore(
        capacity=settings.viewer_session_capacity,
        ttl_seconds=settings.viewer_session_ttl_seconds,
    )
    app.state.live = live

    def openapi_schema() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
            description=(
                "Local immutable ingest, retrieval, purge, and digest-bound analysis "
                "for Earshot v1alpha1 incidents."
            ),
        )
        incident_schema = IncidentBundleJson.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        definitions = incident_schema.pop("$defs", {})
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(definitions)
        components["IncidentBundleJson"] = incident_schema
        # The capture body is read and validated by hand (streamed byte bound
        # first), so its schema is published here rather than inferred from a
        # route signature.
        capture_schema = CaptureRequest.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components.update(capture_schema.pop("$defs", {}))
        components["CaptureRequest"] = capture_schema
        schema["components"].setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
        }
        schema["components"]["securitySchemes"]["BrowserSession"] = {
            "type": "apiKey",
            "in": "cookie",
            "name": _VIEWER_SESSION_COOKIE,
        }
        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith("/v1/"):
                continue
            for method, operation in path_item.items():
                if isinstance(operation, dict) and "responses" in operation:
                    # Loopback deployments may omit auth; remote deployments
                    # accept bearer clients or the same-origin viewer session.
                    authenticated = [{"BearerAuth": []}, {"BrowserSession": []}]
                    auth_needed = _authentication_required(settings)
                    if path == "/v1/auth/session" and method == "post":
                        operation["security"] = [{"BearerAuth": []}]
                    elif path == "/v1/auth/session" and method == "get":
                        operation["security"] = (
                            [{"BrowserSession": []}]
                            if auth_needed
                            else [{"BrowserSession": []}, {}]
                        )
                    elif path == "/v1/auth/logout":
                        operation["security"] = [{"BrowserSession": []}]
                    else:
                        operation["security"] = (
                            authenticated if auth_needed else [*authenticated, {}]
                        )
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_schema  # type: ignore[method-assign]

    @app.exception_handler(ApiProblem)
    async def handle_api_problem(_: Request, error: ApiProblem) -> JSONResponse:
        error_body: dict[str, object] = {"code": error.code, "message": error.message}
        value: dict[str, object] = {"error": error_body}
        if error.issues is not None:
            error_body["issues"] = error.issues
        return JSONResponse(value, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, error: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "code": "EARSHOT_INVALID_REQUEST_FIELD",
                "path": [str(part) for part in item.get("loc", ())],
                "message": "request field is invalid",
                "severity": "error",
            }
            for item in error.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_INVALID_REQUEST",
                    "message": "request parameters are invalid",
                    "issues": issues,
                }
            },
            status_code=422,
        )

    @app.exception_handler(IncidentNotFoundError)
    async def handle_not_found(_: Request, __: IncidentNotFoundError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "EARSHOT_INCIDENT_NOT_FOUND", "message": "incident not found"}},
            status_code=404,
        )

    @app.exception_handler(IncidentPurgedError)
    async def handle_purged(_: Request, __: IncidentPurgedError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "EARSHOT_INCIDENT_PURGED", "message": "incident was purged"}},
            status_code=410,
        )

    @app.exception_handler(IncidentConflictError)
    async def handle_conflict(_: Request, __: IncidentConflictError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_INCIDENT_CONFLICT",
                    "message": "bundle identifier already exists with different content",
                }
            },
            status_code=409,
        )

    @app.exception_handler(ArtifactCorruptionError)
    async def handle_corruption(_: Request, __: ArtifactCorruptionError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_ARTIFACT_CORRUPT",
                    "message": "stored incident failed an integrity check",
                }
            },
            status_code=500,
        )

    @app.exception_handler(StorageError)
    async def handle_storage(_: Request, __: StorageError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_STORAGE_UNAVAILABLE",
                    "message": "incident storage is temporarily unavailable",
                }
            },
            status_code=503,
        )

    @app.exception_handler(DeliveryError)
    async def handle_delivery_error(_: Request, error: DeliveryError) -> JSONResponse:
        headers = (
            {"Retry-After": str(error.retry_after_seconds)}
            if error.retry_after_seconds is not None
            else None
        )
        return JSONResponse(
            {
                "error": {
                    "code": error.code,
                    "message": error.public_message,
                    "retryable": error.retryable,
                }
            },
            status_code=error.http_status,
            headers=headers,
        )

    @app.exception_handler(ExportPolicyError)
    async def handle_export_policy(_: Request, __: ExportPolicyError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_EXPORT_DENIED",
                    "message": "incident policy denies this export",
                }
            },
            status_code=403,
        )

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def handle_storage_system_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "EARSHOT_STORAGE_UNAVAILABLE",
                    "message": "incident storage is temporarily unavailable",
                }
            },
            status_code=503,
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Callable[..., Any]) -> Response:
        runtime_server = request.scope.get("server")
        runtime_host = str(runtime_server[0]) if runtime_server else ""
        test_transport = runtime_host == "testserver"
        unsafe_runtime_binding = (
            runtime_host and not test_transport and not _is_loopback(runtime_host)
        )
        transport_protected_path = request.url.path.startswith(("/v1/", "/hooks/v1/"))
        if (
            unsafe_runtime_binding
            and transport_protected_path
            and not settings.behind_tls_proxy
            and not settings.trust_local_network
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "EARSHOT_REMOTE_BINDING_UNSAFE",
                        "message": (
                            "runtime listener is non-loopback; the backend permits only "
                            "a loopback listener unless a remote TLS proxy is trusted"
                        ),
                    }
                },
                status_code=503,
            )
        local_only_host_boundary = settings.trust_local_network or (
            not settings.token and _is_loopback(settings.host)
        )
        if (
            local_only_host_boundary
            and transport_protected_path
            and not _host_header_is_loopback(request.headers.get("host", ""))
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "EARSHOT_UNTRUSTED_HOST",
                        "message": "loopback API requires a loopback Host header",
                    }
                },
                status_code=400,
            )
        if request.url.path.startswith("/v1/"):
            authorization = request.headers.get("authorization", "")
            scheme, separator, credential = authorization.partition(" ")
            supplied = credential if separator and scheme.lower() == "bearer" else ""
            principal = None
            browser_session = None
            browser_token = ""
            if supplied:
                principal = await run_in_threadpool(
                    repository.authenticate_api_key,
                    supplied,
                )
            elif not authorization:
                browser_token = request.cookies.get(_VIEWER_SESSION_COOKIE, "")
                if browser_token:
                    browser_session = app.state.browser_sessions.authenticate(browser_token)
                    if browser_session is not None and browser_session.key_id is not None:
                        issuer_active = await run_in_threadpool(
                            repository.api_key_is_active,
                            browser_session.project_id,
                            browser_session.key_id,
                        )
                        if not issuer_active:
                            app.state.browser_sessions.revoke(browser_token)
                            browser_session = None
            legacy_valid = bool(
                supplied and settings.token and hmac.compare_digest(supplied, settings.token)
            )
            auth_required = _authentication_required(settings)
            bearer_valid = principal is not None or legacy_valid
            session_valid = browser_session is not None
            invalid_supplied = bool(authorization or browser_token) and not (
                bearer_valid or session_valid
            )
            if (auth_required and not bearer_valid and not session_valid) or invalid_supplied:
                return JSONResponse(
                    {
                        "error": {
                            "code": "EARSHOT_UNAUTHORIZED",
                            "message": "valid bearer token required",
                        }
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if browser_session is not None and request.method.upper() in _UNSAFE_METHODS:
                csrf = request.headers.get(_CSRF_HEADER, "")
                if not app.state.browser_sessions.csrf_matches(browser_session, csrf):
                    return JSONResponse(
                        {
                            "error": {
                                "code": "EARSHOT_CSRF_REQUIRED",
                                "message": "valid CSRF token required",
                            }
                        },
                        status_code=403,
                    )
            request.state.project_id = (
                principal.project_id
                if principal is not None
                else (
                    browser_session.project_id
                    if browser_session is not None
                    else DEFAULT_PROJECT_ID
                )
            )
            request.state.auth_method = (
                "bearer" if bearer_valid else ("session" if session_valid else "anonymous")
            )
            request.state.auth_key_id = principal.key_id if principal is not None else None
            request.state.browser_session = browser_session
            request.state.browser_session_token = browser_token
            asserted_project_id = request.headers.get(_PROJECT_HEADER)
            if asserted_project_id is not None and asserted_project_id != request.state.project_id:
                return JSONResponse(
                    {
                        "error": {
                            "code": "EARSHOT_PROJECT_MISMATCH",
                            "message": (
                                "asserted SDK project does not match the authenticated project"
                            ),
                        }
                    },
                    status_code=403,
                )
        return await call_next(request)

    @app.post(
        "/v1/auth/session",
        response_model=BrowserSessionResponse,
        status_code=201,
        responses=_ERROR_RESPONSES,
    )
    def create_browser_session(request: Request) -> JSONResponse:
        if request.state.auth_method != "bearer":
            raise ApiProblem(401, "EARSHOT_UNAUTHORIZED", "valid bearer token required")
        previous = request.cookies.get(_VIEWER_SESSION_COOKIE, "")
        if previous:
            app.state.browser_sessions.revoke(previous)
        issued = app.state.browser_sessions.issue(
            project_id=request.state.project_id,
            key_id=request.state.auth_key_id,
        )
        response = JSONResponse(
            {
                "project_id": issued.session.project_id,
                "csrf_token": issued.session.csrf_token,
                "expires_in_seconds": settings.viewer_session_ttl_seconds,
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            _VIEWER_SESSION_COOKIE,
            issued.token,
            max_age=settings.viewer_session_ttl_seconds,
            path="/",
            secure=settings.behind_tls_proxy,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get(
        "/v1/auth/session",
        response_model=BrowserSessionStatusResponse,
        responses=_ERROR_RESPONSES,
    )
    def get_browser_session(request: Request) -> JSONResponse:
        session = request.state.browser_session
        if request.state.auth_method != "session" or session is None:
            # A trusted local network (e.g. a loopback-mapped container) needs no
            # viewer login, so the SPA can load without a project key it does not
            # yet have. Same predicate as the middleware and OpenAPI security.
            if _authentication_required(settings):
                raise ApiProblem(401, "EARSHOT_UNAUTHORIZED", "viewer session required")
            return JSONResponse(
                {
                    "authenticated": False,
                    "authentication_required": False,
                    "project_id": DEFAULT_PROJECT_ID,
                    "csrf_token": None,
                    "expires_in_seconds": None,
                },
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "authenticated": True,
                "authentication_required": True,
                "project_id": session.project_id,
                "csrf_token": session.csrf_token,
                "expires_in_seconds": max(
                    0,
                    int(session.expires_at - time.monotonic()),
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/v1/auth/logout",
        status_code=204,
        responses=_ERROR_RESPONSES,
    )
    def logout_browser_session(request: Request) -> Response:
        token = request.state.browser_session_token
        if request.state.auth_method != "session" or not token:
            raise ApiProblem(401, "EARSHOT_UNAUTHORIZED", "viewer session required")
        app.state.browser_sessions.revoke(token)
        response = Response(status_code=204)
        response.delete_cookie(
            _VIEWER_SESSION_COOKIE,
            path="/",
            secure=settings.behind_tls_proxy,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/healthz", response_model=HealthResponse)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    def ready() -> JSONResponse:
        is_ready = repository.ready()
        return JSONResponse(
            {"status": "ready" if is_ready else "unavailable"},
            status_code=200 if is_ready else 503,
        )

    @app.post(
        "/hooks/v1/connectors/{endpoint_id}",
        response_model=ConnectorDeliveryResponse,
        responses=_CONNECTOR_ERROR_RESPONSES,
    )
    async def connector_delivery_endpoint(endpoint_id: str, request: Request) -> JSONResponse:
        if _content_type(request) != "application/json":
            raise ApiProblem(
                415,
                "EARSHOT_UNSUPPORTED_MEDIA_TYPE",
                "connector delivery requires application/json",
            )
        payload = await _read_connector_body(request, settings.max_connector_body_bytes)
        raw = RawProviderDelivery(
            endpoint_id=endpoint_id,
            headers=tuple(request.headers.raw),
            body=payload,
        )
        outcome = await run_in_threadpool(app.state.connector_ingestion.receive, raw)
        return JSONResponse(
            {
                "receipt_id": outcome.receipt_id,
                "disposition": outcome.disposition,
                "bundle_id": outcome.bundle_id,
                "canonical_sha256": outcome.canonical_sha256,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/metrics/turns",
        response_model=TurnMetricSummaryResponse,
        responses=_ERROR_RESPONSES,
    )
    def turn_metrics_endpoint(
        request: Request,
        metric: Literal[
            "stt_finalization_ms",
            "eou_ms",
            "first_token_ms",
            "generated_response_ms",
            "sent_response_ms",
            "received_response_ms",
            "render_start_response_ms",
            "response_ms",
            "turn_duration_ms",
        ] = "response_ms",
        group_by: Literal["framework", "provider", "model", "language", "status"] = "framework",
    ) -> JSONResponse:
        """Aggregate one turn metric across this project's final incidents.

        The withheld counts and the limitations travel with the numbers so that
        a caller reading a percentile also reads the population it came from.
        """

        fleet = repository.summarize_turn_metric_fleet(
            metric,
            project_id=request.state.project_id,
            group_by=group_by,
        )
        return JSONResponse(
            {
                "metric": metric,
                "group_by": group_by,
                "groups": [
                    {
                        "group": group.group,
                        "availability": group.availability,
                        "basis": group.basis,
                        "confidence": group.confidence,
                        "limitation": group.limitation,
                        "turn_count": group.turn_count,
                        "available_count": group.available_count,
                        "average_ms": group.average_ms,
                        "minimum_ms": group.minimum_ms,
                        "maximum_ms": group.maximum_ms,
                        "p50_ms": group.p50_ms,
                        "p95_ms": group.p95_ms,
                    }
                    for group in fleet.groups
                ],
                "incident_count": fleet.incident_count,
                "withheld_incident_count": fleet.withheld_incident_count,
                "withheld_turn_count": fleet.withheld_turn_count,
                "limitations": list(TURN_METRIC_LIMITATIONS),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/v1/capture",
        response_model=CaptureAcceptedResponse,
        status_code=201,
        responses={200: {"model": CaptureAcceptedResponse}, **_ERROR_RESPONSES},
        openapi_extra=_CAPTURE_REQUEST_BODY,
    )
    async def capture_endpoint(request: Request) -> JSONResponse:
        """Accept one browser capture batch and store it as a governed incident.

        Authentication, project scoping and CSRF are the same ``/v1`` rules every
        other endpoint uses: the middleware has already resolved a bearer project
        key or a viewer session, rejected a mismatched ``X-Earshot-Project-Id``,
        and -- because this is an unsafe method -- required a CSRF token from a
        cookie-authenticated caller.

        Everything after that is fail-closed: the body is bounded while it is
        still being streamed, the wire version is checked before the schema, the
        collection sizes are checked before the payload is materialised, and
        every stat/event member is re-derived from the server's own allowlist so
        no client value reaches an engine -- or storage -- unless this server
        governs it.

        Delivery is idempotent by batch content, so a transport retry after an
        unknown outcome resolves to the incident the first delivery created
        (``201`` when this call created it, ``200`` when it already existed).
        """

        if _content_type(request) != "application/json":
            raise ApiProblem(
                415,
                "EARSHOT_UNSUPPORTED_MEDIA_TYPE",
                "browser capture requires application/json",
            )
        body = await _read_body(request, settings.max_capture_body_bytes, subject="capture")
        parsed = _strict_json_preflight(body, settings.max_json_depth, subject="capture")
        capture = _decode_capture_request(parsed, settings)
        project_id = request.state.project_id

        def accept() -> tuple[dict[str, object], bool, _CaptureRejections, int, int]:
            snapshots, dropped_stats, dropped_members = _sanitize_capture_snapshots(
                capture.snapshots,
                max_stats=settings.max_capture_stats_per_snapshot,
            )
            events, dropped_events, dropped_event_members = _sanitize_capture_events(
                capture.deviceEvents
            )
            rejections = _CaptureRejections(
                stats=dropped_stats,
                stat_members=dropped_members,
                device_events=dropped_events,
                device_members=dropped_event_members,
            )
            bundle_id = _capture_bundle_id(
                project_id,
                {
                    "capture_version": capture.captureVersion,
                    "session_id": capture.sessionId,
                    "clock_domain": capture.clockDomain.model_dump(),
                    "snapshots": snapshots,
                    "device_events": events,
                    "coverage": [note.model_dump() for note in capture.coverage],
                },
            )
            try:
                # A re-delivered batch resolves to the incident it already became
                # rather than a second copy of the same evidence.
                return (
                    repository.get_record(bundle_id, project_id=project_id).as_dict(),
                    False,
                    rejections,
                    len(snapshots),
                    len(events),
                )
            except IncidentNotFoundError:
                pass
            bundle = _build_capture_incident(
                capture,
                snapshots,
                events,
                rejections,
                bundle_id=bundle_id,
            )
            try:
                result = repository.ingest(
                    bundle,
                    encode_incident_protobuf(bundle),
                    project_id=project_id,
                )
            except IncidentConflictError:
                # Two concurrent deliveries of the same batch: the loser sees the
                # id already taken. Only the ingest timestamps differ, so this is
                # the same evidence, not a conflict to report to the client.
                record = repository.get_record(bundle_id, project_id=project_id)
                return (record.as_dict(), False, rejections, len(snapshots), len(events))
            return (
                result.record.as_dict(),
                result.created,
                rejections,
                len(snapshots),
                len(events),
            )

        record, created, rejections, snapshot_count, event_count = await run_in_threadpool(accept)
        value: dict[str, object] = dict(record)
        value["created"] = created
        value["capture_version"] = capture.captureVersion
        value["trace_id"] = None if capture.traceContext is None else capture.traceContext.traceId
        value["accepted_snapshots"] = snapshot_count
        value["accepted_device_events"] = event_count
        value["accepted_coverage"] = len(capture.coverage)
        value["rejected_stats"] = rejections.stats
        value["rejected_stat_members"] = rejections.stat_members
        value["rejected_device_events"] = rejections.device_events
        value["rejected_device_members"] = rejections.device_members
        return JSONResponse(
            value,
            status_code=201 if created else 200,
            headers={
                "Location": f"/v1/incidents/{quote(str(record['bundle_id']), safe='')}",
                "Cache-Control": "no-store",
            },
        )

    async def decode_and_validate(
        request: Request,
    ) -> tuple[IncidentBundle, list[dict[str, object]], bytes]:
        content_encoding = request.headers.get("content-encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity", "gzip"}:
            raise ApiProblem(
                415,
                "EARSHOT_UNSUPPORTED_CONTENT_ENCODING",
                "incident content encoding is not supported",
            )
        payload = await _read_body(request, settings.max_body_bytes)
        if content_encoding == "gzip":
            payload = await run_in_threadpool(
                _decompress_gzip,
                payload,
                settings.max_body_bytes,
            )
        content_type = _content_type(request)

        def decode_validate_and_canonicalize() -> tuple[
            IncidentBundle, list[dict[str, object]], bytes
        ]:
            bundle = _decode_request(payload, content_type, settings)
            report = validate_incident(bundle)
            if not report.ok:
                raise ApiProblem(
                    422,
                    "EARSHOT_INVALID_INCIDENT",
                    "incident does not satisfy the Earshot contract",
                    issues=[_issue_dict(issue) for issue in report.errors],
                )
            canonical = encode_incident_protobuf(bundle)
            return bundle, [_issue_dict(issue) for issue in report.warnings], canonical

        return await run_in_threadpool(decode_validate_and_canonicalize)

    @app.post(
        "/v1/incidents/validate",
        response_model=ValidateResponse,
        responses=_ERROR_RESPONSES,
        openapi_extra=_INCIDENT_REQUEST_BODY,
    )
    async def validate_endpoint(request: Request) -> JSONResponse:
        bundle, warnings, canonical = await decode_and_validate(request)
        return JSONResponse(
            {
                "valid": True,
                "bundle_id": bundle.profile.manifest.bundle_id,
                "session_id": bundle.profile.manifest.session_id,
                "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
                "warnings": warnings,
            }
        )

    @app.post(
        "/v1/incidents",
        response_model=IngestResponse,
        status_code=201,
        responses={200: {"model": IngestResponse}, **_ERROR_RESPONSES},
        openapi_extra=_INCIDENT_REQUEST_BODY,
    )
    async def ingest_endpoint(request: Request) -> JSONResponse:
        bundle, warnings, canonical = await decode_and_validate(request)
        idempotency_key = request.headers.get("idempotency-key")
        if idempotency_key and idempotency_key != bundle.profile.manifest.bundle_id:
            raise ApiProblem(
                400,
                "EARSHOT_IDEMPOTENCY_KEY_MISMATCH",
                "Idempotency-Key must match the incident bundle identifier",
            )
        result = await run_in_threadpool(
            repository.ingest,
            bundle,
            canonical,
            project_id=request.state.project_id,
        )
        # The artifact now exists, so the live buffer for this session is
        # superseded and is dropped rather than lingering as a second, weaker
        # account of the same conversation.
        live.drop_session(
            bundle.profile.manifest.session_id,
            reason=END_FINAL_ARTIFACT_STORED,
            project_id=request.state.project_id,
        )
        value = result.record.as_dict()
        value["created"] = result.created
        value["warnings"] = warnings
        return JSONResponse(
            value,
            status_code=201 if result.created else 200,
            headers={
                "Location": f"/v1/incidents/{quote(result.record.bundle_id, safe='')}",
                "ETag": f'"sha256:{result.record.digest}"',
            },
        )

    def _reject_foreign_origin(request: Request) -> None:
        """Refuse a browser-driven live request whose Origin is not this host.

        The API sets no CORS headers, so a cross-origin ``EventSource`` cannot
        read the stream in the first place. This is the belt to that suspenders:
        a bearer client never sends ``Origin``, so requiring the two to match
        costs nothing and removes the whole class of confused-deputy reads
        against a cookie the browser attaches automatically.
        """

        origin = request.headers.get("origin")
        if not origin or getattr(request.state, "auth_method", None) == "bearer":
            return
        if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            raise ApiProblem(
                403,
                "EARSHOT_ORIGIN_NOT_ALLOWED",
                "live requests must originate from this host",
            )

    @app.get(
        "/v1/live/sessions",
        response_model=LiveSessionPageResponse,
        responses=_ERROR_RESPONSES,
    )
    def live_sessions_endpoint(request: Request) -> JSONResponse:
        """List the conversations currently being written, and nothing more.

        These are not incidents and never appear under ``/v1/incidents``. The
        limitations travel with the collection so that "no analysis here" is read
        as a refusal rather than as an empty result.
        """

        _reject_foreign_origin(request)
        items = live.sessions(project_id=request.state.project_id)
        return JSONResponse(
            {
                "items": [item.as_dict() for item in items],
                "limitations": list(LIVE_LIMITATIONS),
                "following_journal_directory": live.journal_dir is not None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/live/sessions/{session_id}/tail",
        responses=_TAIL_RESPONSES,
    )
    async def live_tail_endpoint(
        session_id: str,
        request: Request,
        from_: str = Query(
            default="start",
            alias="from",
            pattern=r"^(start|live|[0-9]{1,10})$",
            description=(
                "start replays the journal from its first frame, live sends only "
                "what arrives next, and a number resumes at that sequence. "
                "Last-Event-ID overrides all three."
            ),
        ),
    ) -> Response:
        """Stream one journal as server-sent events.

        SSE rather than a WebSocket, deliberately. Every guarantee this backend
        makes — refusing an unsafe runtime binding, the loopback Host check,
        bearer/API-key/browser-session authentication, CSRF, project scoping —
        lives in one ``@app.middleware("http")``, and Starlette does not run HTTP
        middleware for WebSocket scopes. A WebSocket endpoint would have to
        restate all of it, and the first drift would be a vulnerability. As an
        ordinary GET this route inherits the entire stack unchanged, is covered
        by the same-origin policy, and gets ``Last-Event-ID`` resume for free.
        """

        _reject_foreign_origin(request)
        try:
            subscription = live.subscribe(
                session_id,
                project_id=request.state.project_id,
                from_spec=from_,
                last_event_id=request.headers.get("last-event-id"),
            )
        except SessionNotLiveError as error:
            raise ApiProblem(
                404,
                "EARSHOT_SESSION_NOT_LIVE",
                "no live session with this identifier",
            ) from error
        except TailCapacityError as error:
            raise ApiProblem(
                429,
                "EARSHOT_TAIL_CAPACITY",
                "the server is carrying as many live tails as it will",
            ) from error

        wakeup = asyncio.Event()
        subscription.attach(asyncio.get_running_loop(), wakeup)
        heartbeat = live.config.heartbeat_seconds

        async def stream() -> AsyncIterator[str]:
            try:
                while True:
                    # Cleared before draining so an event queued during the drain
                    # still wakes the next wait instead of being slept through.
                    wakeup.clear()
                    for event in subscription.drain():
                        yield render_sse(event)
                    terminal = subscription.terminal()
                    if terminal:
                        for event in terminal:
                            yield render_sse(event)
                        return
                    try:
                        await asyncio.wait_for(wakeup.wait(), timeout=heartbeat)
                    except TimeoutError:
                        # Carries no id, so it never advances the client's
                        # resume cursor, and states the position it is quiet at.
                        yield render_sse(
                            make_event(
                                EVENT_HEARTBEAT,
                                subscription.journal_id,
                                0,
                                {
                                    "as_of_sequence": subscription.last_delivered_sequence,
                                    "close_observed": False,
                                },
                            )
                        )
            finally:
                subscription.close()

        return StreamingResponse(
            stream(),
            media_type=SSE_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-store",
                # Buffering proxies turn an event stream into a long poll; say so
                # to the ones that listen.
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/v1/live/sessions/{session_id}/checkpoints",
        response_model=CheckpointAcceptedResponse,
        status_code=202,
        responses=_ERROR_RESPONSES,
        openapi_extra=_CHECKPOINT_REQUEST_BODY,
    )
    async def live_checkpoints_endpoint(session_id: str, request: Request) -> JSONResponse:
        """Accept a contiguous run of checkpoint frames from a live producer.

        The buffer this feeds is never an incident and never becomes one on its
        own. It expires, it is superseded when the real artifact is ingested, or
        an operator seals it explicitly — because the server cannot tell a
        crashed producer from a slow one.
        """

        _reject_foreign_origin(request)
        if _content_type(request) != CHECKPOINT_MEDIA_TYPE:
            raise ApiProblem(
                415,
                "EARSHOT_UNSUPPORTED_MEDIA_TYPE",
                f"checkpoint batches require {CHECKPOINT_MEDIA_TYPE}",
            )
        payload = await _read_body(
            request,
            settings.max_checkpoint_body_bytes,
            subject="checkpoint",
        )
        try:
            accepted = await run_in_threadpool(
                live.accept_frames,
                session_id,
                payload,
                project_id=request.state.project_id,
            )
        except SessionNotLiveError as error:
            raise ApiProblem(
                404,
                "EARSHOT_SESSION_NOT_LIVE",
                "no live session with this identifier",
            ) from error
        except CheckpointSequenceError as error:
            raise ApiProblem(
                409,
                "EARSHOT_CHECKPOINT_SEQUENCE_GAP",
                "checkpoint batch does not continue the accepted sequence",
                issues=[
                    {
                        "code": "EARSHOT_CHECKPOINT_SEQUENCE_GAP",
                        "path": ["expected_sequence"],
                        "message": str(error.expected_sequence),
                        "severity": "error",
                    }
                ],
            ) from error
        except CheckpointFramesInvalidError as error:
            raise ApiProblem(
                400,
                "EARSHOT_CHECKPOINT_FRAMES_INVALID",
                "checkpoint batch is not an intact run of journal frames",
            ) from error
        except LiveCapacityError as error:
            raise ApiProblem(
                429,
                "EARSHOT_LIVE_CAPACITY",
                "this project is holding as many live sessions as it will",
            ) from error
        return JSONResponse(
            {
                "journal_id": accepted.journal_id,
                "accepted_through": accepted.accepted_through,
                "accepted_records": accepted.accepted_records,
                "state": accepted.state,
                "sealable": accepted.sealable,
            },
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/v1/live/sessions/{session_id}/seal",
        response_model=LiveSealResponse,
        status_code=201,
        responses={200: {"model": LiveSealResponse}, **_ERROR_RESPONSES},
    )
    async def live_seal_endpoint(session_id: str, request: Request) -> JSONResponse:
        """Materialize a live buffer into an artifact, on operator command only.

        Nothing else in this server turns a live session into an incident. A seal
        of a journal that never reached close produces a *provisional* artifact
        under a distinct bundle id, so it can never be confused with, or collide
        with, the final one the producer will still send.
        """

        _reject_foreign_origin(request)
        try:
            kind, source = live.seal_source(session_id, project_id=request.state.project_id)
            summary = live.summary(session_id, project_id=request.state.project_id)
        except SessionNotLiveError as error:
            raise ApiProblem(
                404,
                "EARSHOT_SESSION_NOT_LIVE",
                "no live session with this identifier",
            ) from error
        except SessionNotSealableError as error:
            raise ApiProblem(
                409,
                "EARSHOT_SESSION_NOT_SEALABLE",
                "this live session cannot be materialized into an artifact",
            ) from error

        # A journal that reached close reproduces exactly what the producer will
        # send, so it keeps its bundle id and content-addressed ingest
        # deduplicates it. One that did not is a different artifact and takes a
        # distinct, deterministic id derived from the sequence sealed.
        suffix = None if summary.close_observed else f".s{summary.last_sequence}"

        def materialize() -> tuple[Any, Any]:
            if kind == SOURCE_CHECKPOINT:
                with tempfile.TemporaryDirectory(prefix="earshot-seal-") as directory:
                    path = Path(directory) / "sealed.eck"
                    path.write_bytes(source if isinstance(source, bytes) else b"")
                    path.chmod(0o600)
                    result = assemble_incident(path, bundle_id_suffix=suffix)
            else:
                result = assemble_incident(Path(str(source)), bundle_id_suffix=suffix)
            ingested = repository.ingest(
                result.bundle,
                encode_incident_protobuf(result.bundle),
                project_id=request.state.project_id,
            )
            return result, ingested

        try:
            result, ingested = await run_in_threadpool(materialize)
        except (AssemblyError, JournalUnreadableError) as error:
            raise ApiProblem(
                409,
                "EARSHOT_SESSION_NOT_SEALABLE",
                "this live session cannot be materialized into an artifact",
            ) from error
        except IncidentValidationError as error:
            raise ApiProblem(
                422,
                "EARSHOT_INVALID_INCIDENT",
                "the sealed incident does not satisfy the Earshot contract",
                issues=[_issue_dict(issue) for issue in error.report.errors],
            ) from error

        if summary.close_observed:
            # The producer finished and the artifact exists; the live buffer is
            # now the weaker account of the same conversation.
            live.drop_session(
                session_id,
                reason=END_SEALED,
                project_id=request.state.project_id,
            )
        manifest = result.bundle.profile.manifest
        return JSONResponse(
            {
                "bundle_id": manifest.bundle_id,
                "session_id": manifest.session_id,
                "created": ingested.created,
                "finality": manifest.finality,
                "completeness": manifest.completeness,
                "close_observed": result.report.close_observed,
                "last_sequence": result.report.last_sequence,
                "torn_tail_bytes": result.report.torn_tail_bytes,
                "journal_complete": result.report.journal_complete,
                "unfinished_operations": result.report.unfinished_operations,
            },
            status_code=201 if ingested.created else 200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/incidents",
        response_model=IncidentPageResponse,
        responses=_ERROR_RESPONSES,
    )
    def list_endpoint(
        request: Request,
        session_id: str | None = None,
        limit: int = Query(default=settings.default_page_size, ge=1, le=100),
        cursor: str | None = None,
    ) -> JSONResponse:
        try:
            page = repository.list_incidents(
                project_id=request.state.project_id,
                session_id=session_id,
                limit=limit,
                cursor=cursor,
                destination="local_api",
            )
        except InvalidCursorError as error:
            raise ApiProblem(
                400,
                "EARSHOT_INVALID_CURSOR",
                "invalid incident pagination cursor",
            ) from error
        visible = []
        for item in page.items:
            _, payload = repository.get_artifact(
                item.bundle_id, project_id=request.state.project_id
            )
            try:
                bundle = decode_incident_protobuf(payload)
            except IncidentCodecError as error:
                raise ArtifactCorruptionError("stored incident cannot be decoded") from error
            try:
                assert_export_allowed(bundle, "local_api")
            except ExportPolicyError:
                continue
            visible.append(item.as_dict())
        return JSONResponse(
            {
                "items": visible,
                "next_cursor": page.next_cursor,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/incidents/{bundle_id}",
        responses={
            200: {
                "content": {
                    JSON_MEDIA_TYPE: {
                        "schema": {"$ref": "#/components/schemas/IncidentBundleJson"}
                    },
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/IncidentBundleJson"}
                    },
                    PROTOBUF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
                }
            },
            **_ERROR_RESPONSES,
        },
    )
    def get_endpoint(bundle_id: str, request: Request) -> Response:
        record, payload = repository.get_artifact(bundle_id, project_id=request.state.project_id)
        try:
            bundle = decode_incident_protobuf(payload)
        except IncidentCodecError as error:
            raise ArtifactCorruptionError("stored incident cannot be decoded") from error
        assert_export_allowed(bundle, "local_api")
        common_headers = {
            "X-Earshot-Digest": record.digest,
            "Vary": "Accept",
            "Cache-Control": "no-store",
        }
        response_media_type = _response_media_type(request)
        if response_media_type == PROTOBUF_MEDIA_TYPE:
            headers = dict(common_headers)
            headers["ETag"] = f'"sha256:{hashlib.sha256(payload).hexdigest()}"'
            return Response(payload, media_type=PROTOBUF_MEDIA_TYPE, headers=headers)
        rendered = encode_incident_json(bundle, indent=2)
        headers = dict(common_headers)
        headers["ETag"] = f'"sha256:{hashlib.sha256(rendered).hexdigest()}"'
        return Response(rendered, media_type=response_media_type, headers=headers)

    def resolve_analysis(
        bundle_id: str, *, project_id: str
    ) -> tuple[IncidentBundle, StoredAnalysis, DerivedAnalysis]:
        record, payload = repository.get_artifact(bundle_id, project_id=project_id)
        try:
            bundle = decode_incident_protobuf(payload)
        except IncidentCodecError as error:
            raise ArtifactCorruptionError("stored incident cannot be decoded") from error
        assert_export_allowed(bundle, "local_api")
        stored = repository.get_analysis(
            bundle_id,
            settings.analyzer_version,
            project_id=project_id,
        )
        if stored is not None:
            try:
                bound_analysis = DerivedAnalysis.model_validate(stored.value)
            except ValidationError as error:
                raise ArtifactCorruptionError("stored analysis cannot be decoded") from error
            return bundle, stored, bound_analysis
        if analyzer is None:
            raise ApiProblem(
                404,
                "EARSHOT_ANALYSIS_NOT_AVAILABLE",
                "analysis is not available for this incident",
            )

        generated_at = time.time_ns()
        value = analyzer(
            bundle,
            input_sha256=record.digest,
            generated_at_unix_nano=generated_at,
        )
        try:
            bound_analysis = DerivedAnalysis.model_validate(_analysis_value(value))
        except ValidationError as error:
            raise ApiProblem(
                500,
                "EARSHOT_ANALYZER_CONTRACT",
                "analyzer returned an invalid DerivedAnalysis value",
            ) from error
        if (
            bound_analysis.input_sha256 != record.digest
            or bound_analysis.analyzer_version != settings.analyzer_version
            or int(bound_analysis.generated_at_unix_nano) != generated_at
        ):
            raise ApiProblem(
                500,
                "EARSHOT_ANALYZER_BINDING_MISMATCH",
                "analyzer output does not match its requested input/version/time binding",
            )
        analysis_report = validate_derived_analysis(bundle, bound_analysis)
        if not analysis_report.ok:
            raise ApiProblem(
                500,
                "EARSHOT_ANALYZER_CONTRACT",
                "analyzer output is inconsistent with the source evidence graph",
                issues=[_issue_dict(issue) for issue in analysis_report.errors],
            )
        stored = repository.put_analysis(
            bundle_id,
            settings.analyzer_version,
            bound_analysis,
            project_id=project_id,
        )
        return bundle, stored, bound_analysis

    @app.get(
        "/v1/incidents/{bundle_id}/analysis",
        response_model=StoredAnalysisResponse,
        responses=_ERROR_RESPONSES,
    )
    def analysis_endpoint(bundle_id: str, request: Request) -> JSONResponse:
        _, stored, _ = resolve_analysis(
            bundle_id,
            project_id=request.state.project_id,
        )
        return JSONResponse(
            stored.as_dict(),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/incidents/{bundle_id}/explanation",
        response_model=IncidentExplanation,
        responses=_ERROR_RESPONSES,
    )
    def explanation_endpoint(bundle_id: str, request: Request) -> JSONResponse:
        bundle, _, analysis = resolve_analysis(
            bundle_id,
            project_id=request.state.project_id,
        )
        explanation = explain_incident(bundle, analysis)
        return JSONResponse(
            explanation.model_dump(mode="json", exclude_none=True),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/incidents/{bundle_id}/contradictions",
        response_model=IncidentContradictionsResponse,
        responses=_ERROR_RESPONSES,
    )
    def contradictions_endpoint(bundle_id: str, request: Request) -> JSONResponse:
        bundle, stored, analysis = resolve_analysis(
            bundle_id,
            project_id=request.state.project_id,
        )
        contradictions = _derived_projection(lambda: detect_contradictions(bundle, analysis))
        return JSONResponse(
            {
                "bundle_id": stored.bundle_id,
                "analyzer_version": stored.analyzer_version,
                "input_digest": stored.input_digest,
                "contradictions": [item.as_dict() for item in contradictions],
            },
            headers={"Cache-Control": "no-store"},
        )

    def resolve_known_good_analysis(
        bundle_id: str, *, project_id: str
    ) -> tuple[IncidentBundle, StoredAnalysis, DerivedAnalysis]:
        """Resolve the comparison baseline, naming *which* side is unavailable.

        A comparison names two incidents, so the generic incident errors would leave
        the caller unable to tell which one is missing, purged, or unanalysed. The
        baseline keeps its own stable codes; the same non-reflective messages apply.
        """

        try:
            return resolve_analysis(bundle_id, project_id=project_id)
        except IncidentNotFoundError as error:
            raise ApiProblem(
                404,
                "EARSHOT_KNOWN_GOOD_NOT_FOUND",
                "known-good incident not found",
            ) from error
        except IncidentPurgedError as error:
            raise ApiProblem(
                410,
                "EARSHOT_KNOWN_GOOD_PURGED",
                "known-good incident was purged",
            ) from error
        except ApiProblem as error:
            if error.code != "EARSHOT_ANALYSIS_NOT_AVAILABLE":
                raise
            raise ApiProblem(
                404,
                "EARSHOT_KNOWN_GOOD_ANALYSIS_NOT_AVAILABLE",
                "analysis is not available for the known-good incident",
            ) from error

    @app.get(
        "/v1/incidents/{bundle_id}/comparison",
        response_model=IncidentComparisonResponse,
        responses=_ERROR_RESPONSES,
    )
    def comparison_endpoint(
        bundle_id: str,
        request: Request,
        known_good_bundle_id: str = Query(min_length=1),
    ) -> JSONResponse:
        project_id = request.state.project_id
        bundle, stored, analysis = resolve_analysis(bundle_id, project_id=project_id)
        known_good, known_good_stored, known_good_analysis = resolve_known_good_analysis(
            known_good_bundle_id,
            project_id=project_id,
        )
        comparison = _derived_projection(
            lambda: compare_incidents(
                bundle,
                known_good,
                incident_analysis=analysis,
                known_good_analysis=known_good_analysis,
            )
        )
        return JSONResponse(
            {
                "bundle_id": stored.bundle_id,
                "known_good_bundle_id": known_good_stored.bundle_id,
                "analyzer_version": stored.analyzer_version,
                "input_digest": stored.input_digest,
                "known_good_input_digest": known_good_stored.input_digest,
                **comparison.as_dict(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/incidents/{bundle_id}/export",
        response_model=IncidentExportResponse,
        responses=_ERROR_RESPONSES,
    )
    def export_endpoint(
        bundle_id: str,
        request: Request,
        format: str = Query(
            default="otlp",
            description=(
                "Registered exporter name. The enumerated choices are the exporter "
                "registry's names when this document was generated; a process that "
                "registers its own exporter can select it here by name."
            ),
            json_schema_extra={"enum": list(exporter_names())},
        ),
    ) -> JSONResponse:
        record, payload = repository.get_artifact(bundle_id, project_id=request.state.project_id)
        try:
            bundle = decode_incident_protobuf(payload)
        except IncidentCodecError as error:
            raise ArtifactCorruptionError("stored incident cannot be decoded") from error
        # Two gates, both fail closed: reading the incident out through this API,
        # then the exporter's own declared destination. The projection runs through
        # the registry rather than an exporter function so that second gate cannot
        # be bypassed by adding a route.
        assert_export_allowed(bundle, "local_api")
        try:
            registration = get_exporter(format)
        except ValueError as error:
            raise ApiProblem(
                400,
                "EARSHOT_UNKNOWN_EXPORT_FORMAT",
                "requested export format is not a registered exporter",
            ) from error
        document = export_incident(bundle, format=registration.name)
        return JSONResponse(
            {
                "bundle_id": record.bundle_id,
                "digest": record.digest,
                "format": registration.name,
                "destination": registration.destination,
                "document": document,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.delete(
        "/v1/incidents/{bundle_id}",
        status_code=204,
        responses=_ERROR_RESPONSES,
    )
    def delete_endpoint(bundle_id: str, request: Request) -> Response:
        repository.purge(bundle_id, project_id=request.state.project_id)
        return Response(status_code=204)

    # The SPA catch-all is registered last so every API route takes precedence.
    web_root = _resolve_web_dir(web_dir)
    if web_root is not None:
        _mount_spa(app, web_root)

    return app
