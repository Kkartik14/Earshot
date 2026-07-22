# Earshot Voice Observability

Earshot is the open-source Voice Observability SDK. It turns heterogeneous voice-agent
evidence into portable, governed incidents that can be compared across runtimes and providers
without overstating what was observed.

## Language

**Voice Observability SDK**:
An SDK that instruments existing voice runtimes and providers, preserves evidence, and derives
portable operational insight without owning the voice runtime itself.
_Avoid_: Voice SDK, voice runtime, voice-agent framework

**Incident**:
A finalized, immutable evidence artifact for one voice session, including its causal graph,
coverage, provenance, and privacy policy.
_Avoid_: Trace, call record, log bundle

**Project**:
The isolation and authorization scope that owns incidents, connectors, credentials, and
derived facts.
_Avoid_: Tenant, workspace, account

**Connector**:
A configured relationship between one Project and one external provider surface that can
deliver or retrieve voice-session evidence.
_Avoid_: Integration, plugin, webhook

**Provider Delivery**:
One authenticated payload received from, or fetched through, a Connector.
_Avoid_: Webhook request, event

**Delivery Receipt**:
The durable replay identity and content digest of a Provider Delivery.
_Avoid_: Idempotency record, webhook log

**External Identity**:
A provider-scoped identifier that correlates an Incident with one or more external session,
call, room, carrier, or trace identities.
_Avoid_: Provider session key, foreign ID

**Turn Fact**:
A rebuildable, project-scoped summary of one conversational turn for fleet comparison,
whose measurements retain individual evidence quality and coverage.
_Avoid_: Metric row, aggregate

**Evidence**:
A claim about a voice session together with its source, observer, method, confidence, and
availability.
_Avoid_: Data point, telemetry

**Coverage**:
An explicit statement that a signal was available, not observed, not exposed, or otherwise
unavailable, including the reason when known.
_Avoid_: Missing data, null
