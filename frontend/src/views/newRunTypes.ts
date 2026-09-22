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

/**
 * Whether the form starts one conversation or several.
 *
 * A RUN TYPE rather than another speaker method, which is `docs/ENSEMBLE-CONVERSATIONS.md`
 * §8's wording and is not a cosmetic distinction: a speaker method decides who talks inside
 * one conversation, while this decides how many conversations exist and produces a different
 * kind of artefact — a report over N of them, with per-cell counts.
 */
export type RunType = 'single' | 'ensemble'

/**
 * Replicates per cell the form offers by default.
 *
 * Matches `ensemble_spec.DEFAULT_REPLICATES`. Five is what supports the coarse tiers
 * (unanimous / split / rare) the report is built on, and nothing finer — §8.2.
 */
export const DEFAULT_REPLICATES = 5

/**
 * Fewest replicates a cell may have.
 *
 * Mirrors `ensemble_spec.MIN_REPLICATES`, and the server refuses anything lower. Two is not
 * arbitrary: with one run a cell has no within-cell variance, and that variance is the only
 * thing that distinguishes a real finding from a coin flip.
 */
export const MIN_REPLICATES = 2

/**
 * Most members the form will offer across all cells.
 *
 * Mirrors `ensemble_spec.MAX_MEMBERS`. Not a cost control — the monthly cap is — but a guard
 * against a slip fanning out a dozen conversations. The form also shows the member count so
 * the multiplier is never a surprise.
 */
export const MAX_MEMBERS = 12
