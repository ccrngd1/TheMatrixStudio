// SPDX-License-Identifier: Apache-2.0
// What is actually drawn in the room, read back from the canvas.
//
// These read pixels at logical room coordinates, using the same constants the app lays the room
// out with, so a change to the seating moves the tests with it rather than breaking them. They are
// not image comparisons: nothing here depends on a sprite's artwork, only on whether something is
// standing where the transcript says someone should be.

import { ROOM_H, ROOM_W, seatLayout } from '../src/theatre/script'
import { MESSENGER_SPOT, SME_SPOT } from '../src/theatre/blocking'
import { CAST, EXPERT, expect, stubApi, test } from './fixtures'

type Rgb = [number, number, number]

const seats = seatLayout(CAST.length)
const seatOf = (name: string) => seats[CAST.indexOf(name as (typeof CAST)[number])]

/** The palette the room is drawn with, from `stage.ts` and `theatre.css`. */
const GOLD: Rgb = [255, 210, 74] // a lit name tag
const NOTE: Rgb = [255, 226, 122] // the first whiteboard note
/**
 * What is there when nobody is: the carpet's two checker squares, a chair and its highlight, and
 * the table's four woods. An empty chair counts as empty, which is the point of the list — and the
 * table is in it because a near-side seat sits below the tabletop and beside one of its legs, so
 * any band sampled above those feet crosses furniture.
 */
const EMPTY: Rgb[] = [
  [52, 66, 100], [46, 58, 89], // carpet
  [37, 44, 56], [58, 68, 86], // chair, chair highlight
  [139, 90, 43], [162, 106, 53], [92, 58, 26], [185, 127, 69], // table
]

const near = (a: Rgb, b: Rgb, tol = 12) => a.every((v, i) => Math.abs(v - b[i]) <= tol)
const isEmpty = (c: Rgb) => EMPTY.some((f) => near(c, f))

/** Where a name tag's plate is, clear of its text. */
const tagAt = (s: { y: number; behindTable: boolean }) => (s.behindTable ? s.y - 39 : s.y + 5)

/** Read one pixel at a logical room coordinate. */
async function pixel(page: import('@playwright/test').Page, x: number, y: number): Promise<Rgb> {
  return page.evaluate(
    ([lx, ly, w, h]) => {
      const c = document.querySelector('canvas') as HTMLCanvasElement
      const ctx = c.getContext('2d')!
      const d = ctx.getImageData(Math.round((lx * c.width) / w), Math.round((ly * c.height) / h), 1, 1).data
      return [d[0], d[1], d[2]] as [number, number, number]
    },
    [x, y, ROOM_W, ROOM_H] as const,
  )
}

/**
 * True when somebody is standing or sitting with their feet at this point.
 *
 * Sampled across the torso, 18 to 26 px above the feet, at three x offsets. Anything that is not
 * carpet, chair or table is somebody, so an empty seat reads as empty however the furniture falls
 * around it.
 */
async function occupied(page: import('@playwright/test').Page, spot: { x: number; y: number }) {
  for (const dx of [-4, 0, 4]) {
    for (const dy of [-26, -22, -18]) {
      if (!isEmpty(await pixel(page, spot.x + dx, spot.y + dy))) return true
    }
  }
  return false
}

const start = async (page: import('@playwright/test').Page) => {
  await page.getByRole('button', { name: /Start/ }).click()
  await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible()
}
const pause = (page: import('@playwright/test').Page) => page.getByRole('button', { name: 'Pause' }).click()
const play = (page: import('@playwright/test').Page) => page.getByRole('button', { name: 'Play' }).click()
const next = (page: import('@playwright/test').Page) => page.getByRole('button', { name: 'Next page' }).click()

