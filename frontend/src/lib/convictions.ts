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
 * - `underlying_concern` IS authorable, in its own field rather than crammed into the
 *   line. One concern per line, matched to positions **by index** — which is exactly
 *   how the engine already renders them into the prompt ("numbered to match"), so the
 *   two representations agree.
 *
 *   It gets its own box because it is prose about personal stakes, not a clause, and
 *   because it needs a label explaining that it is withheld. An earlier version
 *   dropped it entirely on the theory that a casual form would invite filling it in
 *   without understanding the consequence; the wizard now generates it *with* that
 *   explanation, which removes the objection. Throwing the field away at submit was
 *   discarding it at exactly the point it became usable.
 */

export const FIRMNESS = ['negotiable', 'firm', 'non-negotiable', 'requires-escalation'] as const
export type Firmness = (typeof FIRMNESS)[number]

export interface ParsedViewpoint {
  position: string
  firmness: Firmness
  evidence_that_shifts?: string[]
  /** Withheld from the conversation; the persona knows it and only says it if asked. */
  underlying_concern?: string
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
  concerns?: string
}): { viewpoints: ParsedViewpoint[]; preferences?: { dismisses: string[] } } | undefined {
  const viewpoints = parsePositions(input.positions)
  const dismisses = parseList(input.dismisses)

  // Concerns attach to positions BY INDEX — line 1 to position 1 — mirroring the
  // engine's own "numbered to match" rendering. Split on newline only, keeping blank
  // lines as gaps: otherwise a concern left empty for position 2 would slide onto
  // position 3 and attach the wrong worry to the wrong stance.
  const concerns = (input.concerns ?? '').split('\n').map((c) => c.trim())
  const withConcerns = viewpoints.map((vp, i) =>
    concerns[i] ? { ...vp, underlying_concern: concerns[i] } : vp,
  )

  if (!withConcerns.length && !dismisses.length) return undefined
  return {
    viewpoints: withConcerns,
    ...(dismisses.length ? { preferences: { dismisses } } : {}),
  }
}
