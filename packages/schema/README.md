# @earshot/schema

The superseded **voice-agent trace format (M0)** prototype. It remains as tested Zod
scaffolding, but the Python-backed v1 contract is now authoritative. See the public
[`incident bundle contract`](../../docs/incident-bundle.md).

```ts
import { parseTraceBundle, type TraceBundle } from "@earshot/schema";

const result = parseTraceBundle(await req.json());
if (!result.ok) {
  // never trust network/disk data — reject or mark degraded
  return respond(422, result.error.issues);
}
const bundle: TraceBundle = result.bundle;
```

## Shape

```
TraceBundle
  session   Session          one call/conversation
  turns     Turn[]           one user <-> agent exchange each
  spans     Span[]           stt | llm | tool | tts | playout (waterfall rows)
  events    Event[]          optional fine-grained timeline markers
  audio     AudioRef[]        pointers to recorded audio (never inline)
```

Timing is a call-relative monotonic clock (`*Ms` fields = ms from session start).
The SDK emits raw facts; metrics (TTFT, TTFB, latency, rates) are derived by the
backend and are intentionally **not** in this schema.

## Status

Do not add new v1 semantics here. The generated schema is
[`spec/incident-bundle.schema.json`](../../spec/incident-bundle.schema.json), and the
normative models/validator live under `packages/sdk-python/src/earshot`.
