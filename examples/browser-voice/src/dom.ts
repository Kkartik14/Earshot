/**
 * Tiny DOM helpers. No framework — this example keeps its dependency surface to
 * exactly `@earshot/browser` plus the Vite/TS toolchain, so what it exercises is
 * unambiguous.
 */

/** Create an element with attributes and children in one call. */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Partial<Record<string, string>> = {},
  children: Array<Node | string> = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children) {
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/** An append-only log panel that timestamps every line against a wall clock. */
export class LogPanel {
  constructor(private readonly target: HTMLElement) {}

  line(message: string): void {
    const stamp = new Date().toISOString().slice(11, 23);
    this.target.append(document.createTextNode(`${stamp}  ${message}\n`));
    this.target.scrollTop = this.target.scrollHeight;
  }

  /** Log a value as JSON, pretty-printed, so a drained payload is inspectable. */
  json(label: string, value: unknown): void {
    this.line(`${label} ${JSON.stringify(value, null, 2)}`);
  }
}
