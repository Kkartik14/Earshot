from __future__ import annotations

import tomllib
from pathlib import Path

import earshot
from earshot.analysis import ANALYZER_VERSION
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
    assert ANALYZER_VERSION == "0.4.0"


def test_explanation_identity_contract_is_api_version_0_2() -> None:
    assert API_VERSION == "0.2.0"


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
