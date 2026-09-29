// SPDX-License-Identifier: Apache-2.0
/** @type {import('tailwindcss').Config} */
const v = (name) => `rgb(var(--rgb-${name}) / <alpha-value>)`

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // The theme's colours (src/index.css, docs/MOBILE-UI.md §5.1). `matrix-*` keeps its old names so
        // every existing class re-themes; the rest are the design's one-meaning-each roles.
        matrix: {
          bg: v('bg'),
          panel: v('panel'),
          border: v('border'),
          accent: v('accent'),
          live: v('live'),
        },
        cc: {
          accent2: v('accent2'),
          inject: v('inject'),
          shift: v('shift'),
          danger: v('danger'),
          t1: v('t1'),
          t2: v('t2'),
          t3: v('t3'),
        },
      },
    },
  },
  plugins: [],
}
