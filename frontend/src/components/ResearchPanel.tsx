// SPDX-License-Identifier: Apache-2.0
import type { ResearchRecord } from '../types'

interface Props {
  research: ResearchRecord | null
}

// `docs/PERSONA-RESEARCH.md` §5.3. Nobody watches the research happen — that was the whole
// point of putting it in a state before turn 1 — so this is where an operator finds out what
// it did.
//
// **Three outcomes have to be visually distinct, not merged into "no results".** §5.2: a run
// that researched and found nothing is an HONEST run; one whose provider was unreachable is a
// run to retry; one with no search key configured is a deployment to fix. Collapsing them into
// one empty state would hide the only distinction that tells an operator what to do next.
//
// Counts only, because that is all the run row holds — the passages themselves live in the
// knowledge bases, which the KB view already lists. Duplicating them here would have put a
// 400 KB item limit between a run and a large research pass.

const STATUS: Record<string, { label: string; tone: string; detail: string }> = {
  researched: {
    label: 'Researched',
    tone: 'text-emerald-400',
    detail: 'Sources were found, stored and embedded before turn 1.',
  },
  'found-nothing': {
    label: 'Researched — nothing found',
    tone: 'text-amber-400',
    detail:
      'The search ran and returned no usable source. This is a real answer about the ' +
      'subject rather than a failure, and it was recorded as a finding the conversation ' +
      'can retrieve — so the room can tell "nobody looked" from "we looked and there is ' +
      'nothing".',
  },
  unavailable: {
    label: 'Unavailable',
    tone: 'text-amber-400',
    detail:
      'No search provider is configured for this deployment, so nothing was searched. ' +
      'The conversation ran anyway. This is fixed by configuration, not by retrying.',
  },
  failed: {
    label: 'Failed',
    tone: 'text-rose-400',
    detail:
      'The research pass did not complete. The conversation ran regardless — research is ' +
      'additive and never fails a run.',
  },
  skipped: {
    label: 'Skipped',
    tone: 'text-slate-400',
    detail: 'No research was attempted for this run.',
  },
}

