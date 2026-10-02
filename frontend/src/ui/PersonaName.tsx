// SPDX-License-Identifier: Apache-2.0
// Every persona and consultant is simulated, and the owner's rule (2026-10-02) is that their names always say so.
// This is the one place that marker is drawn, so it looks the same on every surface and changes in one edit.
//
// DISPLAY ONLY. The stored name is unchanged, nothing here is ever sent to the API, and the marker never enters a
// prompt: personas must not start addressing each other as "(bot) Ruth". A surface that needs the name for a
// request, a key or a comparison uses the plain name, never `botLabel`.
//
// Not used for an injected operator message (the Incoming banner, a scheduled message, the Narrator): that is the
// operator speaking, not a simulated persona, and marking it would say the opposite of what it is.
import { Icon } from './icons'
import './PersonaName.css'

/** What a screen reader says for the glyph. Said once, before the name, and nowhere else on the name. */
export const SIMULATED_LABEL = 'simulated persona'

/** The plain-text form, for places an icon cannot go: an `<option>`, a canvas, a document title. */
export const BOT_PREFIX = '(bot) '
export const botLabel = (name: string) => `${BOT_PREFIX}${name}`

/** The glyph alone, for beside a name the user is typing (an input cannot hold it). */
export function BotMark({ size = 12, className }: { size?: number; className?: string }) {
  return <Icon name="bot" size={size} label={SIMULATED_LABEL} className={`cc-botmark${className ? ` ${className}` : ''}`} />
}

/**
 * A persona's (or a consultant's) name with the simulated-persona marker before it. The name stays its own text
 * node, so what reads the text — a search, a copy, a test — reads the name; the glyph carries the marker.
 */
export function PersonaName({ name, className }: { name: string; className?: string }) {
  return (
    <span className={`cc-pname${className ? ` ${className}` : ''}`}>
      <BotMark />
      {name}
    </span>
  )
}

/** Several names, separated, each marked. */
export function PersonaNames({ names, sep = ' · ' }: { names: readonly string[]; sep?: string }) {
  return (
    <>
      {names.map((n, i) => (
        <span key={`${n}-${i}`}>
          {i > 0 && sep}
          <PersonaName name={n} />
        </span>
      ))}
    </>
  )
}
