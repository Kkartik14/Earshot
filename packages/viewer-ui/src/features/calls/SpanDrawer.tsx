import type { KeyboardEvent } from "react";
import { ApiError } from "../../api/client";
import drawer from "../inspector/drawer.module.css";
import { roleColorVar, roleLabel } from "../inspector/timeline";
import { useInitialFocus } from "../inspector/useInitialFocus";
import { useCallSpan, type CallSpan, type SpanAttribute, type TurnSpan } from "./api";
import { humanize } from "./format";
import styles from "./CallView.module.css";

// What a span's stored attributes mean, read by their semantic key (one table for
// every framework, so the drawer never branches on Pipecat or LiveKit).
const INPUT_KEYS = ["gen_ai.input.messages", "gen_ai.input"];
const OUTPUT_KEYS = ["gen_ai.output.messages", "gen_ai.output"];
const SYSTEM_KEYS = ["gen_ai.system_instructions"];
const CONTENT_CLASSES = new Set([
  "transcript",
  "audio",
  "tool_payload",
  "model_payload",
  "extension_payload",
  "diagnostic_payload",
]);

/** A stored value as text: JSON-encoded strings (model messages often are) and
 * structured values pretty-printed, anything else as it is. */
function valueText(value: unknown): string {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return value;
      }
    }
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function firstOf(attributes: SpanAttribute[], keys: string[]): SpanAttribute | undefined {
  for (const key of keys) {
    const found = attributes.find((attribute) => attribute.semantic_key === key);
    if (found) return found;
  }
  return undefined;
}

function Payload({ title, attribute }: { title: string; attribute: SpanAttribute }) {
  return (
    <section className={drawer.sec}>
      <h3 className={drawer.secLabel}>{title}</h3>
      <pre className={styles.payload}>{valueText(attribute.value)}</pre>
    </section>
  );
}

function durationMs(span: CallSpan): number {
  return Number((BigInt(span.end_unix_nano) - BigInt(span.start_unix_nano)) / 1_000_000n);
}

/** One stored span: LLM input and output, tool arguments and results, and the
 * error status with its exception events, then its metadata. Only what the
 * project's capture preset kept when the span arrived is here. */
export function SpanDetail({ span }: { span: CallSpan }) {
  const attributes = span.attributes;
  const input = firstOf(attributes, INPUT_KEYS);
  const output = firstOf(attributes, OUTPUT_KEYS);
  const system = firstOf(attributes, SYSTEM_KEYS);
  const shown = new Set([input, output, system]);
  const tools = attributes.filter(
    (attribute) => attribute.capture_class === "tool_payload" && !shown.has(attribute),
  );
  const exceptions = span.events.filter((event) => event.name === "exception");
  const metadata = attributes.filter(
    (attribute) => attribute.capture_class === "metadata",
  );
  const hasContent = attributes.some((attribute) =>
    CONTENT_CLASSES.has(attribute.capture_class),
  );

  return (
    <>
      <section className={drawer.sec}>
        <h3 className={drawer.secLabel}>Span</h3>
        <div className={drawer.kv}>
          <span className={drawer.kk}>status</span>
          <span className={drawer.vv}>
            {span.status.code}
            {span.status.message ? ` · ${span.status.message}` : ""}
          </span>
        </div>
        <div className={drawer.kv}>
          <span className={drawer.kk}>duration</span>
          <span className={drawer.vv}>{durationMs(span)} ms</span>
        </div>
        <div className={drawer.kv}>
          <span className={drawer.kk}>instrumentation</span>
          <span className={drawer.vv}>{span.scope_name || "—"}</span>
        </div>
        <div className={drawer.kv}>
          <span className={drawer.kk}>span</span>
          <span className={drawer.vv}>
            {span.trace_id}/{span.span_id}
          </span>
        </div>
      </section>

      {!hasContent ? (
        <p className={drawer.note}>
          No content is stored for this span: the project&apos;s capture preset kept
          metadata only, or the span carried none.
        </p>
      ) : null}
      {system ? <Payload title="System instructions" attribute={system} /> : null}
      {input ? <Payload title="LLM input" attribute={input} /> : null}
      {output ? <Payload title="LLM output" attribute={output} /> : null}
      {tools.map((attribute) => (
        <Payload
          key={attribute.key}
          title={`Tool · ${attribute.semantic_key}`}
          attribute={attribute}
        />
      ))}

      {span.status.code === "error" || exceptions.length > 0 ? (
        <section className={drawer.sec}>
          <h3 className={drawer.secLabel}>Error</h3>
          {span.status.code === "error" ? (
            <p className={styles.errorText}>
              {span.status.message ?? "The span ended with an error status (no message)."}
            </p>
          ) : null}
          {exceptions.map((event, index) => (
            <dl key={index} className={styles.exception}>
              {event.attributes.map((attribute) => (
                <div key={attribute.key}>
                  <dt>{attribute.key}</dt>
                  <dd>
                    <pre className={styles.payload}>{valueText(attribute.value)}</pre>
                  </dd>
                </div>
              ))}
            </dl>
          ))}
        </section>
      ) : null}

      {metadata.length > 0 ? (
        <section className={drawer.sec}>
          <h3 className={drawer.secLabel}>Metadata</h3>
          {metadata.map((attribute) => (
            <div key={attribute.key} className={drawer.kv}>
              <span className={drawer.kk}>{attribute.semantic_key}</span>
              <span className={drawer.vv}>{valueText(attribute.value)}</span>
            </div>
          ))}
        </section>
      ) : null}
    </>
  );
}

/** The details drawer for one span of a turn, loaded from the span route when it
 * opens. It reuses the inspector drawers' chrome; their bodies are built from an
 * incident's operations, which a stored span is not. */
export function SpanDrawer({
  callId,
  span,
  onClose,
}: {
  callId: string;
  span: TurnSpan;
  onClose: () => void;
}) {
  const detail = useCallSpan(callId, span);
  const close = useInitialFocus<HTMLButtonElement>(`${span.trace_id}:${span.span_id}`);
  const color = roleColorVar(span.role);
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape") onClose();
  };

  return (
    <aside
      role="dialog"
      aria-label={`${span.name} span detail`}
      className={drawer.drawer}
      onKeyDown={onKeyDown}
    >
      <div className={drawer.head}>
        <button
          ref={close}
          type="button"
          className={drawer.close}
          onClick={onClose}
          aria-label="Close detail"
        >
          ×
        </button>
        <div className={drawer.kind}>
          <span className={drawer.dot} style={{ background: color }} />
          <span className={drawer.title} style={{ color }}>
            {span.name}
          </span>
          <span className={drawer.kindTag}>{roleLabel(span.role)}</span>
          {span.status === "error" ? (
            <span className={drawer.statusBadge}>{humanize(span.status)}</span>
          ) : null}
        </div>
      </div>
      <div className={drawer.body}>
        {detail.isPending ? (
          <p className={drawer.note} role="status">
            Loading the span…
          </p>
        ) : null}
        {detail.isError ? (
          <p className={drawer.note} role="alert">
            {detail.error instanceof ApiError && detail.error.status === 404
              ? "This span is not stored for this call."
              : `The span could not be loaded (${detail.error.message}).`}
          </p>
        ) : null}
        {detail.data ? <SpanDetail span={detail.data} /> : null}
      </div>
    </aside>
  );
}
