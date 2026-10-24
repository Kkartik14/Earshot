/// <reference types="node" />
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { configureApiBasePath } from "../api/client";

// Runs a real Earshot server for a view's tests: a Python script that builds the
// server's data, prints one JSON line when it serves, and serves until its
// standard input closes. Every request the view makes is real HTTP.

const ROOT = resolve(import.meta.dirname, "../../../..");

/** The repo's venv (`.venv`, where the dev setup and CI install Earshot), or the
 * interpreter `EARSHOT_PYTHON` names. Turborepo's strict env mode does not pass
 * that variable to `turbo run test`, so it is for running vitest directly. */
function python(): string {
  const configured = process.env.EARSHOT_PYTHON;
  if (configured) return configured;
  const venv = join(ROOT, ".venv", "bin", "python");
  if (!existsSync(venv)) {
    throw new Error(
      `No Python at ${venv}: create it and install Earshot ('python -m venv .venv && .venv/bin/pip install -e ".[dev]"'), or set EARSHOT_PYTHON.`,
    );
  }
  return venv;
}

export interface RealServer<Seed> {
  seed: Seed;
  stop(): Promise<void>;
}

/** Start `script`, point the page at the server it prints (`{"port": …}`), and
 * return what it printed. `stop` restores the page and ends the server. */
export async function startRealServer<Seed extends { port: number }>(
  script: string,
): Promise<RealServer<Seed>> {
  const server = spawn(python(), [script], {
    cwd: ROOT,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  server.stderr.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });
  const seed = await new Promise<Seed>((ready, fail) => {
    const lines = createInterface({ input: server.stdout });
    lines.once("line", (line) => ready(JSON.parse(line) as Seed));
    server.once("exit", (code) =>
      fail(new Error(`${script} exited (${code}) before it was ready:\n${stderr}`)),
    );
  });
  const restore = browseFrom(`http://127.0.0.1:${seed.port}/`);
  return {
    seed,
    async stop() {
      restore();
      if (server.exitCode != null) return;
      const exited = new Promise((done) => server.once("exit", done));
      server.stdin.end();
      const timer = setTimeout(() => server.kill("SIGKILL"), 30_000);
      await exited;
      clearTimeout(timer);
    },
  };
}

/** A browser resolves a relative request URL against the page; jsdom runs on
 * Node's fetch, which does not. Point the page at the real server and resolve
 * relative URLs against it, as a browser on that origin would. */
function browseFrom(origin: string): () => void {
  (
    globalThis as unknown as { jsdom: { reconfigure(options: { url: string }): void } }
  ).jsdom.reconfigure({ url: origin });
  const NodeRequest = globalThis.Request;
  const nodeFetch = globalThis.fetch;
  const absolute = (input: RequestInfo | URL) =>
    typeof input === "string" ? new URL(input, document.baseURI).href : input;
  class PageRequest extends NodeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(absolute(input), init);
    }
  }
  globalThis.Request = PageRequest as typeof Request;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    nodeFetch(absolute(input), init)) as typeof fetch;
  // The API client captures fetch and Request when it is made: remake it.
  configureApiBasePath("");
  return () => {
    globalThis.Request = NodeRequest;
    globalThis.fetch = nodeFetch;
    configureApiBasePath("");
  };
}
