// SPDX-License-Identifier: Apache-2.0
/**
 * Load a conversation **setup** from JSON into the new-run form.
 *
 * Note the wording: a *setup* is a definition of a conversation that has not run yet.
 * The codebase already uses "imported" for a completed transcript brought in from
 * elsewhere (`config.imported`, see `branching.py`), which is a different thing
 * entirely — that is history, this is an input.
 *
 * ## The format is the create-run request
 *
 * Deliberately not a new schema. `{ topic, cast: [{ name, persona, goals }] }` is
 * already what `POST /api/runs` accepts, so a setup file is just that body — which
 * means anything the API can run, a file can express, and the two cannot drift apart.
 * Everything beyond `topic` and `cast[].name`/`persona` is optional.
 *
 * ## Tolerant on the way in, strict about what it tells you
 *
 * A hand-written or LLM-produced setup file will have small problems, and the useful
 * response is to load what works and *say* what did not, rather than rejecting the
 * file. So this returns warnings alongside the draft: an operator who pasted eight
 * personas and got seven wants to know which one vanished and why. Silent dropping is
 * the failure mode being avoided.
 *
 * Loading into the form rather than starting a run directly is also deliberate. The
 * sample setups people actually have carry no convictions and no config, and the
 * point of loading them into the form is to add those before running.
 */

import type { DraftDoc, DraftPersona } from '../views/newRunTypes'

export interface ImportedSetup {
  topic: string
  name?: string
  description?: string
  cast: DraftPersona[]
  maxMessages?: number
  cognition?: {
    enabled: boolean
    memory?: boolean
    reflection?: boolean
    goals_dynamic?: boolean
    relationships?: boolean
  }
  /** Problems that did not stop the load. Shown to the operator verbatim. */
  warnings: string[]
}

export class ImportError extends Error {}

function asStringList(value: unknown): string[] {
  if (typeof value === 'string') return value.split(/[\n;]+/).map((s) => s.trim()).filter(Boolean)
  if (!Array.isArray(value)) return []
  return value.map((v) => String(v).trim()).filter(Boolean)
}

/** Render a `structured` block back into the form's editable line format. */
function positionsFromStructured(vps: unknown): { positions: string; concerns: string } {
  if (!Array.isArray(vps)) return { positions: '', concerns: '' }
  const lines: string[] = []
  const concerns: string[] = []
  for (const raw of vps) {
    if (!raw || typeof raw !== 'object') continue
    const v = raw as Record<string, unknown>
    const position = String(v.position ?? '').trim()
    if (!position) continue
    const firmness = String(v.firmness ?? 'negotiable').trim() || 'negotiable'
    const shifts = asStringList(v.evidence_that_shifts).join('; ')
    lines.push(`[${firmness}] ${position}${shifts ? ` -> ${shifts}` : ''}`)
    // Index-aligned with the positions, blanks included, so a concern cannot slide
    // onto the wrong stance.
    concerns.push(String(v.underlying_concern ?? '').trim())
  }
  return { positions: lines.join('\n'), concerns: concerns.join('\n') }
}

function documentsFrom(member: Record<string, unknown>, warnings: string[], who: string): DraftDoc[] {
  const out: DraftDoc[] = []
  const inline = member.document_texts
  if (Array.isArray(inline)) {
    for (const raw of inline) {
      if (!raw || typeof raw !== 'object') continue
      const d = raw as Record<string, unknown>
      const text = String(d.text ?? '')
      if (!text.trim()) continue
      out.push({ title: String(d.title ?? '').trim(), text })
    }
  }
  // `documents` holds SERVER-readable paths. A browser cannot read them, and silently
  // discarding them would leave the operator wondering where their background went.
  const paths = asStringList(member.documents)
  if (paths.length) {
    warnings.push(
      `${who}: ${paths.length} document path(s) skipped — paths are only readable by ` +
        `the server, so paste the text instead (${paths.join(', ')}).`,
    )
  }
  return out
}

export function parseSetup(raw: string): ImportedSetup {
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch (e) {
    throw new ImportError(`That is not valid JSON: ${e instanceof Error ? e.message : e}`)
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new ImportError('Expected a JSON object with "topic" and "cast".')
  }
  const obj = data as Record<string, unknown>

  const topic = String(obj.topic ?? '').trim()
  if (!topic) throw new ImportError('The setup needs a "topic".')
  if (!Array.isArray(obj.cast) || obj.cast.length === 0) {
    throw new ImportError('The setup needs a non-empty "cast" array.')
  }

  const warnings: string[] = []
  const cast: DraftPersona[] = []
  const seen = new Set<string>()

  obj.cast.forEach((rawMember, i) => {
    if (!rawMember || typeof rawMember !== 'object') {
      warnings.push(`Cast entry ${i + 1} was not an object and was skipped.`)
      return
    }
    const m = rawMember as Record<string, unknown>
    const name = String(m.name ?? '').trim()
    const persona = String(m.persona ?? '').trim()
    if (!name || !persona) {
      warnings.push(
        `Cast entry ${i + 1}${name ? ` ("${name}")` : ''} was skipped: both "name" and ` +
          `"persona" are required.`,
      )
      return
    }
    // Duplicate names collide in the engine's agent dict, which would silently drop a
    // persona at run start — better to say so now.
    if (seen.has(name.toLowerCase())) {
      warnings.push(`"${name}" appears more than once; the later one was skipped.`)
      return
    }
    seen.add(name.toLowerCase())

    const structured = (m.structured ?? {}) as Record<string, unknown>
    const prefs = (structured.preferences ?? {}) as Record<string, unknown>
    const { positions, concerns } = positionsFromStructured(structured.viewpoints)

    cast.push({
      name,
      persona,
      goals: asStringList(m.goals).join('\n'),
      positions,
      concerns,
      dismisses: asStringList(prefs.dismisses).join('\n'),
      documents: documentsFrom(m, warnings, name),
    })
  })

  if (cast.length === 0) {
    throw new ImportError('No usable personas in the file — every cast entry was skipped.')
  }

  const config = (obj.config ?? {}) as Record<string, unknown>
  const cog = (config.cognition ?? {}) as Record<string, unknown>
  const maxMessages = Number(config.max_messages)

  return {
    topic,
    name: String(obj.name ?? '').trim() || undefined,
    description: String(obj.description ?? '').trim() || undefined,
    cast,
    maxMessages: Number.isFinite(maxMessages) && maxMessages > 0 ? maxMessages : undefined,
    // Only returned when the file actually said something about cognition. Absent
    // means "leave the form's default alone" rather than "turn it off" — a setup file
    // written before cognition existed should not silently disable it.
    cognition: cog.enabled === undefined
      ? undefined
      : {
          enabled: Boolean(cog.enabled),
          memory: cog.memory === undefined ? true : Boolean(cog.memory),
          reflection: Number(cog.reflection_every ?? 4) > 0,
          goals_dynamic: Boolean(cog.goals_dynamic),
          relationships: Boolean(cog.relationships),
        },
    warnings,
  }
}
