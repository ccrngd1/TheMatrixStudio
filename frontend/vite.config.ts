// SPDX-License-Identifier: Apache-2.0
/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// Build the SPA directly into the Python package's static dir so the FastAPI
// app serves it from the same process (one container, one port).
const outDir = fileURLToPath(new URL('../matrix_studio/static', import.meta.url))

export default defineConfig({
  plugins: [react()],
  build: {
    outDir,
    emptyOutDir: true,
  },
  server: {
    // In dev, proxy API + WS calls to the FastAPI backend on :8000.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
      // The login gate reads `/config.json` to decide whether a login is required, and
      // a config it cannot read fails CLOSED. Vite serves no such file, so without this
      // the dev server would 404 and `vite dev` would show "Configuration error"
      // instead of the app. The backend answers it from AUTH_MODE.
      '/config.json': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
  },
})
