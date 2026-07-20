"""Show EVERYTHING Earshot surfaces for a self-built cascade pipeline.

Models the common case: your own STT (Groq Whisper) -> your LLM -> your voice
(Cartesia), wired together by hand. Builds two turns (one with a barge-in), then
prints the three views Earshot exposes today: per-incident detail, per-turn
derived metrics, and the cross-session fleet summary.

    .venv2/bin/python examples/provider_adapters/inspect_pipeline.py
"""

from __future__ import annotations

import os
import tempfile

import earshot
from earshot.analysis import analyze_incident
from earshot.codec import analysis_input_sha256
from earshot.storage import IncidentStore

VOICE = os.environ.get("CARTESIA_VOICE_ID", "fd2ada67-c2d9-4afe-b474-6386b87d8fc3")


def _build() -> earshot.IncidentBundle:
    sess = earshot.pipeline(session_id="support-call-42", framework="custom_pipeline")
    with sess.turn() as t:
        t.vad(speech_start_ms=0, speech_end_ms=1800)
        t.stt("groq", model="whisper-large-v3-turbo", ttfb_ms=210, final_ms=320)
        t.llm("groq", model="llama-3.1-8b-instant", ttft_ms=280, completion_ms=540)
        t.tts("cartesia", model="sonic-2", voice=VOICE, ttfb_ms=120, first_audio_ms=140)
    with sess.turn() as t:
        t.stt("groq", model="whisper-large-v3-turbo", ttfb_ms=230, final_ms=300)
        t.llm("groq", model="llama-3.1-8b-instant", ttft_ms=310, completion_ms=610)
        t.tts("cartesia", model="sonic-2", voice=VOICE, ttfb_ms=135, first_audio_ms=160)
        t.barge_in(at_ms=90, accepted=True)
    return sess.close()


def main() -> int:
    bundle = _build()
    p = bundle.profile

    print("=" * 74)
    print("VIEW 1 — THE INCIDENT (per-session capture)")
    print("=" * 74)
    print(f"session={p.session.status}  framework={p.manifest.adapters[0].framework}  "
          f"participants={[x.role for x in p.participants]}")

    print("\n  operations (the pipeline stages, evidence-qualified):")
    for op in p.operations:
        ev = op.evidence
        extra = op.attributes.get("gen_ai.request.model") or op.attributes.get("earshot.tts.voice")
        print(f"    [{op.turn_id}] {op.operation_name:5} "
              f"provider={op.attributes.get('gen_ai.provider.name','-'):8} "
              f"{('model/voice=' + str(extra)) if extra else '':40} "
              f"evidence={ev.source}/{ev.confidence}")

    print("\n  events (turn boundaries, transcript-final, barge-in):")
    for e in p.events:
        ev = e.evidence
        print(f"    [{e.turn_id}] {e.event_name:34} evidence={ev.source}/{ev.confidence}")

    print("\n  quality samples (provider + derived measurements):")
    for s in p.quality_samples:
        for m in s.measurements:
            print(f"    [{s.attributes.get('earshot.turn.id','-')}] {m.name:26} "
                  f"= {m.value:>8} {m.unit:12} {s.evidence.source}/{s.evidence.confidence}")

    print("\n  coverage (what was NOT observed, and why):")
    for c in p.coverage:
        print(f"    {c.signal:26} {c.availability:14} {c.reason or ''}")

    print("\n" + "=" * 74)
    print("VIEW 2 — DERIVED PER-TURN METRICS (what the analyzer computes)")
    print("=" * 74)
    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=1
    )
    for turn in analysis.projections.turns:
        m = turn.metrics
        print(f"\n  turn {turn.turn_id}:")
        for label, metric in (
            ("first_token", m.first_token_latency),
            ("generated_response", m.generated_response_latency),
            ("sent_response", m.sent_response_latency),
            ("received_response", m.received_response_latency),
            ("render_start_response", m.render_start_response_latency),
            ("response", m.response_latency),
        ):
            value = f"{metric.value:.0f}ms" if metric.value is not None else metric.availability
            print(f"    {label:22} {value:14} basis={metric.basis:26} "
                  f"conf={metric.confidence}")
        if m.provider_measurements:
            names = ", ".join(sorted(m.provider_measurements))
            print(f"    provider_measurements: {names}")

    print("\n" + "=" * 74)
    print("VIEW 3 — FLEET SUMMARY (cross-session, from GET /v1/metrics/turns)")
    print("=" * 74)
    with tempfile.TemporaryDirectory() as tmp:
        store = IncidentStore(tmp)
        store.create_project("prod", display_name="Prod")
        store.ingest(bundle, project_id="prod")
        facts = store.list_turn_facts(project_id="prod")
        print(f"  {len(facts)} turn-facts stored, queryable by provider/model/status")
        for metric in ("first_token_ms", "generated_response_ms", "response_ms"):
            groups = store.summarize_turn_metric(metric, project_id="prod", group_by="provider")
            for g in groups:
                p50 = f"{g.p50_ms:.0f}ms" if g.p50_ms is not None else g.availability
                p95 = f"{g.p95_ms:.0f}ms" if g.p95_ms is not None else "-"
                print(f"    {metric:22} provider={g.group:10} "
                      f"p50={p50:10} p95={p95:10} n={g.available_count}/{g.turn_count}")
        interruptions = sum(f.interruption_count or 0 for f in facts)
        print(f"  interruptions across turns: {interruptions}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