export function ResearchPanel({ research }: Props) {
  // Absent for every run that did not ask, which is every run created before the feature
  // existed. Rendering an empty panel on those would add a section to every conversation in
  // the system to say that nothing happened.
  if (!research) return null

  const status = STATUS[research.status] ?? {
    label: research.status,
    tone: 'text-slate-400',
    detail: '',
  }
  const scopes = research.scopes ?? []
  const controlling = scopes.reduce((n, s) => n + (s.controlling ?? 0), 0)
  const documents = scopes.reduce((n, s) => n + (s.documents ?? 0), 0)
  const embedded = scopes.reduce((n, s) => n + (s.embedded ?? 0), 0)
  // Both kinds: a corpus that found no controlling authority at all, and — §9.3 — each search that
  // found none inside a corpus that found some. Counted as documents, which is what reaches a prompt.
  const negatives = scopes.reduce(
    (n, s) => n + (s.negative ? 1 : 0) + (s.query_negatives ?? 0),
    0,
  )

  return (
    <div className="rounded-lg border border-matrix-border bg-matrix-panel p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold text-slate-300">Pre-conversation research</h2>
        <span className={`text-sm font-semibold ${status.tone}`}>{status.label}</span>
        {research.provider && (
          <span className="text-xs text-slate-500">via {research.provider}</span>
        )}
        {typeof research.cost_usd === 'number' && (
          <span className="text-xs text-slate-500">${research.cost_usd.toFixed(4)}</span>
        )}
      </div>

      {status.detail && <p className="mt-1 text-xs text-slate-400">{status.detail}</p>}

      {/* The error is shown for every non-success status, including `skipped`, because the
          reason is the useful part: "an ensemble researches once" and "research is not enabled"
          are both skips and mean entirely different things to whoever is reading. */}
      {research.error && (
        <p className="mt-2 rounded border border-matrix-border bg-matrix-bg p-2 text-xs text-slate-400">
          {research.error}
        </p>
      )}

      {scopes.length > 0 && (
        <>
          <div className="mt-3 flex flex-wrap gap-4 text-xs text-slate-400">
            <span>
              <strong className="text-slate-200">{documents}</strong> sources
            </span>
            <span>
              {/* Called out on its own because it is the reservation the retrieval floor acts
                  on. A pass that found 90 sources and no controlling authority did not answer
                  the question the personas were asking. */}
              <strong className={controlling > 0 ? 'text-emerald-400' : 'text-amber-400'}>
                {controlling}
              </strong>{' '}
              controlling
            </span>
            <span>
              <strong className="text-slate-200">{embedded}</strong> passages embedded
            </span>
            {negatives > 0 && (
              <span>
                <strong className="text-slate-200">{negatives}</strong> documented negative
                {negatives === 1 ? '' : 's'}
              </span>
            )}
          </div>

          <table className="mt-3 w-full text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1 pr-3 font-normal">Scope</th>
                <th className="py-1 pr-3 text-right font-normal">Queries</th>
                <th className="py-1 pr-3 text-right font-normal">Sources</th>
                <th className="py-1 pr-3 text-right font-normal">Controlling</th>
                <th className="py-1 pr-3 text-right font-normal">Unreadable</th>
                <th className="py-1 pr-3 text-right font-normal">Embedded</th>
                <th className="py-1 font-normal">Collection</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {scopes.map((s) => (
                <tr key={`${s.consultant ? 'c:' : ''}${s.scope}`} className="border-t border-matrix-border/50">
                  <td className="py-1 pr-3">
                    {s.scope === 'shared' ? (
                      <span className="text-slate-200">shared (the researcher)</span>
                    ) : s.consultant ? (
                      `${s.scope} (consultant)`
                    ) : (
                      s.scope
                    )}
                    {(s.negative || (s.query_negatives ?? 0) > 0) && (
                      <span
                        className="ml-1 text-amber-400"
                        title="No controlling authority was found, and that absence was recorded as a finding the conversation can retrieve."
                      >
                        ◆
                      </span>
                    )}
                  </td>
                  <td className="py-1 pr-3 text-right">{s.queries ?? 0}</td>
                  <td className="py-1 pr-3 text-right">{s.documents ?? 0}</td>
                  <td
                    className={`py-1 pr-3 text-right ${
                      (s.controlling ?? 0) > 0 ? 'text-emerald-400' : 'text-slate-500'
                    }`}
                  >
                    {s.controlling ?? 0}
                  </td>
                  {/* Unreadable is not a bug count. A state board's own statute page
                      answering 403 is a fact about the search, and it is reported so an
                      absence is never mistaken for "there is nothing there". */}
                  <td className="py-1 pr-3 text-right text-slate-500">{s.unreadable ?? 0}</td>
                  <td className="py-1 pr-3 text-right">
                    {/* An embed error means the documents ARE stored and no turn can see
                        them. That is the one failure here worth shouting about, because
                        every other symptom of it looks like a bad researcher. */}
                    {s.embed_error ? (
                      <span className="text-rose-400" title={s.embed_error}>
                        not retrievable
                      </span>
                    ) : (
                      (s.embedded ?? 0)
                    )}
                  </td>
                  <td className="py-1 font-mono text-slate-500">
                    {s.refused ? (
                      <span className="text-rose-400" title={s.refused}>
                        refused
                      </span>
                    ) : (
                      (s.kb_id ?? '—')
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="mt-2 text-xs text-slate-500">
            The sources themselves are in the collections above — open one from Knowledge
            bases to read them. Research documents are marked as found rather than uploaded,
            so a pass can be undone without touching anything you curated by hand.
          </p>
        </>
      )}
    </div>
  )
}
