"""Drive the Gemini Live adapter over SYNTHETIC native speech-to-speech events.

Gemini Live is a native S2S runtime with no separately observable STT/LLM/TTS
boundary, so one model turn projects into exactly one fused `agent` operation. This
example needs no network and no API key: it replays the exact `BidiGenerateContent`
frame shapes the adapter reads (client turn stop, server audio, a tool call, turn
end with per-modality token usage) and builds a contract-valid incident that retains
only governed timing facts -- never transcript, audio, or tool-argument content.

    .venv/bin/python examples/provider_adapters/drive_gemini.py
"""

from __future__ import annotations

import pathlib

import earshot
from earshot.adapters.providers import GeminiLiveAdapter
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256

MODEL = "gemini-2.5-flash-native-audio"
OUTPUT = pathlib.Path(".earshot/provider_adapters/gemini_incident.json")

# Content sentinels planted in the synthetic frames. A correct adapter keeps none
# of these in the canonical incident.
PRIVATE_AUDIO = "base64-gemini-audio-must-not-survive"
PRIVATE_USER_TRANSCRIPT = "the user's spoken secret"
PRIVATE_AGENT_TRANSCRIPT = "the model's spoken reply"
PRIVATE_TOOL_ARG = "private-tool-argument"

# (payload, received_at_ms) pairs, timed on one application-monotonic clock. The
# client turn stop at 1000 ms and first server audio at 1410 ms anchor a 410 ms
# receipt-to-first-audio response latency.
EVENTS: list[tuple[dict[str, object], int]] = [
    ({"setupComplete": {}}, 900),
    # The application owns the upstream half of the bidi socket, so it observes its
    # own client turn stop -- Gemini emits no server-side speech-stopped message.
    ({"realtimeInput": {"activityEnd": {}}}, 1_000),
    (
        {
            "serverContent": {
                "modelTurn": {
                    "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": PRIVATE_AUDIO}}]
                },
                "inputTranscription": {"text": PRIVATE_USER_TRANSCRIPT},
                "outputTranscription": {"text": PRIVATE_AGENT_TRANSCRIPT},
            }
        },
        1_410,
    ),
    (
        {
            "toolCall": {
                "functionCalls": [
                    {"id": "call-1", "name": "lookup", "args": {"query": PRIVATE_TOOL_ARG}}
                ]
            }
        },
        1_450,
    ),
    (
        {
            "serverContent": {"turnComplete": True, "generationComplete": True},
            "usageMetadata": {
                "promptTokenCount": 42,
                "responseTokenCount": 17,
                "totalTokenCount": 59,
                "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 42}],
                "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 17}],
            },
        },
        1_500,
    ),
]


def main() -> int:
    adapter = GeminiLiveAdapter(model=MODEL)
    session = earshot.pipeline(session_id="gemini-synthetic")
    with session.turn() as turn:
        for payload, received_at_ms in EVENTS:
            adapter.adapt(payload, received_at_ms=received_at_ms).apply(turn)
    bundle = session.close()

    report = earshot.validate_incident(bundle)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(earshot.encode_incident_json(bundle, indent=2))

    measurements = sorted(
        {
            measurement.name
            for sample in bundle.profile.quality_samples
            for measurement in sample.measurements
        }
    )
    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    turn_metrics = analysis.projections.turns[0].metrics
    response = turn_metrics.response_latency
    canonical = earshot.encode_incident_protobuf(bundle)
    leaked = any(
        sentinel.encode() in canonical
        for sentinel in (
            PRIVATE_AUDIO,
            PRIVATE_USER_TRANSCRIPT,
            PRIVATE_AGENT_TRANSCRIPT,
            PRIVATE_TOOL_ARG,
        )
    )

    print("\n" + "=" * 68)
    print("SYNTHETIC GEMINI LIVE (NATIVE S2S) INCIDENT")
    print("=" * 68)
    print(f"  model                 : {MODEL}")
    print(f"  valid v1 contract     : {report.ok}  (errors={len(report.errors)})")
    print(f"  operations            : {[op.operation_name for op in bundle.profile.operations]}")
    print(f"  events                : {[event.event_name for event in bundle.profile.events]}")
    print(f"  measurements          : {measurements}")
    print(
        f"  response_latency      : {response.value} "
        f"({response.availability}, {response.confidence})"
    )
    print(f"  render coverage       : {turn_metrics.render_start_response_latency.availability}")
    print(f"  content retained?     : {'YES (LEAK!)' if leaked else 'no'}")
    print(f"  artifact              : {OUTPUT}")
    print("=" * 68)
    return 0 if report.ok and not leaked else 1


if __name__ == "__main__":
    raise SystemExit(main())
