// SPDX-License-Identifier: Apache-2.0
// The falling-glyph rain behind the panels (docs/MOBILE-UI.md §5.4, stage 6), in the theme's `--rain` colour:
// green on matrix. Ported from the prototype's canvas loop.
//
// It is decoration, so it gives way to everything: the CSS hides it with FX off, it draws nothing while
// hidden or with reduced motion asked for, it fades out down the screen (the mask in command.css) so it never
// sits behind text, and it redraws at ~16 fps rather than every frame. One loop for the life of the app,
// started once, so a re-render cannot restart it.
import { useEffect, useRef } from 'react'
import type { Theme } from './theme'

export const GLYPHS = 'ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉ0123456789<>/=+*#'
const COL = 16
const FRAME_MS = 60

export function Rain({ fx, theme }: { fx: boolean; theme: Theme }) {
  const ref = useRef<HTMLCanvasElement>(null)
  const live = useRef({ fx, color: '#22d3ee' })
  live.current.fx = fx

  // The colour follows the theme without restarting the loop.
  useEffect(() => {
    live.current.color =
      getComputedStyle(document.documentElement).getPropertyValue('--rain').trim() || '#22d3ee'
  }, [theme])

  useEffect(() => {
    const c = ref.current
    let ctx: CanvasRenderingContext2D | null = null
    try {
      ctx = c?.getContext('2d') ?? null
    } catch {
      ctx = null
    }
    if (!c || !ctx) return // jsdom, or a browser without canvas
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    let w = 0, h = 0
    let drops: number[] = []
    const size = () => {
      const r = c.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1)
      c.width = Math.max(1, r.width * dpr)
      c.height = Math.max(1, r.height * dpr)
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0)
      w = r.width
      h = r.height
      drops = Array.from({ length: Math.ceil(w / COL) }, (_, i) => drops[i] ?? Math.floor(Math.random() * -60))
    }
    size()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(size) : null
    ro?.observe(c)
    let raf = 0, last = 0
    const frame = (t: number) => {
      raf = requestAnimationFrame(frame)
      if (!live.current.fx || reduce?.matches || document.hidden || t - last < FRAME_MS) return
      last = t
      ctx!.globalCompositeOperation = 'destination-out'
      ctx!.fillStyle = 'rgba(0,0,0,.12)'
      ctx!.fillRect(0, 0, w, h)
      ctx!.globalCompositeOperation = 'source-over'
      ctx!.font = '13px ui-monospace,monospace'
      drops.forEach((y, i) => {
        ctx!.fillStyle = Math.random() > 0.95 ? '#ffffff' : live.current.color
        ctx!.fillText(GLYPHS[Math.floor(Math.random() * GLYPHS.length)], i * COL, y * COL)
        drops[i] = y * COL > h && Math.random() > 0.975 ? 0 : y + 1
      })
    }
    raf = requestAnimationFrame(frame)
    return () => {
      cancelAnimationFrame(raf)
      ro?.disconnect()
    }
  }, [])

  return <canvas id="cc-rain" ref={ref} aria-hidden="true" />
}
