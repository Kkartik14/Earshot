# Semantic registry reproduction

`earshot.yaml` is the standalone v1 authoring registry for Earshot-owned telemetry
attributes. It includes a local snapshot of OpenTelemetry `session.id` from
semantic-conventions tag `v1.41.1`, git
`ead83b9b0fa36540c1642fce46e874f002ac23f1`, verified 2026-07-11. Native `lk.*`,
Pipecat, `gen_ai.*`, resource, and instrumentation-scope fields remain owned by their
upstream registries and are preserved separately.

Validate local references and ensure every Earshot attribute emitted by the Python
implementation is registered:

```bash
python scripts/check_semconv.py
```

Quality observations are typed records inside the v1 incident artifact. Earshot does
not standardize or emit an `earshot.*` OpenTelemetry metric instrument in M1; an OTLP
metric mapping requires a later compatibility proposal so units and aggregation do
not acquire a second authority accidentally.
