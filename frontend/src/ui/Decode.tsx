// SPDX-License-Identifier: Apache-2.0
// A title decodes from glyphs when the screen changes (docs/MOBILE-UI.md §5.4): left to right, each character
// settles out of the rain's glyphs, over 0.65 s. Ported from the prototype's `scramble`.
//
// Only the eye sees the glyphs. The real title is in the DOM from the first frame, visually hidden, and the
// scrambling layer beside it is aria-hidden, so a screen reader (and a test) reads the title at once. It runs
// when the text changes, not on every render, and not at all with FX off or reduced motion asked for.
import { useLayoutEffect, useRef, useState } from 'react'
import { useMotion } from './fx'
import { GLYPHS } from './Rain'

export const DECODE_MS = 650

const glyph = () => GLYPHS[Math.floor(Math.random() * GLYPHS.length)]

export function Decode({ text }: { text: string }) {
  const motion = useMotion()
  // Decode on mount, and again whenever the title changes (a run's codename arriving after "Run").
  const [shown, setShown] = useState(text)
  const [pending, setPending] = useState(motion)
  if (shown !== text) {
    setShown(text)
    setPending(motion)
  }
  const active = pending && motion
  const layer = useRef<HTMLSpanElement>(null)

  // A layout effect, so the first scrambled frame is painted in place of the title rather than after it.
  useLayoutEffect(() => {
    const el = layer.current
    if (!active || !el) return
    const start = performance.now()
    let raf = 0
    const step = (now: number) => {
      const p = Math.min(1, (now - start) / DECODE_MS)
      const n = Math.floor(text.length * p)
      el.textContent = text.slice(0, n) + [...text.slice(n)].map((c) => (c === ' ' ? ' ' : glyph())).join('')
      if (p < 1) raf = requestAnimationFrame(step)
      else setPending(false)
    }
    step(start)
    return () => cancelAnimationFrame(raf)
  }, [active, text])

  if (!active) return <>{text}</>
  return (
    <>
      <span className="sr-only">{text}</span>
      <span ref={layer} aria-hidden="true" />
    </>
  )
}
