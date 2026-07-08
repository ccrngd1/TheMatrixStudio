// SPDX-License-Identifier: Apache-2.0
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // "Control room" palette — dark, high-contrast.
        matrix: {
          bg: '#0b0f14',
          panel: '#121821',
          border: '#1e2733',
          accent: '#38bdf8',
          live: '#22c55e',
        },
      },
    },
  },
  plugins: [],
}
