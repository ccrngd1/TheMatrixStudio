// SPDX-License-Identifier: Apache-2.0
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sfx, soundWanted } from './sfx'

/** Enough of the Web Audio API to record what a play would have made. */
function fakeAudio() {
  const started: { type: string; from: number; stopped: boolean }[] = []
  const ctx = {
    state: 'running' as AudioContextState,
    currentTime: 0,
    resumed: 0,
    closed: 0,
    resume() { this.resumed++ },
    close() { this.closed++ },
    createOscillator() {
      const rec = { type: '', from: 0, stopped: false }
      return {
        set type(v: string) { rec.type = v },
        frequency: {
          setValueAtTime: (v: number) => { rec.from = v },
          linearRampToValueAtTime: () => {},
        },
        connect: (n: unknown) => n,
        start: () => { started.push(rec) },
        stop: () => { rec.stopped = true },
      }
    },
    createGain() {
      return {
        gain: { setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {} },
        connect: () => ({}),
      }
    },
    destination: {},
  }
  const Ctor = function () { return ctx } as unknown as { new (): AudioContext }
  return { ctx, started, Ctor }
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('Sfx', () => {
  it('is silent until it is turned on, and makes no audio context meanwhile', () => {
    const { started, Ctor } = fakeAudio()
    const made = vi.fn(() => Ctor)
    const sfx = new Sfx(made)
    expect(sfx.enabled).toBe(false)
    sfx.play('door')
    sfx.play('chime')
    expect(started).toEqual([])
    // Nothing is built for a muted theatre, so no browser autoplay policy is ever tripped.
    expect(made).not.toHaveBeenCalled()
  })

  it('plays once turned on, and stops again when turned off', () => {
    const { started, Ctor } = fakeAudio()
    const sfx = new Sfx(() => Ctor)
    expect(sfx.toggle()).toBe(true)
    sfx.play('door')
    expect(started).toHaveLength(2)
    expect(started.every((s) => s.stopped)).toBe(true)
    expect(sfx.toggle()).toBe(false)
    sfx.play('door')
    expect(started).toHaveLength(2)
  })

  it('remembers the choice, so a new visit is not surprised by sound', () => {
    expect(soundWanted()).toBe(false)
    const { Ctor } = fakeAudio()
    new Sfx(() => Ctor).toggle()
    expect(soundWanted()).toBe(true)
    expect(new Sfx(() => Ctor).enabled).toBe(true)
  })

  it('rate-limits the typing blip, which would otherwise fire per character', () => {
    const { started, Ctor } = fakeAudio()
    const sfx = new Sfx(() => Ctor)
    sfx.toggle()
    let t = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => t)
    for (let i = 0; i < 20; i++) sfx.play('blip')
    expect(started).toHaveLength(1)
    t += 200
    sfx.play('blip')
    expect(started).toHaveLength(2)
  })

  it('wakes a context the browser suspended when the tab was in the background', () => {
    const { ctx, Ctor } = fakeAudio()
    const sfx = new Sfx(() => Ctor)
    sfx.toggle()
    sfx.play('door')
    ctx.state = 'suspended'
    sfx.play('door')
    expect(ctx.resumed).toBe(1)
  })

  it('plays on in silence where there is no audio at all', () => {
    const sfx = new Sfx(() => null)
    sfx.toggle()
    expect(() => sfx.play('chime')).not.toThrow()
  })

  it('survives a browser that refuses to build a context', () => {
    const Ctor = function () { throw new Error('not allowed') } as unknown as { new (): AudioContext }
    const sfx = new Sfx(() => Ctor)
    sfx.toggle()
    expect(() => sfx.play('blip')).not.toThrow()
  })

  it('does not fail when storage is blocked, it just does not remember', () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    expect(soundWanted()).toBe(false)
    const { Ctor } = fakeAudio()
    const sfx = new Sfx(() => Ctor)
    expect(() => sfx.toggle()).not.toThrow()
    expect(sfx.enabled).toBe(true)
    get.mockRestore()
    set.mockRestore()
  })
})
