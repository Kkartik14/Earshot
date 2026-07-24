import { defineConfig } from "vite";

// During `vite dev` the SPA proxies the earshot backend so capture POSTs run
// same-origin against `POST /v1/capture`, exactly as they would in production
// where the API and the page share an origin. Override the target with
// EARSHOT_API_URL. This affects the dev server only; a production deployment
// serves the built assets from the same origin as the API.
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
