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

**Session Key**:
The string a producer's own instrumentation attaches to OTLP spans (`earshot.session.id`,
then `gen_ai.conversation.id`, `session.id`, `conversation.id`) or to a Recording so that the
server groups that evidence into one Call within a Project. Unlike an External Identity, which
a Connector derives from a provider's identifiers to correlate already-finalized Incidents and
stores only as an HMAC, a Session Key decides which Call live evidence joins; its raw value is
stored with the Call only under the `full` Capture Preset.
_Avoid_: Session ID, correlation ID

**Call**:
One voice session that the server assembles from the OTLP spans and Recordings sharing a
Session Key in a Project, or from one unkeyed trace that carries a framework session span.
A Call is open while evidence arrives and closes after an end marker or a quiet period; later
evidence produces another Revision. A browser **CaptureCall** is a separate, existing
lifecycle: it accumulates browser capture drains into a live session that only an explicit
seal materializes, and it never becomes a Call.
_Avoid_: Conversation, trace, capture call

**Revision**:
One materialization of a Call into an Incident, numbered from 1, with bundle ID
`<call_id>.r<n>` and session ID equal to the Call ID. A Revision is an Incident: it is
immutable once stored, is built from all of the Call's spans and stored Recordings as of its
claim, and records why the Call closed and whether the close was final. A later Revision
supersedes an earlier one as the Call's latest but never edits it. Separately finalized
Connector deliveries for the same external session are sibling Incidents, not Revisions.
_Avoid_: Version, update, re-close

**Recording**:
An audio file attached to a Call through the Recordings API: a manifest (speaker, content
type, start time) created against a Session Key, then content streamed to the blob store. A
Recording is pending until its content is stored, and failed if the upload does not finish
in time; only stored Recordings enter a Revision, as media references.
_Avoid_: Media, audio blob, attachment

**Capture Preset**:
A Project setting that decides what call evidence the server persists: `full` keeps content
within size limits, while `metadata_only` drops transcript, model and tool payloads, audio
and identity, removes Session Key attributes, and refuses Recordings. It is enforced by the
server on receipt, unlike the privacy policy a producer declares inside each Incident.
_Avoid_: Capture policy, privacy mode, redaction level

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
