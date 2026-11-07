# Earshot 0.5.0 release notes

Earshot 0.5.0 adds live turns for open Calls, project-scoped hosted Recording reads,
host-profile 0.2.0 transcripts and usage, and an offline host-profile checker. It also
deprecates the Platform Observe summary route. The backend API is version 0.17.0.

## Compatibility versions

| Surface                                  | Version |
| ---------------------------------------- | ------- |
| Python distribution and Node npm package | 0.5.0   |
| Backend API                              | 0.17.0  |
| Contract and semantic profile            | 0.2.0   |
| Host span profile                        | 0.2.0   |
| Call projection                          | 10      |

The server rebuilds stored Call projections after the projection version changes.

## Breaking changes

- Under `metadata_only`, Earshot now drops `lk.cloud_agent_id`, `lk.job.agent_id` and
  `gen_ai.agent.id`. Spans stored before 0.5.0 still contain those attributes.
- Pipecat 1.5 support is removed. The adapter supports `pipecat-ai>=1.6.0,<1.7`.

## Live turns

`GET /v1/calls/{call_id}/live-turns` returns turns supported by evidence stored so far,
without persisting a provisional projection. For a closed Call it reads the latest durable
Revision. The route is read-only and carries an `ETag`. The viewer polls while the Call is
open or its durable projection is not current, then stops. Hosted access requires
`earshot:calls:read`. See [the Calls guide](calls.md).

## Host profile 0.2.0

The host profile adds optional transcript text on speech spans and provider-metered STT
seconds and TTS characters. Agent transcript text is what was spoken; user text is final,
not interim, and one value is limited to 64 KiB. Positive finite STT seconds may be sent
as an OTLP `doubleValue` or `intValue` and are rounded to milliseconds per request;
streaming STT session totals reported at close are not request usage. Positive finite
reported latency accepts either numeric representation; projections store whole
milliseconds. Older latency summaries read as `null` until the server rebuilds them.

The offline `earshot check-host-profile` command checks one OTLP/JSON request's structure
and host-profile fields. It does not prove the facts match live audio, that transcripts are
authorized, or that the host meets a latency target. See [the host span profile](calls.md#the-host-span-profile)
and the [checker reference](calls.md#offline-conformance-checker).

## Hosted Recording reads

A hosted token with `earshot:calls:read` can stream Recording audio, waveform peaks and a
generated stereo rendition only from its signed Project. Tokens minted for 0.4.0 gain this
access. Earshot returns no blob-store URL. Project or Call deletion removes the catalog
rows, analyses, peaks, renditions and Recording blobs; later reads cannot retrieve them.

New Recordings are not stored under `metadata_only`. Switching an existing Project to
`metadata_only` does not retroactively remove Recordings already stored. See
[hosted mode](calls.md#hosted-mode) and [privacy](privacy.md#capture-presets).

## Outcome evidence

In 0.5.0, `earshot.interruption.accepted` and `earshot.turn.cancelled` are reserved event
names, not host-profile 0.2.0 fields that Earshot reads or validates. Cancellation reasons
are not carried. An explicit `earshot.call.outcome` value takes precedence. When none is
present, any span with status `ERROR`, including a stage span, makes the Call outcome
`error`; otherwise, an ended cancelled Call appears `completed`. Hosted tokens cannot read
raw span detail. See [outcome mapping](calls.md#mapping-interruption-turn-and-stage-outcomes).

## Platform Observe summary deprecation

The Platform-specific
`GET /v1/platform/projects/{id}/observe/summary` route is deprecated as of this release.
Responses carry `Deprecation: @1794528000` (2026-11-13) and `Link` relations for the
successor route and the version-pinned migration guide. It remains available through
0.5.x; removal is planned for 0.6.0 ([#77](https://github.com/Kkartik14/Earshot/issues/77)).
No `Sunset` header is sent until the 0.6.0 unavailability date is set. The Platform route
accepts a `record_id` filter that the generic summary route does not. See
[generic and Platform summaries](platform-integration.md#generic-and-platform-summaries).

## Pricing and integrations

- Cartesia `sonic-3.6` has a built-in price of $38 per 1 million credits, with one credit
  per character.
- Earshot supports LiveKit Agents `>=1.6.5,<1.9`.
- Vapi and Retell adapters remain available for parsing and synthetic conformance checks,
  but their signed production delivery paths have not been verified.

The [changelog](../CHANGELOG.md) lists the complete set of changes.
