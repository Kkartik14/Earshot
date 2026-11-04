# Integrate hosted Calls

This guide describes the host-neutral integration with Earshot's hosted Calls API.
The host owns users, authorization, the user interface, and the mapping from its call
IDs to Earshot Calls. Earshot receives and retains telemetry, builds call views, and
serves project-scoped API responses. The services communicate over HTTP; they do not
share a database.

The host renders its own call experience from Earshot's API and keeps the Earshot
service token on the server. Never send that token to browser code. For the exact request
and response schemas, see the [Backend API](./backend-api.md) and
[Calls guide](./calls.md).

## Check server compatibility

Before provisioning or sending telemetry, request `GET /version`. It is unauthenticated
and contains no project data. Compare the returned API and profile versions and
capabilities with the host build; do not infer them from a package version or a fixed
release number. The response includes `api_version`, `host_profile_version`,
`call_projection_version`, and whether hosted JWT and recording analysis are enabled.
See [`GET /version`](./backend-api.md#get-version) for the response shape.

## Authenticate server-to-server

Run Earshot in `hosted_jwt` mode and configure `EARSHOT_JWT_ISSUER`,
`EARSHOT_JWT_AUDIENCE`, and an HTTPS `EARSHOT_JWKS_URL`. Earshot verifies short-lived
RS256 or ES256 JWTs against the configured JWKS. The host's backend derives the user and
project from its own authenticated session and mints a token for that project. Keep the
token on the server and proxy only the required Earshot operations through the host's
authenticated API boundary.

Every project request must match the signed `project_id` claim. A project-ID header is
only an assertion; it never grants access. Tokens can live for at most five minutes.
Hosted mode does not accept operator API keys, `EARSHOT_TOKEN`, or the browser-cookie
exchange. See [hosted authorization](./backend-api.md#hosted-detail-and-live-apis) and
[production configuration](./configuration.md).

The current Earshot scope vocabulary is documented in the
[Backend API](./backend-api.md#hosted-scopes). Confirm that the token issuer
grants the matching scopes before accepting a hosted deployment. Generic incident
scopes do not grant access to Calls.

## Provision and delete a project

Create the Earshot project with `PUT /v1/projects/{project_id}` using a token with
`earshot:project:provision`. Use the host's stable project identifier directly and
provide both terms explicitly:

```http
PUT /v1/projects/project-123
Authorization: Bearer <short-lived-service-token>
Content-Type: application/json

{"display_name":"Support voice","capture_preset":"metadata_only"}
```

Choose `full` only when the host is authorized to send and retain call content. Choose
`metadata_only` to keep metadata while Earshot drops recognized content and identity
attributes and refuses recordings. The preset is fixed at project creation through this
route; a repeated request with different terms conflicts. It is not a complete PII
scrubber: producers remain responsible for sensitive values under neutral attribute
names. Read [Privacy and capture policy](./privacy.md#capture-presets) before choosing.

For deletion, the host's coordinator calls `DELETE /v1/projects/{project_id}` with
`earshot:project:delete` and repeats while Earshot reports `deleting`. Earshot fences
new writes before cleanup and returns `deleted` only when its owned cleanup is complete.
Call deletion is separately available as `DELETE /v1/calls/{call_id}` under
`earshot:calls:delete`. See [project deletion](./backend-api.md#project-deletion) and
the [hosted Call scopes](./calls.md#hosted-mode).

## Send call evidence

Send OTLP/HTTP spans to `POST /v1/traces` with `earshot:calls:write`. The Session Key
groups traces and recordings into a Call. Set a stable, opaque call key on an early span
as `earshot.session.id`; it must be the same key used when creating an optional
Recording. Earshot also recognizes framework keys such as `gen_ai.conversation.id`, but
the first key binding for a trace wins. Read the
[Session Key rules](./otlp-quickstart.md#session-keys)
before choosing identifiers or retry behavior.

For a custom voice pipeline, emit spans under the `earshot.host` instrumentation scope
using the [host span profile](./calls.md#the-host-span-profile) version reported by
`GET /version`. It describes turn, speech, and stage spans; stable span IDs make retries
idempotent. Do not export text or identity fields the project is not permitted to
retain. Pipecat and LiveKit producers should follow the
[OTLP quick start](./otlp-quickstart.md) and the relevant
[Pipecat](./adapters/pipecat.md) and [LiveKit](./adapters/livekit.md) adapter guides
instead of translating their spans into the host profile.

If a recording is permitted by the project's `full` preset, create it with
`POST /v1/recordings` and upload its bytes to the returned `upload_path` with
`PUT /v1/recordings/{recording_id}/content`; both require `earshot:calls:write`. A
Recording gives Earshot audio evidence for analysis and call metrics. In hosted JWT mode,
the host cannot fetch Earshot's stored audio, raw spans, or transcript upload endpoints;
the API exposes only the permitted Call JSON surfaces. Install the `signals` extra (or
use the container image that includes it) if recordings should be analysed. The
[recording workflow](./otlp-quickstart.md#recordings) and
[hosted Call surfaces](./calls.md#hosted-mode) describe the details.

## Read Calls in the host UI

Use a server-side token with `earshot:calls:read` for:

| Operation                                       | Route                     |
| ----------------------------------------------- | ------------------------- |
| List calls and filter summaries                 | `GET /v1/calls`           |
| Read one Call and its current view              | `GET /v1/calls/{call_id}` |
| Resolve a host Session Key to Earshot's Call ID | `POST /v1/calls:resolve`  |
| Read the dashboard summary                      | `GET /v1/dashboard`       |

The host can use the returned call data to render its own UI. A Call's projection is
created from a Revision; while a Call is open, `projection` can be `null` and the detail
response may contain only the span transcript. Follow the
[Call projection lifecycle](./calls.md#how-a-call-gets-its-view) and do not treat a
transcript refresh as a completed turn projection. Audio, raw-span detail, prices,
transcript uploads, and OTLP statistics remain closed to hosted JWT clients. Consult
the exact [hosted mode surface](./calls.md#hosted-mode) before exposing any additional
route through the host.

## Keep ownership boundaries clear

- The host owns account and membership checks, project-to-user authorization, UI, and
  host call identifiers.
- Earshot owns the evidence it receives, its retention and deletion behavior, analysis,
  and the Call API. Do not read or write Earshot's database directly.
- Runtime producers send telemetry to the host or to the configured Earshot boundary
  according to the deployment's trust model. They do not receive the host's service JWT.
- Use the [Calls guide](./calls.md), [Privacy guide](./privacy.md), and
  [production notes](./production.md) as the authoritative references for Call
  semantics, retained data, storage, and operations.
