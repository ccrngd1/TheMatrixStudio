// SPDX-License-Identifier: Apache-2.0
// The stage's name tags carry the simulated-persona marker as pixels (`stage.ts`, `drawNameTag`). jsdom has no
// canvas, so a recording context stands in for one.
import { describe, expect, it } from 'vitest'
import { drawNameTag, type Actor } from './stage'

function recorder() {
  const rects: [number, number, number, number][] = []
  const texts: { text: string; x: number }[] = []
  const ctx = {
    font: '',
    fillStyle: '',
    textAlign: '',
    textBaseline: '',
    save() {},
    restore() {},
    measureText: (s: string) => ({ width: s.length * 4 }),
    fillRect: (x: number, y: number, w: number, h: number) => rects.push([x, y, w, h]),
    fillText: (text: string, x: number) => texts.push({ text, x }),
  }
  return { ctx: ctx as unknown as CanvasRenderingContext2D, rects, texts }
}

const actor = (over: Partial<Actor> = {}): Actor => ({
  key: 'Ana Silva', label: 'Ana', sprite: 's1', marks: [],
  pose: { x: 100, y: 200, facing: 'down', walking: false, visible: true } as Actor['pose'],
  ...over,
})

describe('drawNameTag', () => {
  it('draws the robot marker before a persona’s name, inside a tag widened to hold it', () => {
    const plain = recorder()
    drawNameTag(plain.ctx, actor({ bot: false }), false)
    const marked = recorder()
    drawNameTag(marked.ctx, actor({ bot: true }), false)

    // The marker is single pixels; the tag itself is the one larger rect.
    const pixels = marked.rects.filter(([, , w, h]) => w === 1 && h === 1)
    expect(pixels.length).toBe(17) // every "#" in the 5×5 robot
    expect(plain.rects.filter(([, , w, h]) => w === 1 && h === 1)).toHaveLength(0)
    expect(marked.rects[0][2]).toBe(plain.rects[0][2] + 7)
    // The name is still drawn, in full, after the marker.
    expect(marked.texts.map((t) => t.text)).toEqual(['ANA'])
    expect(marked.texts[0].x).toBeGreaterThan(Math.max(...pixels.map(([x]) => x)))
  })

  it('leaves the messenger’s tag unmarked: it carries the operator’s words, not a persona’s', () => {
    const r = recorder()
    drawNameTag(r.ctx, actor({ key: '__messenger__', label: 'STATE', bot: undefined }), true)
    expect(r.rects.filter(([, , w, h]) => w === 1 && h === 1)).toHaveLength(0)
  })
})
