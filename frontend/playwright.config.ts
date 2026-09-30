// SPDX-License-Identifier: Apache-2.0
// Browser tests. The unit suite (vitest, jsdom) covers everything except what is drawn on a
// canvas, because jsdom has no canvas at all: `stage.ts` and `Stage.tsx` were the only untested
// modules in the theatre. These tests open the real page in a real browser and read pixels back.
//
// **The browser is the one already on the machine**, not one Playwright downloads. Playwright
// 1.59.1 ships chromium 1217 and this host has 1228, so a download would fetch ~170 MB to get an
// older build than the one already here. `CHROMIUM_PATH` overrides it for anyone whose cache
// differs; a missing binary fails loudly rather than silently skipping.
import { defineConfig, devices } from '@playwright/test'
import { existsSync, readdirSync } from 'node:fs'

function chromium(): string {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH
  // Node 18 has no fs.globSync, so the cache is read directly.
  const cache = `${process.env.HOME ?? ''}/.cache/ms-playwright`
  const builds = existsSync(cache)
    ? readdirSync(cache).filter((d) => d.startsWith('chromium-')).sort()
    : []
  const path = builds
    .map((d) => `${cache}/${d}/chrome-linux64/chrome`)
    .filter(existsSync)
    .pop()
  if (!path || !existsSync(path)) {
    throw new Error(
      'No Chromium found. Set CHROMIUM_PATH, or install one with `npx playwright install chromium`.',
    )
  }
  return path
}

export default defineConfig({
  testDir: './e2e',
  // The theatre's own animation is what several tests wait on, so a generous per-test budget and
  // no parallelism against one preview server.
  timeout: 45_000,
  expect: { timeout: 10_000 },
  workers: 1,
  reporter: process.env.CI ? 'line' : 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:4173',
    launchOptions: { executablePath: chromium(), args: ['--no-sandbox'] },
    // Deterministic pixel coordinates: the room scales by an integer, so the viewport decides it.
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 1,
    trace: 'retain-on-failure',
  },
  webServer: {
    // The built page, not the dev server: this is what a deployment serves.
    command: 'npm run build && npx vite preview --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173/theatre.html',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
