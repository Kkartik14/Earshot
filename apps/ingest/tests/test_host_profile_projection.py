"""End-to-end host-profile transcript, privacy, and cost projection tests."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("soundfile")

from captured_livekit_otlp import livekit_manifest, livekit_requests
from earshot.api import create_app
from earshot.call_capture import spans_from_request
from earshot.storage_prices import NewPrice, PriceStore
from otlp_support import request as http_request
from otlp_support import serve
from projection_job_support import SECOND, START, World, gaps
from store_support import catalog, catalog_rows, stored_needles

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
LIVEKIT_AUDIO = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8" / "audio" / "recording.ogg"

# --- the host profile --------------------------------------------------------------------------


def _host_turns(**stage_attributes):
    """LiveKit's three turns at their own anchors, as a host exports them."""

    from earshot.call_span_facts import turn_anchors
    from host_profile_support import HostStage, HostTurn

    turns = []
    livekit = [span for item in livekit_requests() for span in spans_from_request(item)]
    for number, anchor in enumerate(turn_anchors(livekit), start=1):
        user_on = min(anchor.user_on_unix_nano)
        turns.append(
            HostTurn(
                f"turn-{number}",
                user_on,
                anchor.agent_on_unix_nano + SECOND,
                user_speech=((user_on, anchor.user_off_unix_nano),),
                agent_speech=(anchor.agent_on_unix_nano, anchor.agent_on_unix_nano + SECOND),
                stages=(
                    HostStage(
                        "llm",
                        anchor.user_off_unix_nano,
                        anchor.agent_on_unix_nano,
                        {"gen_ai.provider.name": "openai", **stage_attributes},
                    ),
                ),
                reported_latency_ms=anchor.reported_latency_ms,
            )
        )
    return turns


def test_live_turns_before_close_match_host_profile_revision(tmp_path) -> None:
    from host_profile_support import host_request

    world = World(tmp_path)
    call_id = world.replay([host_request(_host_turns())])

    with serve(create_app(store=world.store)) as port:
        before_close = http_request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        world.close()
        world.project()
        after_close = http_request(port, "GET", f"/v1/calls/{call_id}/live-turns")
        detail = http_request(port, "GET", f"/v1/calls/{call_id}")

    assert before_close.status == after_close.status == detail.status == 200
    assert before_close.json()["state"] == "open"
    assert before_close.json()["revision"] is None
    assert after_close.json()["state"] == "closed"
    assert after_close.json()["revision"] == detail.json()["latest_revision"]["revision"] == 1
    assert before_close.json()["turns"] == after_close.json()["turns"]
    assert after_close.json()["turns"] == detail.json()["projection"]["turns"]


def test_a_host_call_with_its_stereo_recording_is_projected_with_measured_turns(tmp_path) -> None:
    from host_profile_support import HOST_SESSION_KEY, host_request

    world = World(tmp_path)
    call_id = world.replay([host_request(_host_turns())])
    audio = livekit_manifest()["audio"]
    world.store_recording(
        "host.stereo",
        LIVEKIT_AUDIO.read_bytes(),
        {
            "session_key": HOST_SESSION_KEY,
            "speaker": "mixed",
            "channel_speakers": ("user", "agent"),
            "content_type": "audio/ogg",
            "started_at_unix_nano": audio["started_at_unix_nano"],
        },
        duration_ms=34_004,
    )
    world.close()
    world.analyse()
    world.project()

    # The gaps the LiveKit call measures from the same audio and the same anchors.
    assert gaps(world, call_id) == [
        ("measured", 1290, None),
        ("measured", 1660, None),
        ("measured", 1250, None),
    ]
    projection = world.projection(call_id)
    assert projection["state"] == "current"
    assert projection["summary"].framework == "host"
    assert [turn["turn_id"] for turn in projection["detail"]["turns"]] == [
        "turn-1",
        "turn-2",
        "turn-3",
    ]
    assert projection["detail"]["transcript"] == []
    assert catalog_rows(tmp_path, "SELECT count(*) FROM call_transcripts") == [(0,)]
    [(finality, completeness)] = catalog_rows(
        tmp_path, "SELECT finality, completeness FROM incidents"
    )
    assert (finality, completeness) == ("final", "complete")


