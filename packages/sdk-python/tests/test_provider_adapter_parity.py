"""Equivalent raw-provider pipelines project the same governed turn facts."""

from __future__ import annotations

import earshot
from earshot.adapters.providers import CartesiaAdapter, DeepgramAdapter, SarvamAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256

START = 1_752_800_000_000_000_000
IDENTITY_KEY = b"provider-parity-identity-key-32"


def _cartesia_first_audio() -> dict[str, object]:
    return {
        "type": "chunk",
        "context_id": "private-context",
        "data": "private-audio",
        "step_time": 12,
    }


def _analyze_stt_tts_pair(stt_provider: str):
    session = earshot.pipeline(
        session_id=f"parity-{stt_provider}",
        started_at_unix_nano=START,
    )
    cartesia = CartesiaAdapter(model="sonic-3", identity_key=IDENTITY_KEY)
    with session.turn() as turn:
        if stt_provider == "deepgram":
            DeepgramAdapter(model="nova-3", identity_key=IDENTITY_KEY).adapt(
                {
                    "type": "Results",
                    "start": 0,
                    "duration": 1,
                    "is_final": True,
                    "speech_final": True,
                    "channel": {"alternatives": [{"transcript": "private", "confidence": 0.9}]},
                    "metadata": {"request_id": "private-deepgram-request"},
                },
                received_at_ms=400,
            ).apply(turn)
        elif stt_provider == "sarvam":
            SarvamAdapter(identity_key=IDENTITY_KEY).adapt(
                {
                    "type": "data",
                    "data": {
                        "request_id": "private-sarvam-request",
                        "transcript": "private",
                        "language_code": "en-IN",
                        "metrics": {
                            "audio_duration": 1,
                            "processing_latency": 0.08,
                        },
                    },
                },
                received_at_ms=400,
            ).apply(turn)
        else:  # pragma: no cover - test helper contract
            raise AssertionError(f"unsupported provider: {stt_provider}")
        cartesia.adapt(
            _cartesia_first_audio(),
            request_sent_at_ms=400,
            received_at_ms=500,
        ).apply(turn)
    bundle = session.close()
    analysis = analyze_incident(
        bundle,
        input_sha256=analysis_input_sha256(bundle),
        generated_at_unix_nano=1,
    )
    return bundle, analysis.projections.turns[0]


def test_deepgram_and_sarvam_pipelines_share_tts_projection_semantics() -> None:
    deepgram_bundle, deepgram_turn = _analyze_stt_tts_pair("deepgram")
    sarvam_bundle, sarvam_turn = _analyze_stt_tts_pair("sarvam")

    for bundle in (deepgram_bundle, sarvam_bundle):
        assert earshot.validate_incident(bundle).ok
    deepgram_metric = deepgram_turn.metrics.generated_response_latency
    sarvam_metric = sarvam_turn.metrics.generated_response_latency
    assert deepgram_metric.model_dump(exclude={"evidence_ids"}) == sarvam_metric.model_dump(
        exclude={"evidence_ids"}
    )
    assert deepgram_metric.value == 100
    assert deepgram_metric.confidence == "estimated"
    assert deepgram_bundle.profile.coverage == sarvam_bundle.profile.coverage
    assert deepgram_bundle.profile.coverage[0].availability == "not_observed"
    deepgram_native = {
        measurement.name
        for sample in deepgram_bundle.profile.quality_samples
        for measurement in sample.measurements
        if measurement.name.startswith("deepgram.stt.")
    }
    sarvam_native = {
        measurement.name
        for sample in sarvam_bundle.profile.quality_samples
        for measurement in sample.measurements
        if measurement.name.startswith("sarvam.stt.")
    }
    assert deepgram_native == {
        "deepgram.stt.segment_start",
        "deepgram.stt.segment_duration",
        "deepgram.stt.transcript_confidence",
    }
    assert sarvam_native == {
        "sarvam.stt.audio_duration",
        "sarvam.stt.processing_latency",
    }
    assert deepgram_native.isdisjoint(sarvam_native)
