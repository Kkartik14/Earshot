import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// During `vite dev` the SPA proxies the backend API so it runs same-origin,
// exactly as it will in production where FastAPI serves the built assets.
const apiTarget = process.env.EARSHOT_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": apiTarget,
      "/healthz": apiTarget,
      "/readyz": apiTarget,
    },
  },
});