def test_a_host_call_delivered_twice_is_the_same_call_with_the_same_projection(tmp_path) -> None:
    from host_profile_support import host_request

    once, twice = World(tmp_path / "once"), World(tmp_path / "twice")
    first = once.replay([host_request(_host_turns())])
    second = twice.replay([host_request(_host_turns()), host_request(_host_turns())])
    for world in (once, twice):
        world.close()
        world.project()

    def projected(world: World, call_id: str) -> dict:
        detail = world.projection(call_id)["detail"]
        return {key: detail[key] for key in ("turns", "signals", "cost_items", "transcript")}

    assert projected(twice, second) == projected(once, first)
    for world in (once, twice):
        assert catalog_rows(world.path, "SELECT count(*) FROM calls") == [(1,)]
        assert catalog_rows(world.path, "SELECT count(*) FROM call_revisions") == [(1,)]
    assert catalog_rows(twice.path, "SELECT count(*) FROM spans") == catalog_rows(
        once.path, "SELECT count(*) FROM spans"
    )


# Content a host must not send, on host spans: under metadata_only none of it, and not
# the session key, is stored anywhere.
_STRAY_REPLY = "A reply the agent spoke, which a host must not export"


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_metadata_only_stores_no_stray_content_from_host_spans(tmp_path, preset) -> None:
    from host_profile_support import HOST_SESSION_KEY, host_request

    world = World(tmp_path)
    world.store.set_capture_preset("default", preset)
    world.replay([host_request(_host_turns(**{"gen_ai.output.messages": _STRAY_REPLY}))])
    world.close()
    world.project()
    world.store.close()

    needles = [HOST_SESSION_KEY.encode(), _STRAY_REPLY.encode()]
    found = stored_needles(tmp_path, needles)
    if preset == "metadata_only":
        assert found == set()
    else:  # the control: the same search finds both when they are stored
        assert found == set(needles)


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_host_profile_transcript_privacy_and_call_deletion(tmp_path, preset) -> None:
    from earshot.codec import decode_incident_protobuf
    from host_profile_support import HostTurn, host_request

    user_text = "HOST_TRANSCRIPT_USER_D5_SENTINEL_62f7"
    agent_text = "HOST_TRANSCRIPT_AGENT_D5_SENTINEL_91ac"
    turn = HostTurn(
        "turn-1",
        START,
        START + 3 * SECOND,
        user_speech=((START, START + SECOND),),
        user_transcripts=(user_text,),
        agent_speech=(START + 2 * SECOND, START + 3 * SECOND),
        agent_transcript=agent_text,
    )
    world = World(tmp_path)
    world.store.set_capture_preset("default", preset)
    call_id = world.replay([host_request([turn], scope_version="0.2.0")])
    world.close()
    world.project()

    call = world.reads.get_call("default", call_id)
    assert call is not None
    _, payload = world.store.get_artifact(f"{call_id}.r1")
    revision = decode_incident_protobuf(payload)
    captured_text = [
        operation.attributes.get("earshot.transcript.text")
        for operation in revision.profile.operations
        if "earshot.transcript.text" in operation.attributes
    ]
    needles = [user_text.encode(), agent_text.encode()]

    if preset == "full":
        assert {line.text for line in call.transcript} == {user_text, agent_text}
        assert {line["text"] for line in call.projection_detail["transcript"]} == {
            user_text,
            agent_text,
        }
        assert set(captured_text) == {user_text, agent_text}
        assert call.transcript_coverage == "available"
        assert stored_needles(tmp_path, needles) == set(needles)
    else:
        assert call.transcript == ()
        assert call.projection_detail["transcript"] == []
        assert captured_text == []
        assert call.transcript_coverage == "not_captured_by_preset"
        [coverage] = [
            item for item in revision.profile.coverage if item.signal == "call.transcript"
        ]
        assert coverage.reason == "not_captured_by_preset"
        [policy] = [
            item
            for item in revision.profile.privacy.capture_classes
            if item.capture_class == "transcript"
        ]
        assert (policy.decision, policy.captured) == ("deny", False)
        assert stored_needles(tmp_path, needles) == set()

    world.purger.purge_call("default", call_id)
    world.store.close()
    assert stored_needles(tmp_path, needles) == set()


