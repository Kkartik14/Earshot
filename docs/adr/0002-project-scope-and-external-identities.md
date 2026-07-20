# ADR 0002: Scope authority by Project and model repeatable External Identities

Status: accepted

## Context

Every Incident, Connector, credential, Delivery Receipt, and Turn Fact belongs to a Project,
with existing local data migrating to a default Project. Provider correlation is repeatable
External Identity data rather than one overloaded provider-session field because a single
voice session can have call, conversation, carrier, room, and trace identifiers with
different privacy characteristics.

## Decision

All public repository methods enter through a Project scope. Existing data migrates to
`default`. API keys resolve to exactly one Project and store only a scrypt hash. Connector
secrets remain environment references. External provider values are correlated with an
instance-keyed HMAC over `(Connector, kind, value)` and are not stored in plaintext.
Finalized provider formats received separately for the same external session are retained
as evidence siblings, not silently merged or treated as revisions; the External Identity
relation is the reconciliation seam.

## Consequences

Cross-project reads fail at repository and HTTP boundaries. A global bundle identifier
cannot be reassigned across Projects, which keeps CAS/tombstone identity unambiguous.
Producers must therefore use collision-resistant identifiers such as UUIDv4/UUIDv7 or the
Connector-generated HMAC identifier; the global namespace and its conflict behavior are a
deliberate v1 compatibility constraint.
Projects are authorization scopes inside one self-hosted organization, not a hostile
multi-tenant SaaS boundary. Operators must not grant mutually untrusted producers write
access to the same installation while bundle identity remains globally namespaced.
Correlation survives restarts with the protected instance key but cannot be reconstructed
from a database copied without that key.
