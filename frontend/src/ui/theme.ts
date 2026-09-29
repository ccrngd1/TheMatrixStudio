// SPDX-License-Identifier: Apache-2.0
// Theme and effects (docs/MOBILE-UI.md §4.12, §5.4). A theme swaps the palette and nothing else, and is set as
// `data-theme` on <html> so the Tailwind bridge in index.css and the command-centre stylesheet read the same
// variables. FX is the atmosphere switch; `prefers-reduced-motion` turns it off by default.
import { useEffect, useState } from 'react'

export const THEMES = ['holo', 'matrix', 'neon'] as const
export type Theme = (typeof THEMES)[number]

const THEME_KEY = 'cc.theme'
const FX_KEY = 'cc.fx'

function readTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY)
    if (t && (THEMES as readonly string[]).includes(t)) return t as Theme
  } catch {
    // Storage unavailable (private mode, tests): the default is fine.
  }
  return 'holo'
}

function reducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

function readFx(): boolean {
  try {
    const f = localStorage.getItem(FX_KEY)
    if (f === 'on') return true
    if (f === 'off') return false
  } catch {
    // fall through
  }
  return !reducedMotion()
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
}

/** Theme and FX, persisted, applied to <html> on change. */
export function useThemeState() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  const [fx, setFx] = useState<boolean>(readFx)
  useEffect(() => {
    applyTheme(theme)
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      // ignore
    }
  }, [theme])
  useEffect(() => {
    try {
      localStorage.setItem(FX_KEY, fx ? 'on' : 'off')
    } catch {
      // ignore
    }
  }, [fx])
  return { theme, setTheme, fx, setFx }
}

/**
 * The animation phase for this instant, as a negative delay within the 12 s cycle every loop divides (§5.4).
 *
 * Set as `--ph` on an element that re-renders, so each loop RESUMES in phase instead of restarting: without it
 * the feed's re-render on every turn made every pulse visibly stutter.
 */
export function phase(): string {
  return `-${Date.now() % 12000}ms`
}