test.describe('the room', () => {
  test('draws the cast, and lights only the speaker’s name tag', async ({ theatre: page }) => {
    await start(page)
    await pause(page)
    await expect(page.getByText(/LINE 1\/8/)).toBeVisible()

    // Eve speaks first. Her tag sits below her feet, because she is on the near side.
    const eve = seatOf('Eve Morgan')
    expect(eve.behindTable).toBe(false)
    await expect.poll(() => pixel(page, eve.x, tagAt(eve)).then((c) => near(c, GOLD))).toBe(true)

    // Everyone else's tag is the dark plate, and everyone is drawn in their seat.
    for (const name of ['Ana Silva', 'Chloe Wu', 'Hana Sato']) {
      const s = seatOf(name)
      expect(near(await pixel(page, s.x, tagAt(s)), GOLD)).toBe(false)
      expect(await occupied(page, s)).toBe(true)
    }
  })

  test('pins the assumptions that were up at that line to the whiteboard', async ({ theatre: page }) => {
    await start(page)
    await pause(page)
    // The first note goes on at x 127, y 18, 31 x 27 (see drawBoard).
    await expect.poll(() => pixel(page, 140, 30).then((c) => near(c, NOTE))).toBe(true)
    await expect(page.getByLabel('Working assumptions on the board')).toContainText('Headcount stays flat')
  })

  test('walks the consultant in to answer, then out again', async ({ theatre: page }) => {
    await start(page)
    await pause(page)
    for (let i = 0; i < 5; i++) await next(page) // to the consultant's answer, line 6
    await expect(page.getByText(/LINE 6\/8/)).toBeVisible()
    await expect(page.getByLabel('Dialogue: tap to continue')).toContainText(EXPERT) // uppercased by CSS, so the DOM keeps its own capitalisation

    // Nobody is standing there yet: they are still outside the door.
    expect(await occupied(page, SME_SPOT)).toBe(false)

    await play(page)
    await expect.poll(() => occupied(page, SME_SPOT), { timeout: 15_000, intervals: [100] }).toBe(true)
    // The whole cast stayed seated, the persona who asked included.
    expect(await occupied(page, seatOf('Farah Haddad'))).toBe(true)

    await next(page)
    await expect.poll(() => occupied(page, SME_SPOT), { timeout: 15_000, intervals: [100] }).toBe(false)
  })

  test('brings an outside message in by messenger', async ({ theatre: page }) => {
    await start(page)
    await pause(page)
    for (let i = 0; i < 4; i++) await next(page) // to the customer email, line 5
    await expect(page.getByLabel('Dialogue: tap to continue')).toContainText(/injected into the conversation/)
    expect(await occupied(page, MESSENGER_SPOT)).toBe(false)
    await play(page)
    await expect.poll(() => occupied(page, MESSENGER_SPOT), { timeout: 15_000, intervals: [100] }).toBe(true)
  })

  test('loads every sprite sheet it asks for', async ({ page }) => {
    const bad: string[] = []
    page.on('response', (r) => {
      if (r.url().includes('/theatre/sprites/') && !r.ok()) bad.push(`${r.status()} ${r.url()}`)
    })
    await stubApi(page)
    await page.goto('/theatre.html?run=demo')
    await start(page)
    await page.waitForTimeout(1500)
    expect(bad).toEqual([])
  })
})

test.describe('the research prologue', () => {
  test('starts with an empty room and walks everyone in', async ({ page }) => {
    await stubApi(page, { research: true })
    await page.goto('/theatre.html?run=demo')
    await start(page)
    await expect(page.getByText('BEFORE TURN 1')).toBeVisible()

    // The seats are empty at first: this is the room arriving, not the room already there.
    const last = seatOf('Hana Sato')
    expect(await occupied(page, last)).toBe(false)

    // They file in, and by the end of the walk everybody has a seat.
    for (const name of ['Ana Silva', 'Hana Sato']) {
      await expect.poll(() => occupied(page, seatOf(name)), { timeout: 20_000, intervals: [150] }).toBe(true)
    }
  })
})