def test_one_call_projects_both_supported_host_profile_versions(tmp_path) -> None:
    from host_profile_support import HOST_SESSION_KEY, HostStage, HostTurn, host_request

    legacy_turn = HostTurn(
        "turn-legacy",
        START,
        START + 2 * SECOND,
        user_speech=((START, START + SECOND),),
        user_transcripts=("This legacy transcript is not defined by 0.1.0.",),
        reported_latency_ms=850,
        stages=(
            HostStage(
                "stt",
                START,
                START + SECOND,
                {
                    "gen_ai.provider.name": "legacy-provider",
                    "gen_ai.request.model": "legacy-model",
                    "earshot.stt.usage.audio_seconds": 19,
                },
            ),
        ),
    )
    current_start = START + 5 * SECOND
    current_turn = HostTurn(
        "turn-current",
        current_start,
        current_start + 2 * SECOND,
        user_speech=((current_start, current_start + SECOND),),
        user_transcripts=("The 0.2.0 transcript reaches the call.",),
        stages=(
            HostStage(
                "stt",
                current_start,
                current_start + SECOND,
                {
                    "gen_ai.provider.name": "current-provider",
                    "gen_ai.request.model": "current-model",
                    "earshot.stt.usage.audio_seconds": 3,
                },
            ),
        ),
    )
    legacy = host_request([legacy_turn], session_key=HOST_SESSION_KEY, scope_version="0.1.0")
    current = host_request([current_turn], session_key=HOST_SESSION_KEY, scope_version="0.2.0")
    for span in current.resource_spans[0].scope_spans[0].spans:
        span.trace_id = (0x49).to_bytes(16, "big")

    world = World(tmp_path)
    call_id = world.replay([legacy, current])
    world.close()
    world.project()

    call = world.reads.get_call("default", call_id)
    assert call is not None
    assert [line.text for line in call.transcript] == ["The 0.2.0 transcript reaches the call."]
    assert call.summary.projection is not None
    assert call.summary.projection.reported_latency_max_ms == 850
    [(usage_json,)] = catalog_rows(
        tmp_path,
        "SELECT usage_json FROM call_projections WHERE call_id = ?",
        (call_id,),
    )
    usage = json.loads(usage_json)
    [current_stt] = [
        item
        for item in usage
        if item["operation"] == "stt"
        and item["provider"] == "current-provider"
        and item["kind"] == "reported"
    ]
    assert current_stt["quantity_milli"] == 3_000
    assert not any(
        item["operation"] == "stt"
        and item["provider"] == "legacy-provider"
        and item["kind"] == "reported"
        for item in usage
    )


