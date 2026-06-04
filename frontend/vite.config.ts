import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev: `npm run dev` proxies API calls to a locally running dashboard
// (`tao-sentinel serve --mock --port 8787`), so the SPA develops against the
// real JSON contract. Prod: `npm run build` emits static assets that the
// FastAPI app serves itself (see tao_sentinel/web/app.py) — same origin, no
// CORS, no extra server.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/healthz": "http://127.0.0.1:8787",
    },
  },
  build: {
    // Build straight into the Python package so the FastAPI app serves the
    // SPA itself and `pip wheel` ships it (package-data includes web/static).
    outDir: "../tao_sentinel/web/static",
    emptyOutDir: true,
    sourcemap: false,
  },
});
