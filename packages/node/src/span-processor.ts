import { context, createContextKey, type Context } from "@opentelemetry/api";

/** The span attribute that names a span's call: Earshot's highest-priority session key. */
export const SESSION_KEY_ATTRIBUTE = "earshot.session.id";

const SESSION_KEY = createContextKey(SESSION_KEY_ATTRIBUTE);
const MAX_KEY_BYTES = 256;

/** The key, if the server reads it as one: 1 to 256 bytes of UTF-8 after trimming. */
function checkedSessionKey(sessionKey: string): string {
  const trimmed = sessionKey.trim();
  const bytes = trimmed.isWellFormed() ? Buffer.byteLength(trimmed, "utf8") : 0;
  if (trimmed === "NOT_GIVEN" || bytes < 1 || bytes > MAX_KEY_BYTES) {
    throw new RangeError("a session key is 1 to 256 bytes of UTF-8 after trimming");
  }
  return sessionKey;
}

/** `parent` with `sessionKey` as the session key of the spans started in it. */
export function setSessionKey(parent: Context, sessionKey: string): Context {
  return parent.setValue(SESSION_KEY, checkedSessionKey(sessionKey));
}

/** Run work with an in-process session key that the processor adds to its spans, never to outgoing baggage. */
export function withSessionKey<T>(sessionKey: string, fn: () => T): T {
  return context.with(setSessionKey(context.active(), sessionKey), fn);
}

/** The part of an SDK span the processor touches (SDK 1.x and 2.x spans both fit). */
interface StampableSpan {
  readonly attributes?: Readonly<Record<string, unknown>>;
  setAttribute(key: string, value: string): unknown;
}

/** Adds `earshot.session.id` from span context or a fixed key, while preserving a span's existing value. */
export class EarshotSpanProcessor {
  readonly #sessionKey: string | undefined;

  constructor({ sessionKey }: { sessionKey?: string } = {}) {
    this.#sessionKey =
      sessionKey === undefined ? undefined : checkedSessionKey(sessionKey);
  }

  onStart(span: StampableSpan, parentContext: Context): void {
    const key =
      (parentContext.getValue(SESSION_KEY) as string | undefined) ?? this.#sessionKey;
    if (key !== undefined && span.attributes?.[SESSION_KEY_ATTRIBUTE] === undefined) {
      span.setAttribute(SESSION_KEY_ATTRIBUTE, key);
    }
  }

  onEnd(): void {}

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}
