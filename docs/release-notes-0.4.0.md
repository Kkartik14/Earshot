# Earshot 0.4.0 release notes

Earshot 0.4.0 adds hosted JWT access for Calls, scoped project provisioning, and a
standard span profile for hosts that instrument their own voice pipeline. The backend API
is version 0.16.0.

## Compatibility versions

| Surface                                  | Version |
| ---------------------------------------- | ------- |
| Python distribution and Node npm package | 0.4.0   |
| Backend API                              | 0.16.0  |
| Contract and semantic profile            | 0.2.0   |
| Host span profile                        | 0.1.0   |
| Call projection                          | 7       |

These version numbers describe separate compatibility surfaces. The server rebuilds
stored Call projections after the projection version changes.

## Hosted API access

Hosted JWT authorization now uses a closed operation table. An operation that is not in
the table returns `403 EARSHOT_OPERATION_UNAVAILABLE`, regardless of the token's scopes.
Hosted Calls use their own `earshot:calls:write`, `earshot:calls:read`, and
`earshot:calls:delete` scopes. Reading Call revisions also requires `earshot:calls:read`;
routes that name an unreadable incident return the same not-found response used for a
missing incident.

`PUT /v1/projects/{project_id}` provisions a project under
`earshot:project:provision`. Repeating the same project terms confirms the project,
changed terms return `409 EARSHOT_PROJECT_CONFLICT`, and a deleted project cannot be
recreated. See the [backend API guide](backend-api.md) for the current route contract.

Hosted mode continues to keep raw span attributes, audio copies, transcript uploads,
prices, and receiver counters closed. Review the operation and scope table before
upgrading a hosted integration.

## Host span profile

Hosts can emit their own pipeline calls using the `earshot.host` scope and the 0.1.0 host
span profile. It describes `turn` spans, user and agent speech spans, and `stt`, `llm`,
and `tts` stage spans. Earshot derives turn timing, audible gaps, stage spans, and usage
from this evidence without creating a transcript. See [the host span profile](calls.md#the-host-span-profile).

## Other changes

- `GET /version` reports package, API, contract, semantic-profile, host-profile, and Call
  projection versions, plus hosted-JWT and `signals` capability flags.
- `earshot.call.ended` is recorded as safe revision metadata under both `full` and
  `metadata_only` capture policies.
- `/readyz` reports bounded catalog, blob-store and materializer states. A healthy catalog
  remains available with HTTP 200 and `status: "degraded"` during a partial blob-store or
  materializer outage; catalog failure returns 503.
- Server maintenance and durable-work measurements use the OpenTelemetry API. A host
  installs and configures its own SDK/exporter to export them; see the [production guide](production.md#export-earshot-server-metrics).
- The Postgres and S3 integration lane verifies a completed Recording survives a forced
  server-process kill and restart with the same catalog and object-store configuration.

The [changelog](../CHANGELOG.md) lists the complete set of changes.
