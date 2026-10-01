// SPDX-License-Identifier: Apache-2.0
// New messages type themselves in (docs/MOBILE-UI.md §4.2, §5.4). Ported from the prototype's `typeOut`, with
// the one difference that matters in React: the real text is never touched.
//
// The message renders in full from the first frame, so its height is reserved and the feed never jumps, and
// a screen reader, a search or a test finds the whole text at once. While it types, that real paragraph is
// transparent (opacity, which leaves it in the accessibility tree) and an aria-hidden, inert copy is drawn
// over it with the untyped remainder still in place but invisible, so every line wraps exactly where the
// final text does. When the copy is done it is removed and the real paragraph shows. The reveal is visual
// only: the stream does not wait for it.
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'

const MAX_TYPE_MS = 1400
/** How long a message takes to type: quick for a line, capped for a paragraph (the prototype's pace). */
export const typeMs = (length: number) => Math.min(MAX_TYPE_MS, 300 + length * 14)
// One or two messages at once is the stream arriving. More is a jump (Catch up), which shows them at once.
const MAX_BATCH = 2

/**
 * When each message ARRIVED while this feed was open, by seq. A message that did not arrive is absent.
 *
 * Arrived means: not in the feed when it mounted (so a remount after a tab switch types nothing again), seq
 * above `liveFrom`, the last seq of the backlog `useRunStream` revealed on load (so history and replays never
 * type), and one of at most `MAX_BATCH` new at once. With `liveFrom` unset (the scrubber) or motion off,
 * nothing arrives; a message seen then is never typed later either, so turning FX on cannot replay it.
 */
export function useArrivals(seqs: number[], liveFrom: number | null | undefined, motion: boolean) {
  const known = useRef<Set<number> | null>(null)
  if (known.current === null) known.current = new Set(seqs)
  const [arrivals, setArrivals] = useState<ReadonlyMap<number, number>>(() => new Map())

  // Before paint: the render that first shows a message commits it typed-in, never a frame of it in full.
  // No dependency list, because `seqs` is a new array on every render; with nothing new this is a set lookup
  // per message and no state change.
  useLayoutEffect(() => {
    const k = known.current!
    const added = seqs.filter((s) => !k.has(s))
    if (!added.length) return
    added.forEach((s) => k.add(s))
    if (!motion || liveFrom == null) return
    const live = added.filter((s) => s > liveFrom)
    if (!live.length || live.length > MAX_BATCH) return
    const now = performance.now()
    setArrivals((prev) => {
      // Finished entries are dropped as new ones come, so a long run keeps a handful, not one per turn.
      const next = new Map([...prev].filter(([, t]) => now - t < MAX_TYPE_MS))
      live.forEach((s) => next.set(s, now))
      return next
    })
  })
  return arrivals
}

/** Whether a message that arrived at `start` (performance.now() time) is still typing. */
export const isTyping = (start: number | undefined, length: number): start is number =>
  start != null && performance.now() - start < typeMs(length)

/**
 * Types its FIRST child element in, starting at `start`; anything after it shows as it is. Without a `start`,
 * or once it has finished, it renders the children untouched, inside a wrapper that is always there so they are
 * never remounted. The copy is drawn from the top of the wrapper, which is where that first child sits.
 */
export function TypeIn({ start, length, children }: { start?: number; length: number; children: ReactNode }) {
  const [finished, setFinished] = useState<number | null>(null)
  const active = isTyping(start, length) && finished !== start
  const host = useRef<HTMLDivElement>(null)
  const layer = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    const src = host.current?.firstElementChild
    const out = layer.current
    if (!active || start == null || !src || !out) return
    out.setAttribute('inert', '')
    const dur = typeMs(length)
    let raf = 0
    let drawn = -1
    const step = (now: number) => {
      const p = (now - start) / dur
      if (p >= 1) {
        setFinished(start)
        return
      }
      // Counted from the DOM, not `length`: citations render as different text from the raw message.
      const n = Math.floor((src.textContent?.length ?? 0) * Math.max(0, p))
      if (n !== drawn) {
        out.replaceChildren(typedCopy(src, n))
        drawn = n
      }
      raf = requestAnimationFrame(step)
    }
    step(performance.now())
    return () => cancelAnimationFrame(raf)
  }, [active, start, length])

  return (
    <div ref={host} className={active ? 'cc-typein cc-typing' : 'cc-typein'}>
      {children}
      {active && <div ref={layer} className="cc-typein-fx" aria-hidden="true" />}
    </div>
  )
}

/**
 * A copy of `src` with only its first `n` characters visible. The rest stay in place, hidden, so the copy
 * lays out exactly as the original. Taken afresh each frame, so a re-render of the original mid-type (a
 * citation becoming a link) is followed. Nothing in it can take focus: the layer is inert, and older browsers
 * without `inert` get tabindex -1.
 *
 * The hidden rest is generated content (`content: attr(data-rest)` in atmosphere.css), not text, so the copy's
 * DOM text is only what has been typed: a search of the page, or a test's text query, finds the message once.
 */
function typedCopy(src: Element, n: number): Node {
  const copy = src.cloneNode(true) as Element
  const walker = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT)
  const texts: Text[] = []
  while (walker.nextNode()) texts.push(walker.currentNode as Text)
  let left = n
  for (const t of texts) {
    if (left >= t.data.length) {
      left -= t.data.length
      continue
    }
    const rest = document.createElement('span')
    rest.className = 'cc-ghost'
    rest.dataset.rest = t.data.slice(left)
    t.data = t.data.slice(0, left)
    t.after(rest)
    left = 0
  }
  for (const el of [copy, ...copy.querySelectorAll('*')]) {
    el.removeAttribute('id')
    if (el.matches('a, button, input, select, textarea, [tabindex]')) el.setAttribute('tabindex', '-1')
  }
  return copy
}