def test_host_transcript_over_64_kib_is_omitted_from_revision_and_call_index(tmp_path) -> None:
    from earshot.codec import decode_incident_protobuf
    from host_profile_support import HostTurn, host_request

    text = "HOST_TRANSCRIPT_SIZE_D5_SENTINEL_" + "x" * 65_536
    turn = HostTurn(
        "turn-1",
        START,
        START + 2 * SECOND,
        user_speech=((START, START + SECOND),),
        user_transcripts=(text,),
    )
    world = World(tmp_path)
    call_id = world.replay([host_request([turn], scope_version="0.2.0")])
    world.close()
    world.project()

    call = world.reads.get_call("default", call_id)
    assert call is not None
    assert call.transcript == ()
    assert call.projection_detail["transcript"] == []
    assert call.transcript_coverage == "not_observed"
    assert (
        catalog_rows(tmp_path, "SELECT text FROM call_transcripts WHERE call_id = ?", (call_id,))
        == []
    )

    _, payload = world.store.get_artifact(f"{call_id}.r1")
    revision = decode_incident_protobuf(payload)
    [omission] = [
        item for item in revision.profile.privacy.omissions if item.capture_class == "transcript"
    ]
    assert omission.count == 1
    assert not any(
        operation.attributes.get("earshot.transcript.text") == text
        for operation in revision.profile.operations
    )

    needle = b"HOST_TRANSCRIPT_SIZE_D5_SENTINEL_"
    world.purger.purge_call("default", call_id)
    world.store.close()
    assert stored_needles(tmp_path, [needle]) == set()


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_host_reported_usage_is_durable_and_repriced_without_reading_source_spans(
    tmp_path, preset
) -> None:
    from earshot.codec import decode_incident_protobuf
    from host_profile_support import HostStage, HostTurn, host_request

    turn = HostTurn(
        "turn-usage",
        START,
        START + 4 * SECOND,
        user_speech=((START, START + SECOND),),
        user_transcripts=("HOST_USAGE_TRANSCRIPT_CONTENT_D5_SENTINEL",),
        stages=(
            HostStage(
                "stt",
                START,
                START + SECOND,
                {
                    "gen_ai.provider.name": "deepgram",
                    "gen_ai.request.model": "nova-3",
                    "earshot.stt.usage.audio_seconds": 0.375,
                    "audio.data": "HOST_USAGE_AUDIO_CONTENT_D5_SENTINEL",
                },
            ),
            HostStage(
                "stt",
                START + SECOND,
                START + 2 * SECOND,
                {
                    "gen_ai.provider.name": "e7-precision-test",
                    "gen_ai.request.model": "synthetic",
                    "earshot.stt.usage.audio_seconds": 0.0004,
                },
            ),
            HostStage(
                "tts",
                START + 2 * SECOND,
                START + 3 * SECOND,
                {
                    "gen_ai.provider.name": "cartesia",
                    "gen_ai.request.model": "sonic-3",
                    "earshot.tts.usage.character_count": 217,
                },
            ),
        ),
    )
    world = World(tmp_path)
    world.store.set_capture_preset("default", preset)
    call_id = world.replay([host_request([turn], scope_version="0.2.0")])
    world.close()
    world.project()

    _, revision_payload = world.store.get_artifact(f"{call_id}.r1")
    revision = decode_incident_protobuf(revision_payload)
    stt_attributes = [
        operation.attributes
        for operation in revision.profile.operations
        if operation.operation_name == "stt"
    ]
    assert any(
        attributes["earshot.stt.usage.audio_seconds"] == 0.375 for attributes in stt_attributes
    )
    tts_attributes = next(
        operation.attributes
        for operation in revision.profile.operations
        if operation.operation_name == "tts"
    )
    assert tts_attributes["earshot.tts.usage.character_count"] == 217

    content_needles = [
        b"HOST_USAGE_TRANSCRIPT_CONTENT_D5_SENTINEL",
        b"HOST_USAGE_AUDIO_CONTENT_D5_SENTINEL",
    ]
    if preset == "metadata_only":
        assert not any(
            "earshot.transcript.text" in operation.attributes
            or "audio.data" in operation.attributes
            for operation in revision.profile.operations
        )
        assert stored_needles(tmp_path, content_needles) == set()
    else:
        assert any(
            operation.attributes.get("earshot.transcript.text")
            == "HOST_USAGE_TRANSCRIPT_CONTENT_D5_SENTINEL"
            for operation in revision.profile.operations
        )
        assert any(
            operation.attributes.get("audio.data") == "HOST_USAGE_AUDIO_CONTENT_D5_SENTINEL"
            for operation in revision.profile.operations
        )
        assert stored_needles(tmp_path, content_needles) == set(content_needles)

    [(usage_json,)] = catalog_rows(
        tmp_path,
        "SELECT usage_json FROM call_projections WHERE call_id = ?",
        (call_id,),
    )
    stored_usage = json.loads(usage_json)
    stt_usage = {
        item["provider"]: item
        for item in stored_usage
        if item["operation"] == "stt" and item["kind"] == "reported"
    }
    [tts_usage] = [item for item in stored_usage if item["operation"] == "tts"]
    assert stt_usage["deepgram"]["quantity_milli"] == 375
    assert stt_usage["e7-precision-test"]["quantity_milli"] == 1
    assert all("quantity_decimal" not in item for item in stored_usage)
    assert (tts_usage["kind"], tts_usage["quantity_milli"]) == ("reported", 217_000)

    with catalog(tmp_path) as connection:
        connection.execute("UPDATE spans SET payload = ?", (bytes((255,)),))
    prices = PriceStore(world.store)
    prices.add(
        "default",
        NewPrice(
            provider="deepgram",
            model="nova-3",
            unit="audio_second",
            price_nano_usd=6_000_000_000,
            per_quantity=60,
            effective_from_unix_nano=0,
            source_url=None,
            note=None,
        ),
    )
    prices.add(
        "default",
        NewPrice(
            provider="e7-precision-test",
            model="synthetic",
            unit="audio_second",
            price_nano_usd=1_000_000_000,
            per_quantity=1,
            effective_from_unix_nano=0,
            source_url=None,
            note=None,
        ),
    )
    prices.add(
        "default",
        NewPrice(
            provider="cartesia",
            model="sonic-3",
            unit="character",
            price_nano_usd=1_000_000,
            per_quantity=1000,
            effective_from_unix_nano=0,
            source_url=None,
            note=None,
        ),
    )

    assert world.project() == 1
    projection = world.projection(call_id)
    assert projection["state"] == "current"
    stt_item = next(
        item
        for item in projection["detail"]["cost_items"]
        if item["unit"] == "audio_second" and item["provider"] == "deepgram"
    )
    precision_item = next(
        item
        for item in projection["detail"]["cost_items"]
        if item["unit"] == "audio_second" and item["provider"] == "e7-precision-test"
    )
    tts_item = next(
        item for item in projection["detail"]["cost_items"] if item["unit"] == "character"
    )
    assert (stt_item["status"], stt_item["quantity"], stt_item["cost_nano_usd"]) == (
        "priced",
        0.375,
        37_500_000,
    )
    assert (
        precision_item["status"],
        precision_item["quantity"],
        precision_item["cost_nano_usd"],
    ) == ("priced", 0.001, 1_000_000)
    from earshot.api import CostItemResponse

    assert (
        json.loads(CostItemResponse.model_validate(precision_item).model_dump_json())["quantity"]
        == 0.001
    )
    assert (tts_item["status"], tts_item["quantity"], tts_item["cost_nano_usd"]) == (
        "priced",
        217,
        217_000,
    )
    [(repriced_usage,)] = catalog_rows(
        tmp_path,
        "SELECT usage_json FROM call_projections WHERE call_id = ?",
        (call_id,),
    )
    assert json.loads(repriced_usage) == stored_usage


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
@pytest.mark.parametrize(
    (
        "reported_seconds",
        "expected_api_quantity",
        "expected_repriced_status",
    ),
    [
        pytest.param((1e308, 1e308, 0.001), None, "no_price", id="fractional-overflow"),
        pytest.param((1e308, 1e308), None, "no_price", id="whole-overflow"),
        pytest.param(
            (float(2**53), 1.0),
            None,
            "priced",
            id="finite-integer-rounded-by-api-float",
        ),
        pytest.param((1e308, 0.001), None, "no_price", id="finite-fraction-rounded-by-api-float"),
        pytest.param((1e308,), None, "no_price", id="large-finite-whole-seconds"),
    ],
)
def test_large_reported_stt_usage_quantizes_and_reprices_without_overflow(
    tmp_path,
    preset,
    reported_seconds,
    expected_api_quantity,
    expected_repriced_status,
) -> None:
    from host_profile_support import HostStage, HostTurn, host_request

    stages = tuple(
        HostStage(
            "stt",
            START + index * SECOND,
            START + (index + 1) * SECOND,
            {
                "gen_ai.provider.name": "deepgram",
                "gen_ai.request.model": "nova-3",
                "earshot.stt.usage.audio_seconds": seconds,
            },
        )
        for index, seconds in enumerate(reported_seconds)
    )
    turn = HostTurn(
        "turn-large-stt-usage",
        START,
        START + (len(stages) + 1) * SECOND,
        stages=stages,
    )
    world = World(tmp_path)
    world.store.set_capture_preset("default", preset)
    call_id = world.replay([host_request([turn], scope_version="0.2.0")])
    world.close()

    world.project()

    call = world.reads.get_call("default", call_id)
    assert call is not None
    assert call.summary.projection_state == "current"
    assert call.summary.projection is not None
    [(usage_json,)] = catalog_rows(
        tmp_path,
        "SELECT usage_json FROM call_projections WHERE project_id = ? AND call_id = ?",
        ("default", call_id),
    )
    usage = json.loads(usage_json)
    [reported_stt] = [
        item for item in usage if item["operation"] == "stt" and item["kind"] == "reported"
    ]
    assert reported_stt["quantity_milli"] > 0
    assert "quantity_decimal" not in reported_stt
    [stt_cost] = [
        item for item in call.projection_detail["cost_items"] if item["unit"] == "audio_second"
    ]
    assert stt_cost["status"] == "no_price"
    if expected_api_quantity is None:
        assert stt_cost["quantity"] is None
    else:
        assert isinstance(stt_cost["quantity"], float)
        assert math.isfinite(stt_cost["quantity"])
        assert stt_cost["quantity"] == expected_api_quantity

    # Repricing uses the durable millisecond quantity, not source-span reads.
    with catalog(tmp_path) as connection:
        connection.execute(
            "UPDATE spans SET payload = ? WHERE trace_id IN "
            "(SELECT trace_id FROM call_traces WHERE project_id = ? AND call_id = ?)",
            (bytes((255,)), "default", call_id),
        )
    PriceStore(world.store).add(
        "default",
        NewPrice(
            provider="deepgram",
            model="nova-3",
            unit="audio_second",
            price_nano_usd=1,
            per_quantity=1,
            effective_from_unix_nano=0,
            source_url=None,
            note=None,
        ),
    )
    world.project()

    repriced = world.reads.get_call("default", call_id)
    assert repriced is not None
    assert repriced.summary.projection_state == "current"
    assert repriced.summary.projection is not None
    [repriced_stt] = [
        item for item in repriced.projection_detail["cost_items"] if item["unit"] == "audio_second"
    ]
    assert repriced_stt["status"] == expected_repriced_status
    assert repriced_stt["quantity"] == expected_api_quantity
    [(repriced_usage_json,)] = catalog_rows(
        tmp_path,
        "SELECT usage_json FROM call_projections WHERE project_id = ? AND call_id = ?",
        ("default", call_id),
    )
    assert json.loads(repriced_usage_json) == usage
    assert world.projector.failure("default", call_id) is None


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_host_max_json_safe_tts_usage_is_exact_in_revision_and_durable_projection(
    tmp_path, preset
) -> None:
    from earshot.codec import decode_incident_protobuf
    from host_profile_support import HostStage, HostTurn, host_request

    max_json_safe_integer = 2**53 - 1
    turn = HostTurn(
        "turn-max-json-safe-tts",
        START,
        START + 2 * SECOND,
        stages=(
            HostStage(
                "tts",
                START,
                START + SECOND,
                {
                    "gen_ai.provider.name": "e7-max-json-safe-test",
                    "gen_ai.request.model": "synthetic",
                    "earshot.tts.usage.character_count": max_json_safe_integer,
                },
            ),
        ),
    )
    world = World(tmp_path)
    world.store.set_capture_preset("default", preset)
    call_id = world.replay([host_request([turn], scope_version="0.2.0")])
    world.close()
    world.project()

    _, revision_payload = world.store.get_artifact(f"{call_id}.r1")
    revision = decode_incident_protobuf(revision_payload)
    [tts] = [
        operation for operation in revision.profile.operations if operation.operation_name == "tts"
    ]
    assert tts.attributes["earshot.tts.usage.character_count"] == max_json_safe_integer

    [(usage_json,)] = catalog_rows(
        tmp_path,
        "SELECT usage_json FROM call_projections WHERE call_id = ?",
        (call_id,),
    )
    [tts_usage] = [item for item in json.loads(usage_json) if item["operation"] == "tts"]
    assert tts_usage["quantity_milli"] == max_json_safe_integer * 1000


