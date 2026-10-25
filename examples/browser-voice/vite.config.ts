import { defineConfig } from "vite";

// Proxy capture requests to the backend in development; production serves the app and API from one origin.
const apiTarget = process.env.EARSHOT_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  server: {
    proxy: {
      "/v1": apiTarget,
      "/healthz": apiTarget,
      "/readyz": apiTarget,
    },
  },
});
