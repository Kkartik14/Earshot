from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from earshot.adapters import LiveKitAdapter, PipecatAdapter
from earshot.api import create_app
from earshot.codec import encode_incident_protobuf
from earshot.contract import RetentionPolicy, TimePoint
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.storage import TURN_FACT_PROJECTION_VERSION, IncidentStore
from incident_factory import evidence, make_valid_bundle

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


def _with_expired_metadata(bundle):
    policies = tuple(
        policy.model_copy(
            update={"retention": RetentionPolicy(expires_at_unix_nano="0")}
        )
        if policy.capture_class == "metadata"
        else policy
        for policy in bundle.profile.privacy.capture_classes
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": policies})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


def _golden_runtime_bundles():
    golden = ROOT / "fixtures" / "golden"
    pipecat_fixture = json.loads((golden / "pipecat_spans.json").read_text())
    livekit_fixture = json.loads((golden / "livekit_metrics.json").read_text())

    pipecat_recorder = IncidentRecorder(
        session_id="turn-facts-pipecat",
        config=RecorderConfig(clock_domain_id="server-clock"),
    )
    pipecat = PipecatAdapter(pipecat_recorder, framework_version="golden")
    for span in pipecat_fixture["spans"]:
        pipecat.consume_span(span)
    for observed in pipecat_fixture["interruption_frames"]:
        pipecat.consume_interruption_frame(
            observed["frame"],
            observed_at=TimePoint.model_validate(observed["observed_at"]),
            bot_was_speaking=observed["bot_was_speaking"],
            interrupted_turn_id=observed["interrupted_turn_id"],
        )

    livekit_recorder = IncidentRecorder(
        session_id="turn-facts-livekit",
        config=RecorderConfig(clock_domain_id="server-clock"),
    )
    livekit = LiveKitAdapter(livekit_recorder, framework_version="golden")
    for item in livekit_fixture:
        if "metric" in item:
            livekit.consume_metric(
                item["metric"],
                observed_at=TimePoint.model_validate(item["observed_at"]),
            )
        elif "conversation_item" in item:
            livekit.consume_conversation_item(item["conversation_item"])
        else:
            livekit.consume_interruption_event(item["event"])
    return pipecat_recorder.close(), livekit_recorder.close()


def _with_stt_language(bundle, *, language: str = "hi-IN", probability: float = 0.95):
    llm = next(
        operation
        for operation in bundle.profile.operations
        if operation.operation_name == "llm"
    )
    stt = llm.model_copy(
        update={
            "operation_id": "op-stt-language",
            "operation_name": "stt",
            "trace_id": None,
            "span_id": None,
            "parent_span_id": None,
            "parent_scope": "unknown",
            "attributes": {
                "gen_ai.provider.name": "sarvam",
                "gen_ai.request.model": "saaras:v3",
                "earshot.language.code": language,
                "earshot.language.probability": probability,
            },
        }
    )
    return bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={"operations": (*bundle.profile.operations, stt)}
            )
        }
    )


