/**
 * Vitest global setup: one real `earshot serve` for every test file, on a fresh
 * temporary data directory (SQLite catalog, local-disk blobs) and a free loopback
 * port, with a real project API key issued through the `earshot` CLI.
 *
 * The server binary is `EARSHOT_BIN`, else the repository's `.venv/bin/earshot`,
 * else `earshot` on `PATH` (CI installs `.[server]`).
 */
import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import type { TestProject } from "vitest/node";

declare module "vitest" {
  export interface ProvidedContext {
    earshot: { baseUrl: string; apiKey: string };
  }
}

const venvEarshot = fileURLToPath(new URL("../../../.venv/bin/earshot", import.meta.url));
const earshot =
  process.env.EARSHOT_BIN ?? (existsSync(venvEarshot) ? venvEarshot : "earshot");

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      probe.close(() =>
        typeof address === "object" && address !== null
          ? resolve(address.port)
          : reject(new Error("no port")),
      );
    });
  });
}

export default async function setup(project: TestProject): Promise<() => Promise<void>> {
  const dataDir = await mkdtemp(join(tmpdir(), "earshot-node-"));
  const removeData = () => rm(dataDir, { recursive: true, force: true });
  try {
    const stop = await serve(project, dataDir);
    return async () => {
      await stop();
      await removeData();
    };
  } catch (error) {
    await removeData();
    throw error;
  }
}

async function serve(
  project: TestProject,
  dataDir: string,
): Promise<() => Promise<void>> {
  const issued = await promisify(execFile)(earshot, [
    ...["api-key", "issue", "--project", "default", "--label", "node-sdk-tests"],
    ...["--data-dir", dataDir],
  ]);
  const apiKey = (JSON.parse(issued.stdout) as { credential: string }).credential;
  const port = await freePort();
  const server = spawn(
    earshot,
    ["serve", "--data-dir", dataDir, "--port", String(port)],
    {
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  let log = "";
  server.stderr.on(
    "data",
    (chunk: Buffer) => (log = (log + chunk.toString()).slice(-4000)),
  );
  const exited = new Promise<void>((resolve) => server.once("exit", () => resolve()));
  const stop = async () => {
    if (server.exitCode === null && server.signalCode === null) server.kill("SIGTERM");
    await exited;
  };
  const baseUrl = `http://127.0.0.1:${port}`;

  for (const started = Date.now(); ;) {
    if (server.exitCode !== null) throw new Error(`earshot serve exited:\n${log}`);
    const healthy = await fetch(`${baseUrl}/healthz`).then(
      (reply) => reply.ok,
      () => false,
    );
    if (healthy) break;
    if (Date.now() - started > 30_000) {
      await stop();
      throw new Error(`earshot serve did not become healthy:\n${log}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  project.provide("earshot", { baseUrl, apiKey });
  return stop;
}
