// SPDX-License-Identifier: Apache-2.0
import { Hint } from './Hint'
import { KbPicker } from './KbPicker'
import { RenamedNotice } from './RenamedNotice'
import { showsRenamed } from '../lib/realNames'
import type { Renamed } from '../types'
import { BotMark } from '../ui/PersonaName'

export interface DraftConsultant {
  name: string
  expertise: string
  knowledgeBases: string[]
  docTitle: string
  docText: string
  /** Set when the name was switched because it was a real public figure's (`RenamedNotice`). Never sent. */
  renamed?: Renamed
}

export const blankConsultant = (): DraftConsultant => ({
  name: '', expertise: '', knowledgeBases: [], docTitle: '', docText: '',
})

/** The consultants as `config.experts` takes them: named ones only, sources attached. */
export function consultantsConfig(list: DraftConsultant[]) {
  return list
    .filter((c) => c.name.trim())
    .map((c) => ({
      name: c.name.trim(),
      expertise: c.expertise.trim(),
      ...(c.knowledgeBases.length ? { knowledge_bases: c.knowledgeBases } : {}),
      ...(c.docText.trim()
        ? { document_texts: [{ title: c.docTitle.trim() || `${c.name.trim()} notes`, text: c.docText }] }
        : {}),
    }))
}

interface Props {
  consultants: DraftConsultant[]
  onChange: (list: DraftConsultant[]) => void
  limit: number
  onLimit: (n: number) => void
  /**
   * Which consultant's name field has focus (`null` on blur). The form checks names for a real public figure's
   * on blur, not while one is being typed: switching a name under the cursor mid-word would be hostile.
   */
  onNameFocus?: (i: number | null) => void
}

// Experts outside the room (matrix_studio/experts.py). A persona asks one a specific question; the
// consultant answers only from the sources given here, citing them, or says the answer is not in
// them. A consultant never takes a turn or holds a position.
export function ConsultantsEditor({ consultants, onChange, limit, onLimit, onNameFocus }: Props) {
  const update = (i: number, patch: Partial<DraftConsultant>) =>
    onChange(consultants.map((c, n) => (n === i ? { ...c, ...patch } : c)))
  const input = 'rounded border border-matrix-border bg-matrix-bg p-2 text-sm'

  return (
    <div className="mt-6 rounded-lg border border-matrix-border bg-matrix-panel p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="flex items-center gap-1 text-sm font-semibold text-slate-300">
          Consultants
          <Hint label="consultants">
            Experts who are not in the conversation. When a persona needs a fact — what a statute says,
            what a measurement showed — it can ask a consultant, who answers from its own documents and
            knowledge bases with citations, or says "That isn't in my sources." The answer appears in the
            conversation marked as a consultant's, and later speakers can use it.
            <br />
            <br />
            A consultant never takes a turn and has no position to defend. Each consultation is a model
            call, so the room can ask only a limited number per run.
            <br />
            <br />
            With "Research the subject before starting" on, each consultant also gets a library
            searched from the web for its expertise, stored in its own knowledge base.
          </Hint>
        </h2>
        <button
          onClick={() => onChange([...consultants, blankConsultant()])}
          disabled={consultants.length >= 5}
          className="rounded border border-matrix-border px-2 py-1 text-xs hover:border-matrix-accent disabled:opacity-50"
        >
          + Add consultant
        </button>
      </div>
      {consultants.length === 0 ? (
        <p className="text-xs text-slate-500">None. Personas argue from what they already have.</p>
      ) : (
        <div className="space-y-3">
          {consultants.map((c, i) => (
            <div key={i} className="space-y-2 rounded border border-matrix-border p-3">
              <div className="flex gap-2">
                {/* A consultant is simulated too. The input cannot hold the marker, so it sits beside it. */}
                <BotMark className="cc-botmark-field" />
                <input
                  value={c.name}
                  // Editing the name drops the notice: it was about the name that is no longer there.
                  onChange={(e) => update(i, { name: e.target.value, renamed: undefined })}
                  onFocus={() => onNameFocus?.(i)}
                  onBlur={() => onNameFocus?.(null)}
                  placeholder="Consultant name"
                  maxLength={60}
                  className={`w-44 ${input}`}
                />
                <input
                  value={c.expertise}
                  onChange={(e) => update(i, { expertise: e.target.value })}
                  placeholder="Expertise, e.g. state building codes"
                  maxLength={300}
                  className={`min-w-0 flex-1 ${input}`}
                />
                <button
                  onClick={() => onChange(consultants.filter((_, n) => n !== i))}
                  className="px-2 text-slate-500 hover:text-rose-300"
                  aria-label={`Remove consultant ${c.name || i + 1}`}
                >
                  ✕
                </button>
              </div>
              {showsRenamed(c) && <RenamedNotice renamed={c.renamed!} kind="consultant" />}
              <KbPicker
                level="persona"
                personaName={c.name || 'this consultant'}
                selected={c.knowledgeBases}
                onChange={(ids) => update(i, { knowledgeBases: ids })}
              />
              <details className="text-xs text-slate-400">
                <summary className="cursor-pointer hover:text-slate-200">
                  Paste a source document {c.docText.trim() ? '(1 attached)' : ''}
                </summary>
                <input
                  value={c.docTitle}
                  onChange={(e) => update(i, { docTitle: e.target.value })}
                  placeholder="Title"
                  className={`mt-2 w-full ${input}`}
                />
                <textarea
                  value={c.docText}
                  onChange={(e) => update(i, { docText: e.target.value })}
                  placeholder="Text the consultant answers from"
                  rows={4}
                  className={`mt-2 w-full ${input}`}
                />
              </details>
            </div>
          ))}
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Consultations allowed per run
            <input
              type="number"
              min={0}
              max={20}
              value={limit}
              onChange={(e) => onLimit(Math.max(0, Math.min(20, Number(e.target.value) || 0)))}
              className="w-16 rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-sm"
            />
          </label>
        </div>
      )}
    </div>
  )
}
