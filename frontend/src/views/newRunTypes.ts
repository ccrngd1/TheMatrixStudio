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
}

export function blankPersona(): DraftPersona {
  return {
    name: '', persona: '', goals: '', positions: '', concerns: '', dismisses: '', documents: [],
  }
}