def test_ingest_projects_queryable_turn_facts_with_per_metric_quality(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = make_valid_bundle(bundle_id="turn-fact-bundle")

    store.ingest(bundle, encode_incident_protobuf(bundle))

    facts = store.list_turn_facts()
    assert len(facts) == 1
    fact = facts[0]
    assert fact.bundle_id == "turn-fact-bundle"
    assert fact.session_id == "session-1"
    assert fact.turn_id == "turn-1"
    assert fact.framework == "pipecat"
    assert fact.model == "test-model"
    assert fact.first_token_ms == 150.0
    assert fact.first_token_availability == "available"
    assert fact.first_token_confidence == "measured"
    assert fact.response_ms == 720.0
    assert fact.response_confidence == "estimated"
    assert fact.eou_ms == 50.0
    assert fact.eou_basis == "speech_end_to_turn_commit"
    assert fact.stt_finalization_availability == "not_observed"
    assert fact.turn_duration_availability == "not_observed"
    assert fact.interruption_count is None
    assert fact.interruption_availability == "not_observed"
    assert fact.projection_version == TURN_FACT_PROJECTION_VERSION


def test_stt_language_is_a_queryable_fleet_dimension(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = _with_stt_language(make_valid_bundle(bundle_id="sarvam-language"))

    store.ingest(bundle, encode_incident_protobuf(bundle))

    [fact] = store.list_turn_facts()
    assert fact.language == "hi-IN"
    [summary] = store.summarize_turn_metric("response_ms", group_by="language")
    assert summary.group == "hi-IN"
    assert summary.turn_count == 1


def test_conflicting_stt_languages_remain_an_unknown_fleet_dimension(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = _with_stt_language(make_valid_bundle(bundle_id="ambiguous-language"))
    stt = next(
        operation
        for operation in bundle.profile.operations
        if operation.operation_name == "stt"
    )
    conflicting = stt.model_copy(
        update={
            "operation_id": "op-stt-language-conflict",
            "attributes": {
                **stt.attributes,
                "earshot.language.code": "bn-IN",
            },
        }
    )
    bundle = bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={"operations": (*bundle.profile.operations, conflicting)}
            )
        }
    )

    store.ingest(bundle, encode_incident_protobuf(bundle))

    [fact] = store.list_turn_facts()
    assert fact.language is None
    [summary] = store.summarize_turn_metric("response_ms", group_by="language")
    assert summary.group == "unknown"


