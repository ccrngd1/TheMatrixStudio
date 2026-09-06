// SPDX-License-Identifier: Apache-2.0
/**
 * Parse convictions authored as plain text into the `structured` shape the API takes.
 *
 * Why text rather than a nested form: a viewpoint has four fields (`position`,
 * `firmness`, `evidence_that_shifts`, `formed_by`) and a persona can hold several.
 * A nested editor for that is slow to fill in and hostile to editing, whereas one
 * line per position is fast to type and fast to re-read. The line format is
 * lossless into the API shape, so nothing is given up:
 *
 *     [firm] No feature may add an external service -> an embedded index that is a file
 *     Ship this quarter
 *     [requires-escalation] Security signs off first -> a written exception
 *
 * - `[firmness]` is optional and defaults to `negotiable`, which is the safe default:
 *   a position wrongly marked `negotiable` merely gets argued normally, whereas one
 *   wrongly marked firm becomes an immovable wall.
 * - Everything after `->` is the exit condition (`evidence_that_shifts`),
 *   semicolon-separated for more than one.
 * - `underlying_concern` is deliberately NOT authorable here. It is the withheld
 *   field, and offering it on a screen this casual would invite filling it in without
 *   realising it changes what the persona will say when pressed. It stays a
 *   config-file capability until there is a UI that can explain it properly.
 */

export const FIRMNESS = ['negotiable', 'firm', 'non-negotiable', 'requires-escalation'] as const
export type Firmness = (typeof FIRMNESS)[number]

export interface ParsedViewpoint {
  position: string
  firmness: Firmness
  evidence_that_shifts?: string[]
}

const FIRMNESS_RE = /^\s*\[([^\]]+)\]\s*/

/** One position per line. Unknown or absent firmness becomes `negotiable`. */
export function parsePositions(text: string): ParsedViewpoint[] {
  const out: ParsedViewpoint[] = []
  for (const rawLine of text.split('\n')) {
    let line = rawLine.trim()
    if (!line) continue

    let firmness: Firmness = 'negotiable'
    const m = line.match(FIRMNESS_RE)
    if (m) {
      const candidate = m[1].trim().toLowerCase()
      // An unrecognised tag is treated as prose rather than dropped, so a typo
      // does not silently delete the operator's text.
      if ((FIRMNESS as readonly string[]).includes(candidate)) {
        firmness = candidate as Firmness
        line = line.slice(m[0].length)
      }
    }

    const [positionPart, ...rest] = line.split('->')
    const position = positionPart.trim()
    if (!position) continue

    const shifts = rest
      .join('->')
      .split(';')
      .map((x) => x.trim())
      .filter(Boolean)

    out.push({
      position,
      firmness,
      ...(shifts.length ? { evidence_that_shifts: shifts } : {}),
    })
  }
  return out
}

/** Split a newline/semicolon separated list, dropping blanks. */
export function parseList(text: string): string[] {
  return text
    .split(/[\n;]+/)
    .map((x) => x.trim())
    .filter(Boolean)
}

/**
 * Build the `structured` payload, or `undefined` when the operator authored nothing.
 *
 * Returning `undefined` rather than an empty object matters: an empty `structured`
 * block is treated as absent by the backend, and sending one would imply the feature
 * was configured when it was not.
 */
export function buildStructured(input: {
  positions: string
  dismisses: string
}): { viewpoints: ParsedViewpoint[]; preferences?: { dismisses: string[] } } | undefined {
  const viewpoints = parsePositions(input.positions)
  const dismisses = parseList(input.dismisses)
  if (!viewpoints.length && !dismisses.length) return undefined
  return {
    viewpoints,
    ...(dismisses.length ? { preferences: { dismisses } } : {}),
  }
}