@pytest.mark.parametrize("preset", ["full", "metadata_only"])
def test_host_tts_above_json_safe_range_is_omitted_and_not_billed(tmp_path, preset) -> None:
    from hashlib import sha256

    from earshot.codec import decode_incident_protobuf
    from host_profile_support import HostStage, HostTurn, host_request

    turn = HostTurn(
        "turn-oversized-tts",
        START,
        START + 2 * SECOND,
        stages=(
            HostStage(
                "tts",
                START,
                START + SECOND,
                {
                    "gen_ai.provider.name": "e7-max-int64-test",
                    "gen_ai.request.model": "synthetic",
                    "earshot.tts.usage.character_count": 2**53,
                },
            ),
        ),
    )
    world = World(tmp_path)
    world.store.set_capture_preset("default", preset)
    call_id = world.replay([host_request([turn], scope_version="0.2.0")])
    world.close()
    world.project()

    _, revision_payload = world.store.get_artifact(f"{call_id}.r1")
    revision = decode_incident_protobuf(revision_payload)
    [tts] = [
        operation for operation in revision.profile.operations if operation.operation_name == "tts"
    ]
    assert "earshot.tts.usage.character_count" not in tts.attributes
    [omission] = [
        item
        for item in revision.profile.privacy.omissions
        if item.attributes.get("field_key_sha256")
        == sha256(b"earshot.tts.usage.character_count").hexdigest()
    ]
    assert (omission.capture_class, omission.reason, omission.count) == (
        "metadata",
        "adapter_payload_omitted",
        1,
    )

    [(usage_json,)] = catalog_rows(
        tmp_path,
        "SELECT usage_json FROM call_projections WHERE call_id = ?",
        (call_id,),
    )
    [tts_usage] = [item for item in json.loads(usage_json) if item["operation"] == "tts"]
    assert (tts_usage["kind"], tts_usage["quantity_milli"]) == ("not_reported", None)
