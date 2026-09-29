// SPDX-License-Identifier: Apache-2.0
import { Hint } from './Hint'

export interface DraftInjection {
  afterTurn: number
  speaker: string
  content: string
}

export const MAX_INJECTIONS = 5

/** The messages as `config.injections` takes them: complete ones only. */
export function injectionsConfig(list: DraftInjection[]) {
  return list
    .filter((i) => i.speaker.trim() && i.content.trim())
    .map((i) => ({ after_turn: Math.max(0, Math.floor(i.afterTurn)), speaker: i.speaker.trim(), content: i.content.trim() }))
}

interface Props {
  injections: DraftInjection[]
  onChange: (list: DraftInjection[]) => void
  maxTurns: number
}

// Scheduled operator messages (matrix_studio/injections.py): something the room hears at a fixed turn.
export function InjectionsEditor({ injections, onChange, maxTurns }: Props) {
  const update = (i: number, patch: Partial<DraftInjection>) =>
    onChange(injections.map((x, n) => (n === i ? { ...x, ...patch } : x)))
  const input = 'rounded border border-matrix-border bg-matrix-bg p-2 text-sm'
  return (
    <div className="mt-6 rounded-lg border border-matrix-border bg-matrix-panel p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="flex items-center gap-1 text-sm font-semibold text-slate-300">
          Scheduled messages
          <Hint label="scheduled messages">
            A message that enters the conversation after a given turn — a customer complaint, a
            regulator's letter, new numbers. It is shown as injected by you, never as something a
            persona chose to say, and it does not use up a turn.
            <br />
            <br />
            To change one conversation after the fact, branch it from the scrubber instead. This is
            for the same message at the same point in a fresh run — and, in an ensemble, for
            comparing runs with and without it, which is the only way to tell what it changed.
          </Hint>
        </h2>
        <button
          onClick={() => onChange([...injections, { afterTurn: Math.min(5, Math.max(0, maxTurns - 1)), speaker: '', content: '' }])}
          disabled={injections.length >= MAX_INJECTIONS}
          className="rounded border border-matrix-border px-2 py-1 text-xs hover:border-matrix-accent disabled:opacity-50"
        >
          + Add message
        </button>
      </div>
      {injections.length === 0 ? (
        <p className="text-xs text-slate-500">None. The room hears only itself.</p>
      ) : (
        <div className="space-y-2">
          {injections.map((x, i) => (
            <div key={i} className="space-y-1 rounded border border-matrix-border p-2">
              <div className="flex items-center gap-2 text-xs text-slate-400">
                After turn
                <input
                  type="number"
                  min={0}
                  max={Math.max(0, maxTurns - 1)}
                  value={x.afterTurn}
                  aria-label={`Message ${i + 1} after turn`}
                  onChange={(e) => update(i, { afterTurn: Math.max(0, Math.min(maxTurns - 1, Number(e.target.value) || 0)) })}
                  className="w-16 rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-sm"
                />
                <input
                  value={x.speaker}
                  onChange={(e) => update(i, { speaker: e.target.value })}
                  placeholder="From, e.g. Customer"
                  maxLength={60}
                  aria-label={`Message ${i + 1} from`}
                  className={`w-44 ${input}`}
                />
                <button
                  onClick={() => onChange(injections.filter((_, n) => n !== i))}
                  className="ml-auto px-2 text-slate-500 hover:text-rose-300"
                  aria-label={`Remove message ${i + 1}`}
                >
                  ✕
                </button>
              </div>
              <textarea
                value={x.content}
                onChange={(e) => update(i, { content: e.target.value })}
                placeholder="What they say"
                rows={2}
                maxLength={2000}
                aria-label={`Message ${i + 1} text`}
                className={`w-full ${input}`}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
