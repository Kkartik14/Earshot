import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

// The repo root, two levels up from examples/browser-voice. The self-check
// imports @earshot/browser's own test fakes from source (they are excluded from
// the built package, exactly like they are inside the SDK's own tests), so Vite
// must be allowed to read that file across the package boundary.
const repoRoot = fileURLToPath(new URL("../..", import.meta.url));

export default defineConfig({
  test: {
    // The glue and the self-check drive the SDK against injected fakes, so no
    // browser/jsdom environment is needed — the same discipline the SDK uses.
    environment: "node",
    include: ["src/**/*.test.ts", "selfcheck/**/*.test.ts"],
  },
  server: {
    fs: {
      allow: [repoRoot],
    },
  },
});
