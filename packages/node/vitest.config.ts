import { defineConfig } from "vitest/config";

// Every test talks to one real `earshot serve` subprocess that test/server.ts
// starts on a temporary data directory and a free port.
export default defineConfig({
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
    globalSetup: ["test/server.ts"],
    testTimeout: 30_000,
  },
});
