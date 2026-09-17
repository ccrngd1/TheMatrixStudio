// SPDX-License-Identifier: Apache-2.0
/**
 * Draft shapes for the new-run form.
 *
 * Extracted from `NewRunForm.tsx` so the setup importer and the convictions parser can
 * share them without importing the component — a lib importing a view would drag React
 * into unit tests that have no need of it.
 */

export interface DraftDoc {
  title: string
  text: string
}

export interface DraftPersona {
  name: string
  persona: string
  goals: string // newline/semicolon separated in the form
  /**
   * Phase 6 convictions, authored as plain text rather than a nested form. One
   * position per line, optional `[firmness]` prefix and `-> what would change your
   * mind`. A structured editor for four nested fields would be a worse authoring
   * experience than a line of text, and this parses losslessly into the API shape.
   */
  positions: string
  /** Withheld concerns, one per line, matched to `positions` BY INDEX. */
  concerns: string
  dismisses: string // one concern per line
  /**
   * Phase 5 background documents, pasted inline. The browser cannot supply
   * server-readable paths, so inline text is the only workable browser flow.
   */
  documents: DraftDoc[]
  /**
   * Phase 6 knowledge bases bound to THIS persona alone. Distinct from
   * `documents`, and the difference is the point of Phase 6: a document pasted
   * here is indexed for this run only, while a bound collection is indexed once
   * and searchable from any conversation that binds it.
   *
   * The effective scope for a speaker is `run.knowledge_bases ∪
   * persona.knowledge_bases`, intersected with what the caller may actually read
   * — and that intersection is re-checked every turn, so a revoked grant stops
   * working mid-run.
   */
  knowledgeBases: string[]
}

export function blankPersona(): DraftPersona {
  return {
    name: '', persona: '', goals: '', positions: '', concerns: '', dismisses: '',
    documents: [], knowledgeBases: [],
  }
}

/**
 * The turn count a run gets when "end when the conversation is finished" is switched on.
 *
 * High on purpose: with that on, the number is a ceiling that only catches a conversation
 * which never converges, and a low one would cut a converging run short — reintroducing the
 * arbitrary length the feature exists to remove. 100 is about 3x the longest observed
 * convergence point (turn 32 of 40 on a 6-persona cast), which leaves room for a bigger cast
 * without being an unbounded bill.
 */
export const CEILING_TURNS = 100

/** How the next speaker is decided. Mirrors `SelectionConfig.method` in the engine. */
export type Method = 'moderated' | 'rotation' | 'simultaneous' | 'hybrid'
