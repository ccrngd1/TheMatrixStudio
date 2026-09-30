// SPDX-License-Identifier: Apache-2.0
// The theatre's sound, synthesised rather than sampled: square-wave blips as text types, a
// two-tone thunk for a door, a rising chime when a message arrives.
//
// Generated in code on purpose. No audio files means nothing to license, nothing to download and
// nothing to cache, and a square wave at the right pitch is exactly what the format wants.
//
// **Muted by default**, and the choice is remembered. Sound that starts itself in a tab someone
// opened from a run is the most annoying thing this page could do. The AudioContext is built on
// the first sound AFTER unmuting, which is always inside a click, because browsers refuse to
// start audio any other way.

const STORE_KEY = 'theatre.sound'

type Ctor = { new (): AudioContext }

function audioCtor(): Ctor | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as { AudioContext?: Ctor; webkitAudioContext?: Ctor }
  return w.AudioContext ?? w.webkitAudioContext ?? null
}

/** Whether sound is on, from the remembered choice. Off unless it was explicitly turned on. */
export function soundWanted(): boolean {
  try {
    return localStorage.getItem(STORE_KEY) === 'on'
  } catch {
    // A browser with storage blocked is not a reason to fail; it just will not remember.
    return false
  }
}

function remember(on: boolean): void {
  try {
    localStorage.setItem(STORE_KEY, on ? 'on' : 'off')
  } catch {
    /* not remembered, still audible for this visit */
  }
}

export type SfxName = 'blip' | 'door' | 'chime' | 'page'

/**
 * A voice per sound: frequency sweep, length, wave and gain. Kept as data so the sounds can be
 * read and adjusted without touching the player.
 */
const VOICES: Record<SfxName, { from: number; to: number; ms: number; type: OscillatorType; gain: number }[]> = {
  // Short and quiet: this one fires many times a second while a line types.
  blip: [{ from: 620, to: 660, ms: 26, type: 'square', gain: 0.035 }],
  // Two thunks a semitone apart: a door opening and closing.
  door: [
    { from: 200, to: 120, ms: 90, type: 'square', gain: 0.07 },
    { from: 150, to: 90, ms: 120, type: 'triangle', gain: 0.05 },
  ],
  // Rising, so an arriving message reads as an event rather than a fault.
  chime: [
    { from: 700, to: 700, ms: 70, type: 'square', gain: 0.055 },
    { from: 1050, to: 1050, ms: 110, type: 'square', gain: 0.045 },
  ],
  page: [{ from: 320, to: 420, ms: 45, type: 'square', gain: 0.04 }],
}

export class Sfx {
  private ctx: AudioContext | null = null
  private on: boolean
  /** Blips are rate-limited: one per this many ms however fast the text types. */
  private lastBlip = 0

  constructor(private readonly ctorFor: () => Ctor | null = audioCtor) {
    this.on = soundWanted()
  }

  get enabled(): boolean {
    return this.on
  }

  /** Turn sound on or off, remember it, and return the new state. */
  toggle(): boolean {
    this.on = !this.on
    remember(this.on)
    if (!this.on) this.close()
    return this.on
  }

  private context(): AudioContext | null {
    if (this.ctx) return this.ctx
    const Ctor = this.ctorFor()
    if (!Ctor) return null
    try {
      this.ctx = new Ctor()
    } catch {
      // Audio unavailable (no output device, a policy refusal): the theatre plays on in silence.
      this.ctx = null
    }
    return this.ctx
  }

  play(name: SfxName): void {
    if (!this.on) return
    const ctx = this.context()
    if (!ctx) return
    // A tab that has been backgrounded suspends its context; a click brings it back.
    if (ctx.state === 'suspended') void ctx.resume?.()
    const now = ctx.currentTime
    if (name === 'blip') {
      if (performance.now() - this.lastBlip < 55) return
      this.lastBlip = performance.now()
    }
    VOICES[name].forEach((v, i) => {
      try {
        const osc = ctx.createOscillator()
        const gain = ctx.createGain()
        const at = now + i * 0.05
        const until = at + v.ms / 1000
        osc.type = v.type
        osc.frequency.setValueAtTime(v.from, at)
        if (v.to !== v.from) osc.frequency.linearRampToValueAtTime(v.to, until)
        // Ramped down rather than stopped flat, because an abrupt cut clicks.
        gain.gain.setValueAtTime(v.gain, at)
        gain.gain.exponentialRampToValueAtTime(0.0001, until)
        osc.connect(gain).connect(ctx.destination)
        osc.start(at)
        osc.stop(until)
      } catch {
        /* one sound failing is not worth interrupting a replay for */
      }
    })
  }

  close(): void {
    void this.ctx?.close?.()
    this.ctx = null
  }
}
