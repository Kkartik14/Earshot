from __future__ import annotations

import re
import tomllib
from pathlib import Path

import earshot
from earshot.analysis import ANALYZER_NAME, ANALYZER_VERSION
from earshot.api import create_app
from earshot.connectors.elevenlabs import ADAPTER_VERSION as ELEVENLABS_VERSION
from earshot.connectors.retell import ADAPTER_VERSION as RETELL_VERSION
from earshot.connectors.ringg import ADAPTER_VERSION as RINGG_VERSION
from earshot.connectors.vapi import ADAPTER_VERSION as VAPI_VERSION
from earshot.contract import SCHEMA_VERSION, SEMANTIC_PROFILE_VERSION
from earshot.pipeline import PIPELINE_ADAPTER_VERSION
from earshot.storage import TURN_FACT_PROJECTION_VERSION
from earshot.versions import API_VERSION, PACKAGE_VERSION


def test_unreleased_public_layers_are_centrally_versioned_and_pre_v1(tmp_path) -> None:
    pyproject = tomllib.loads((Path(__file__).resolve().parents[3] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == PACKAGE_VERSION
    versions = {
        SCHEMA_VERSION,
        SEMANTIC_PROFILE_VERSION,
        ANALYZER_VERSION,
        PIPELINE_ADAPTER_VERSION,
        TURN_FACT_PROJECTION_VERSION,
        ELEVENLABS_VERSION,
        VAPI_VERSION,
        RETELL_VERSION,
        RINGG_VERSION,
        API_VERSION,
    }
    assert all(item.startswith("0.") for item in versions)
    assert create_app(data_dir=tmp_path).version == API_VERSION


def test_pipeline_evidence_semantics_have_a_new_adapter_version() -> None:
    assert PIPELINE_ADAPTER_VERSION == "0.3.0"


def test_analysis_truth_changes_have_a_new_cache_identity() -> None:
    assert ANALYZER_VERSION == "0.6.0"


def test_published_docs_name_the_analyzer_identity_the_code_actually_ships() -> None:
    """The analyzer identity is a storage cache key, not a decorative version.

    A doc naming an older one tells a reader their cached projection is current
    when the code would recompute it. Research notes and private planning docs
    are excluded on purpose: they are dated records of what was true then.
    """

    root = Path(__file__).resolve().parents[3] / "docs"
    published = [
        path
        for path in root.rglob("*.md")
        if not {"private", "research"} & set(path.relative_to(root).parts)
    ]
    assert published
    identity = re.compile(rf"{re.escape(ANALYZER_NAME)}@([0-9]+\.[0-9]+\.[0-9]+)")
    named = {version for path in published for version in identity.findall(path.read_text())}

    assert named == {ANALYZER_VERSION}


def test_the_capture_surface_evolution_is_versioned() -> None:
    # 0.10.0 adds continuous capture: ``POST /v1/capture`` accepts a
    # ``captureVersion: 2`` drain and accumulates a whole browser call into one
    # journal-backed live session instead of a per-drain incident. A v2 drain
    # answers with a checkpoint-shaped acknowledgement (call_id, journal_id,
    # accepted_through, sealable) and 202/200 rather than an IncidentRecordResponse
    # and 201/200, and it can be refused with two new codes,
    # EARSHOT_CAPTURE_SEQUENCE_GAP and EARSHOT_CAPTURE_SEQUENCE_CONFLICT, that the
    # v1 single-slice path never produced -- a client-visible change to what the
    # endpoint accepts and returns, so it takes a version of its own.
    # 0.9.0 makes ``POST /v1/capture`` refuse two payloads it used to accept and
    # then guess at, because neither can be turned into evidence honestly:
    # EARSHOT_INCOHERENT_TRACE_CONTEXT for a ``traceparent`` that disagrees with
    # the trace/span ids sent beside it, and EARSHOT_CAPTURE_NON_MONOTONIC for a
    # batch whose ``timestamp_ms`` readings move backwards. A request that used
    # to yield 201 now yields 422, which is a client-visible change to what the
    # endpoint accepts, so it takes a version of its own.
    # 0.8.0 gives a live session a per-project identity: it is named by
    # (project, session id) rather than by session id alone, so two projects can
    # use the same session id and neither can squat the other's. Checkpoint
    # ingestion gained the two refusals that keep an uploaded journal honest —
    # EARSHOT_CHECKPOINT_JOURNAL_FINALIZED for a frame after finalize and
    # EARSHOT_CHECKPOINT_DIVERGED for a retry that rewrites an accepted
    # sequence — and GET /v1/live/sessions declares the frame size remote
    # checkpoint upload covers.
    # 0.7.0 makes the live tail enforce its own export destination,
    # ``live_tail``: the ``open`` event declares that name and the capture
    # classes forbidden to it, and a record the policy will not let leave the
    # process arrives as a new ``withheld`` event at its own sequence instead of
    # as its content or as a silent gap. 0.6.0 makes ``GET /v1/metrics/turns``
    # state the population behind its numbers: only final incidents are
    # aggregated, and the provisional ones the aggregate refused are counted on
    # the response (``incident_count``, ``withheld_incident_count``,
    # ``withheld_turn_count``, ``limitations``) rather than dropped silently.
    # 0.5.0 added the ``/v1/live`` namespace: the server-sent-event tail of an
    # open conversation, remote checkpoint ingestion, and the explicit operator
    # seal. 0.4.0 added the authenticated browser capture endpoint
    # (``POST /v1/capture``); 0.3.0 added the contradiction, comparison, and
    # export read endpoints.
    assert API_VERSION == "0.10.0"


def test_top_level_star_surface_is_the_small_supported_sdk_kernel() -> None:
    assert set(earshot.__all__) == {
        "CaptureClass",
        "CapturePolicy",
        "Client",
        "ClientStatus",
        "SamplingDecision",
        "SdkConfig",
        "conversation",
        "flush",
        "get_client",
        "init",
        "pipeline",
        "session",
        "shutdown",
        "status",
        "suppress_instrumentation",
    }
    assert "IncidentBundle" not in earshot.__all__
    assert "IncidentRecorder" not in earshot.__all__
    assert earshot.IncidentBundle is not None  # compatibility; use earshot.contract in new code
