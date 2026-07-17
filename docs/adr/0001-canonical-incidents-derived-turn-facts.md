# ADR 0001: Keep canonical Incidents authoritative and derive Turn Facts

Status: accepted

## Context

Earshot stores the validated, immutable Incident as the evidence authority and projects
rebuildable Turn Facts for fleet queries. The default remains SQLite plus the existing
content-addressed store; a second database will be introduced only after Earshot-shaped
benchmarks demonstrate a measured need, so the implementation does not hide speculative
storage differences behind a premature driver switch.

## Decision

Write one wide `turn_metrics` row per projected turn. Each common latency keeps its own
value, availability, basis, confidence, and limitation. The row also carries Project,
Incident/session/turn identity, framework/provider/model dimensions, contract version, and
projection version. Rebuild the table from canonical Incidents on startup.

STT finalization means speech-end to an explicit final-transcript event, with a governed
turn-owned provider transcription-delay fallback. EOU means speech-end to an explicit
turn-commit event, with a governed provider endpointing-delay fallback. Turn duration is
published only from an explicit native turn-lifecycle interval; Earshot never substitutes
a min/max timestamp envelope. Accepted-interruption count is nullable and carries its own
evidence quality; detected-only events do not masquerade as accepted barge-ins. Turn Facts
have a dedicated projection version because these meanings can evolve
independently of the deterministic incident analyzer.

## Consequences

Fleet p50/p95 queries are cheap without making SQL the evidence authority. Aggregates are
split by availability, basis, confidence, and limitation. Projection bugs
are repairable by version bump/rebuild. SQLite remains the measured default; a future scale
store must preserve the same evidence and rebuild semantics.