def test_metrics_http_can_group_turns_by_stt_language(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = _with_stt_language(make_valid_bundle(bundle_id="sarvam-language-http"))
    store.ingest(bundle, encode_incident_protobuf(bundle))
    client = TestClient(create_app(store=store))

    response = client.get(
        "/v1/metrics/turns",
        params={"metric": "response_ms", "group_by": "language"},
    )

    assert response.status_code == 200
    assert response.json()["group_by"] == "language"
    assert [group["group"] for group in response.json()["groups"]] == ["hi-IN"]


def test_turn_facts_are_rebuilt_from_canonical_incidents_on_restart(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = make_valid_bundle(bundle_id="rebuild-turn-fact")
    store.ingest(bundle, encode_incident_protobuf(bundle))
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DELETE FROM turn_metrics")
    store.close()

    restarted = IncidentStore(tmp_path)

    assert [fact.bundle_id for fact in restarted.list_turn_facts()] == ["rebuild-turn-fact"]


def test_v7_turn_fact_projection_is_recreated_from_canonical_incidents(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = make_valid_bundle(bundle_id="migrated-turn-fact")
    store.ingest(bundle, encode_incident_protobuf(bundle))
    store.close()
    with sqlite3.connect(tmp_path / "earshot.sqlite3") as connection:
        connection.execute("DROP TABLE turn_metrics")
        connection.execute(
            """
            CREATE TABLE turn_metrics (
                bundle_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                first_token_ms REAL,
                PRIMARY KEY (bundle_id, turn_id)
            )
            """
        )
        connection.execute("PRAGMA user_version = 7")

    migrated = IncidentStore(tmp_path)

    [fact] = migrated.list_turn_facts()
    assert fact.bundle_id == "migrated-turn-fact"
    assert fact.eou_ms == 50.0
    assert fact.projection_version == TURN_FACT_PROJECTION_VERSION
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10


def test_v9_turn_facts_are_rebuilt_with_language_from_canonical_incidents(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = _with_stt_language(make_valid_bundle(bundle_id="migrated-language"))
    store.ingest(bundle, encode_incident_protobuf(bundle))
    store.close()
    with sqlite3.connect(tmp_path / "earshot.sqlite3") as connection:
        connection.execute("DROP INDEX turn_metrics_project_dimensions_idx")
        connection.execute("ALTER TABLE turn_metrics DROP COLUMN language")
        connection.execute("PRAGMA user_version = 9")

    migrated = IncidentStore(tmp_path)

    [fact] = migrated.list_turn_facts()
    assert fact.bundle_id == "migrated-language"
    assert fact.language == "hi-IN"
    with sqlite3.connect(migrated.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10


def test_turn_fact_queries_are_project_scoped(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    store.create_project("sales", display_name="Sales")
    bundle = make_valid_bundle(bundle_id="sales-turn-fact")
    store.ingest(
        bundle,
        encode_incident_protobuf(bundle),
        project_id="sales",
    )

    assert store.list_turn_facts() == ()
    assert [fact.bundle_id for fact in store.list_turn_facts(project_id="sales")] == [
        "sales-turn-fact"
    ]


def test_turn_fact_queries_purge_expired_derived_evidence(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = _with_expired_metadata(
        make_valid_bundle(bundle_id="expired-turn-fact")
    )
    store.ingest(bundle, encode_incident_protobuf(bundle))
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_metrics").fetchone()[0] == 1

    assert store.list_turn_facts() == ()
    assert store.summarize_turn_metric("first_token_ms") == ()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_metrics").fetchone()[0] == 0


def test_metrics_http_never_aggregates_expired_turn_facts(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = _with_expired_metadata(
        make_valid_bundle(bundle_id="expired-http-turn-fact")
    )
    store.ingest(bundle, encode_incident_protobuf(bundle))
    client = TestClient(create_app(store=store))

    response = client.get(
        "/v1/metrics/turns",
        params={"metric": "first_token_ms", "group_by": "framework"},
    )

    assert response.status_code == 200
    assert response.json()["groups"] == []


def test_turn_metric_summary_reports_nearest_rank_percentiles(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    for suffix in ("one", "two"):
        bundle = make_valid_bundle(bundle_id=f"summary-{suffix}")
        store.ingest(bundle, encode_incident_protobuf(bundle))

    groups = store.summarize_turn_metric("first_token_ms", group_by="framework")

    assert len(groups) == 1
    summary = groups[0]
    assert summary.group == "pipecat"
    assert summary.availability == "available"
    assert summary.basis == "first_token"
    assert summary.confidence == "measured"
    assert summary.limitation is None
    assert summary.turn_count == 2
    assert summary.available_count == 2
    assert summary.p50_ms == 150.0
    assert summary.p95_ms == 150.0


def test_metrics_http_interface_returns_project_fleet_summary(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    for suffix in ("one", "two"):
        bundle = make_valid_bundle(bundle_id=f"http-summary-{suffix}")
        store.ingest(bundle, encode_incident_protobuf(bundle))
    client = TestClient(create_app(store=store))

    response = client.get(
        "/v1/metrics/turns",
        params={"metric": "first_token_ms", "group_by": "framework"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "metric": "first_token_ms",
        "group_by": "framework",
        "groups": [
            {
                "group": "pipecat",
                "availability": "available",
                "basis": "first_token",
                "confidence": "measured",
                "limitation": None,
                "turn_count": 2,
                "available_count": 2,
                "average_ms": 150.0,
                "minimum_ms": 150.0,
                "maximum_ms": 150.0,
                "p50_ms": 150.0,
                "p95_ms": 150.0,
            }
        ],
    }


def test_fleet_summary_never_mixes_evidence_confidence_strata(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    measured = make_valid_bundle(bundle_id="measured-fact")
    token_event_index = next(
        index
        for index, event in enumerate(measured.profile.events)
        if event.event_name == "earshot.response.first_token"
    )
    estimated_events = list(measured.profile.events)
    token = estimated_events[token_event_index]
    estimated_events[token_event_index] = token.model_copy(
        update={"evidence": evidence(confidence="estimated")}
    )
    estimated = measured.model_copy(
        update={
            "profile": measured.profile.model_copy(
                update={
                    "manifest": measured.profile.manifest.model_copy(
                        update={"bundle_id": "estimated-fact"}
                    ),
                    "events": tuple(estimated_events),
                }
            )
        }
    )
    store.ingest(measured, encode_incident_protobuf(measured))
    store.ingest(estimated, encode_incident_protobuf(estimated))

    groups = store.summarize_turn_metric("first_token_ms")

    assert [(group.confidence, group.turn_count) for group in groups] == [
        ("estimated", 1),
        ("measured", 1),
    ]


def test_fleet_summary_never_mixes_unavailable_limitations(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    source = make_valid_bundle(bundle_id="source")
    target_missing = source.model_copy(
        update={
            "profile": source.profile.model_copy(
                update={
                    "manifest": source.profile.manifest.model_copy(
                        update={"bundle_id": "target-missing"}
                    ),
                    "events": tuple(
                        event
                        for event in source.profile.events
                        if event.event_name != "earshot.response.first_token"
                    ),
                }
            )
        }
    )
    anchor_missing = source.model_copy(
        update={
            "profile": source.profile.model_copy(
                update={
                    "manifest": source.profile.manifest.model_copy(
                        update={"bundle_id": "anchor-missing"}
                    ),
                    "operations": tuple(
                        operation.model_copy(update={"operation_name": "vad"})
                        if operation.operation_name == "turn_detection"
                        else operation
                        for operation in source.profile.operations
                    ),
                    "events": tuple(
                        event
                        for event in source.profile.events
                        if event.event_name
                        not in {"earshot.speech.ended", "earshot.turn.committed"}
                    ),
                }
            )
        }
    )
    store.ingest(target_missing, encode_incident_protobuf(target_missing))
    store.ingest(anchor_missing, encode_incident_protobuf(anchor_missing))

    groups = store.summarize_turn_metric("first_token_ms")

    assert {group.availability for group in groups} == {"not_observed"}
    assert {group.limitation for group in groups} == {
        "target_signal_not_observed",
        "turn_anchor_not_observed",
    }
    assert all(group.available_count == 0 for group in groups)


def test_ambiguous_turn_boundaries_never_publish_a_shortest_pair(tmp_path) -> None:
    bundle = make_valid_bundle(bundle_id="ambiguous-eou")
    speech_end = next(
        event
        for event in bundle.profile.events
        if event.event_name == "earshot.speech.ended"
    )
    duplicate = speech_end.model_copy(update={"event_id": "speech-end-duplicate"})
    bundle = bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={"events": (*bundle.profile.events, duplicate)}
            )
        }
    )
    store = IncidentStore(tmp_path)

    store.ingest(bundle, encode_incident_protobuf(bundle))

    [fact] = store.list_turn_facts()
    assert fact.eou_ms is None
    assert fact.eou_availability == "unavailable"
    assert fact.eou_limitation == "ambiguous_event_boundaries"


def test_cross_clock_turn_boundaries_preserve_explicit_unavailability(tmp_path) -> None:
    bundle = make_valid_bundle(bundle_id="cross-clock-eou")
    other_clock = bundle.profile.clock_domains[0].model_copy(
        update={"clock_domain_id": "other-clock"}
    )
    events = tuple(
        event.model_copy(
            update={
                "time": event.time.model_copy(
                    update={"clock_domain_id": "other-clock"}
                )
            }
        )
        if event.event_name == "earshot.turn.committed"
        else event
        for event in bundle.profile.events
    )
    bundle = bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={
                    "clock_domains": (*bundle.profile.clock_domains, other_clock),
                    "events": events,
                }
            )
        }
    )
    store = IncidentStore(tmp_path)

    store.ingest(bundle, encode_incident_protobuf(bundle))

    [fact] = store.list_turn_facts()
    assert fact.eou_ms is None
    assert fact.eou_availability == "unavailable"
    assert fact.eou_limitation == "cross_clock_domain"


def test_turn_provider_dimension_prefers_llm_stage_over_other_stage(tmp_path) -> None:
    store = IncidentStore(tmp_path)
    bundle = make_valid_bundle(bundle_id="provider-dimension")
    operations = []
    old_llm_id = next(
        operation.operation_id
        for operation in bundle.profile.operations
        if operation.operation_name == "llm"
    )
    new_llm_id = "zz-llm-operation"
    for operation in bundle.profile.operations:
        attributes = dict(operation.attributes)
        if operation.operation_name == "turn_detection":
            attributes["gen_ai.provider.name"] = "endpointing-provider"
        if operation.operation_name == "llm":
            attributes["gen_ai.provider.name"] = "llm-provider"
        operations.append(
            operation.model_copy(
                update={
                    "operation_id": (
                        new_llm_id if operation.operation_name == "llm" else operation.operation_id
                    ),
                    "attributes": attributes,
                }
            )
        )
    events = tuple(
        event.model_copy(
            update={
                "operation_id": new_llm_id
                if event.operation_id == old_llm_id
                else event.operation_id
            }
        )
        for event in bundle.profile.events
    )
    bundle = bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={"operations": tuple(operations), "events": events}
            )
        }
    )

    store.ingest(bundle, encode_incident_protobuf(bundle))

    assert store.list_turn_facts()[0].provider == "llm-provider"


def test_livekit_and_pipecat_goldens_persist_as_honest_project_scoped_facts(
    tmp_path,
) -> None:
    pipecat_bundle, livekit_bundle = _golden_runtime_bundles()
    store = IncidentStore(tmp_path)
    store.create_project("pipecat", display_name="Pipecat")
    store.create_project("livekit", display_name="LiveKit")
    store.ingest(
        pipecat_bundle,
        encode_incident_protobuf(pipecat_bundle),
        project_id="pipecat",
    )
    store.ingest(
        livekit_bundle,
        encode_incident_protobuf(livekit_bundle),
        project_id="livekit",
    )

    [pipecat] = store.list_turn_facts(project_id="pipecat")
    [livekit] = store.list_turn_facts(project_id="livekit")
    assert pipecat.response_ms == livekit.response_ms == 700.0
    assert pipecat.response_basis == livekit.response_basis == "provider_direct"
    assert pipecat.response_confidence == livekit.response_confidence == "measured"
    assert (
        pipecat.response_limitation
        == livekit.response_limitation
        == "server_output_excludes_delivery_and_render"
    )

    assert pipecat.first_token_ms == 100.0
    assert pipecat.first_token_basis == "provider_stage_direct"
    assert pipecat.first_token_limitation == "stage_local_excludes_turn_scheduling"
    assert livekit.first_token_ms == 360.0
    assert livekit.first_token_basis == "first_token"

    assert pipecat.stt_finalization_availability == "not_observed"
    assert pipecat.eou_availability == "not_observed"
    assert pipecat.turn_duration_ms == 1000.0
    assert pipecat.turn_duration_basis == "native_turn_lifecycle"
    assert livekit.stt_finalization_ms == 50.0
    assert livekit.stt_finalization_basis == "provider_transcription_delay"
    assert livekit.eou_ms == 200.0
    assert livekit.eou_basis == "provider_endpointing_delay"
    assert livekit.turn_duration_availability == "not_observed"

    assert pipecat.interruption_count == 1
    assert pipecat.interruption_availability == "available"
    assert livekit.interruption_count is None
    assert livekit.interruption_availability == "unavailable"
    assert livekit.interruption_limitation == "turn_correlation_not_observed"

    assert len(store.summarize_turn_metric("response_ms", project_id="pipecat")) == 1
    assert len(store.summarize_turn_metric("response_ms", project_id="livekit")) == 1
    assert store.list_turn_facts() == ()


def test_interruption_count_counts_accepted_outcomes_not_lifecycle_events(tmp_path) -> None:
    pipecat_bundle, _ = _golden_runtime_bundles()
    accepted = next(
        event
        for event in pipecat_bundle.profile.events
        if event.event_name == "earshot.interruption.accepted"
    )
    detected = accepted.model_copy(
        update={
            "event_id": "interruption-detected-before-accepted",
            "event_name": "earshot.interruption.detected",
        }
    )
    bundle = pipecat_bundle.model_copy(
        update={
            "profile": pipecat_bundle.profile.model_copy(
                update={
                    "manifest": pipecat_bundle.profile.manifest.model_copy(
                        update={"bundle_id": "interruption-lifecycle"}
                    ),
                    "events": (*pipecat_bundle.profile.events, detected),
                }
            )
        }
    )
    store = IncidentStore(tmp_path)

    store.ingest(bundle, encode_incident_protobuf(bundle))

    [fact] = store.list_turn_facts()
    assert fact.interruption_count == 1
    assert fact.interruption_basis == "accepted_interruption_events"
