# ADR 0003: Build finalized provider Connectors before generic live OTLP

Status: accepted

## Context

Hosted-provider ingestion first accepts bounded Provider Deliveries and turns them into
canonical Incidents behind one security and normalization seam. Generic live OTLP remains a
later ingress mode because the protocol does not define Earshot session completion, late-span
revision, or multi-trace correlation; those policies must be explicit before `/v1/traces`
can promise durable Incident finalization.

## Decision

Ship a provider-neutral finalized-delivery kernel first. It owns raw-body authentication,
strict parsing, rate/body/skew limits, durable Receipts, External Identities, privacy-minimal
normalization, and canonical publication. Implement ElevenLabs, Vapi, and Retell through
that seam. Treat ElevenLabs' post-call OTLP-shaped JSON as a finalized provider format, not
as a generic OTLP collector.

## Consequences

Earshot gains hosted and in-app voice-agent coverage without inventing session completion.
A future `/v1/traces` prototype must first specify project routing, deduplication,
completion/idle timeout, late spans, crash recovery, bounded staging, and privacy.
