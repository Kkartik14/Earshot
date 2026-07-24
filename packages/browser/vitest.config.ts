import { defineConfig } from "vitest/config";

// The capture kernel runs against structural, injected W3C mocks (see
// src/testing/fakes.ts), so no jsdom/browser environment is required — the
// tests deliberately exercise the mapping logic in plain Node.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
