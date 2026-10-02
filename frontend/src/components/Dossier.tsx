// SPDX-License-Identifier: Apache-2.0
// One persona's dossier (docs/MOBILE-UI.md §4.4): a HUD of turns, stance and firmness over four tabs.
// Convictions is what they were seeded with, Memory what they said, formed and read, Threads what they left
// open and how they regard the others, Why? each message with its per-turn trace.
//
// Everything is read from what the run recorded, and a tab says so when something was not recorded rather
// than leaving a blank that reads as "nothing".
import './Dossier.css'
import { useEffect, useId, useState, type KeyboardEvent } from 'react'
import { api, avatarUrl } from '../api'
import type {
  AgentDossier, AgentView, DossierThread, FeedMessage, StanceBasisEntry, StructuredViewpoint, TurnTrace,
} from '../types'
import { FIRMNESS } from '../lib/convictions'
import { AvatarBadge } from './AvatarBadge'
import { SourceViewer } from './SourceViewer'
import { STANCE_CLASS, StanceWhy } from './run/Stance'
import { HudCell, HudStrip, Label, Panel, STANCE_LABEL, Sheet, Tag, type Stance } from '../ui/primitives'
import { Hint } from './Hint'

const pad = (n: number) => String(n).padStart(2, '0')
const clip = (s: string, n = 140) => (s.length > n ? `${s.slice(0, n).trimEnd()}…` : s)

type Tab = 'convictions' | 'memory' | 'threads' | 'why'
const TABS: { id: Tab; label: string }[] = [
  { id: 'convictions', label: 'Convictions' },
  { id: 'memory', label: 'Memory' },
  { id: 'threads', label: 'Threads' },
  { id: 'why', label: 'Why?' },
]

interface Props {
  agent: AgentView
  feed: FeedMessage[]
  runId: string
  /** Where they ended (`matrix_studio/stance.py`). Post-run only, so absent while a run is live. */
  stance?: Stance
  /** Why: the source that decided it and the words it rests on. Absent for runs summarised before 2026-10-01. */
  basis?: StanceBasisEntry
  onClose: () => void
}

export function Dossier({ agent, feed, runId, stance, basis, onClose }: Props) {
  const messages = feed.filter((m) => m.speaker === agent.name)
  // A message the operator put in their mouth is not a turn they took, so the HUD counts as the room map does.
  const turns = messages.filter((m) => !m.injected && !m.consultant).length
  const moves = messages.filter((m) => m.shift)
  const [tab, setTab] = useState<Tab>('convictions')
  const uid = useId()
  const tabId = (t: Tab) => `${uid}-tab-${t}`
  // The source a reader has opened from the passages list, if any.
  const [openSource, setOpenSource] = useState<{ documentId: string; ordinal: number } | null>(null)
  const [dossier, setDossier] = useState<AgentDossier | null>(null)
  const [loaded, setLoaded] = useState(false)
  // Without this a failed fetch read as a run with no cognition and no structured persona: two claims about
  // the run that nothing had checked.
  const [failed, setFailed] = useState(false)
  const [regeneratingAvatar, setRegeneratingAvatar] = useState(false)
  // Regeneration replaces the image, so the URL is held in state rather than read
  // from the agent. The key is content-addressed, so a new portrait yields a new URL
  // and the browser cannot serve the old one from cache.
  const [currentUrl, setCurrentUrl] = useState(agent.portraitUrl)

  useEffect(() => {
    let alive = true
    api
      .getDossier(runId, agent.name)
      .then((d) => alive && setDossier(d))
      .catch(() => alive && setFailed(true))
      .finally(() => alive && setLoaded(true))
    return () => {
      alive = false
    }
  }, [runId, agent.name])

  const handleRegenerateAvatar = async () => {
    setRegeneratingAvatar(true)
    try {
      const result = await api.regenerateAvatar(runId, agent.name)
      const url = avatarUrl(runId, agent.name, result.portrait_key)
      setCurrentUrl(url)
      // Keep the shared agent object in step so the cast board updates too.
      agent.portraitKey = result.portrait_key
      agent.portraitUrl = url
    } catch (error) {
      console.error('Failed to regenerate avatar:', error)
      alert('Failed to regenerate avatar. Please try again.')
    } finally {
      setRegeneratingAvatar(false)
    }
  }

  // Cognition is "captured" only if the engine actually produced any of it.
  const hasCognition =
    !!dossier &&
    (dossier.memory_stream.length > 0 ||
      dossier.beliefs.length > 0 ||
      Object.keys(dossier.relationships).length > 0)

  // Whether it was ASKED FOR is a different question, and conflating the two made this
  // panel state a falsehood: it told an operator a run "was created without cognition"
  // when cognition was on and its output was being discarded by a strict JSON parse.
  // `undefined` from an older backend keeps the old wording, which was right for the
  // only case that backend could produce.
  const cognitionConfigured = dossier?.cognition_enabled ?? false
  const cognitionKnown = dossier?.cognition_enabled !== undefined
  const lostTurns = dossier?.cognition_lost_turns ?? 0
  // Shown on Memory and on Why?, the two tabs that are empty without cognition. Only one tab renders at a time.
  const cognitionNote = !loaded
    ? 'Loading…'
    : failed
      ? 'The dossier could not be loaded, so nothing here says whether this run captured cognition.'
      : cognitionKnown && cognitionConfigured
        ? lostTurns > 0
          ? `Cognition was enabled for this run, but ${lostTurns} of this persona's turns returned a structured reply that could not be read, so their memories, reflections and “why” trace were lost. The transcript is unaffected.`
          : 'Cognition was enabled for this run, but this persona did not form any memories, reflections or relationship updates — which is the expected result for a persona who spoke only once or twice.'
        : 'This run was created without cognition, so there is no memory stream, reflections, relationships, or per-turn “why” trace to show. Enable Cognition when creating a run to capture it.'

  // Phase 5. Defaulted because an older backend omits these fields entirely,
  // and retrieval is opt-in, so absent is the normal case rather than an error.
  const docs = dossier?.documents ?? []
  const kbs = dossier?.knowledge_bases ?? []
  const retrievals = dossier?.document_retrievals ?? []
  const retrievedChars = retrievals.reduce((sum, r) => sum + (r.total_chars ?? 0), 0)

  // Phase 4b: only the threads this persona opened, as the backend already filters them.
  const threads = dossier?.pending_threads ?? []
  const openThreads = threads.filter((t) => t.status === 'open').length

  // Phase 6. Null for a run that used no structured personas, which is the normal
  // case since the feature is off by default.
  const structured = dossier?.structured ?? null
  const viewpoints = structured?.viewpoints ?? []
  const prefs = structured?.preferences
  const formative = structured?.background?.formative_events ?? []
  // The firmest position stands for the persona in the HUD: it decides whether they can be argued round at
  // all. `FIRMNESS` runs weakest to strongest, as `matrix_studio/personas.py` does. The real level is shown
  // rather than a soft/firm/fixed gloss, because `requires-escalation` is about authority, not degree.
  const firmest = viewpoints.reduce<StructuredViewpoint['firmness'] | null>(
    (top, vp) => (top === null || FIRMNESS.indexOf(vp.firmness) > FIRMNESS.indexOf(top) ? vp.firmness : top),
    null,
  )

  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = TABS.findIndex((t) => t.id === tab)
    const to =
      e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? -1 : null
    if (to === null) return
    e.preventDefault()
    const next = TABS[(to + TABS.length) % TABS.length].id
    setTab(next)
    document.getElementById(tabId(next))?.focus()
  }

  const convictions = (
    <>
      {/* Where they ended, and on what: the first thing a reader asks of a stance is "says who?". */}
      {stance && basis && (
        <Panel>
          <div className="flex items-center justify-between gap-2">
            <Label>Where they ended</Label>
            <span className={`cc-sm ${STANCE_CLASS[stance]}`}>{STANCE_LABEL[stance]}</span>
          </div>
          <p className="mt-1.5">
            <StanceWhy entry={basis} />
          </p>
        </Panel>
      )}

      {moves.map((m) => (
        <div key={m.seq} className="cc-flag cc-shift">
          <span className="cc-ft">MOVED @{pad(m.turn)}</span>
          <span className="min-w-0 flex-1">
            Said their position moved; credits{' '}
            {m.shift!.credits.length ? m.shift!.credits.map((c) => c.name).join(', ') : 'nobody named'}.
            {m.shift!.no_listed_condition && <b> None of the conditions they had named appears.</b>}
            <Hint label="moved flags">Found by matching words, not by judgement. Read the message and decide.</Hint>
          </span>
        </div>
      ))}

      {/* Phase 6: the convictions this persona was seeded with.
          Rendered ONLY from what the dossier API returns, which deliberately
          omits `underlying_concern` and `validity`.

          `underlying_concern` must never appear here even if a future backend
          regression started sending it: the whole design is that the real worry
          behind a position gets DRAWN OUT in conversation, and an operator who
          can read it off a panel has been handed the answer. The type does not
          declare those fields, so rendering them would not compile — that is the
          guard, and Dossier.test.tsx asserts it against a payload that includes
          them anyway. */}
      {structured &&
        (viewpoints.length > 0 ? (
          viewpoints.map((vp, i) => {
            const defended = vp.firmness !== 'negotiable'
            const shifts = vp.evidence_that_shifts ?? []
            return (
              <Panel key={i}>
                <div className="flex items-center justify-between gap-2">
                  <Label>{viewpoints.length > 1 ? `Position ${i + 1}` : 'Position'}</Label>
                  <Tag tone={defended ? 'ok' : undefined}>{vp.firmness}</Tag>
                </div>
                <p className="cc-big cc-dz-pos">{vp.position}</p>
                {vp.formed_by && (
                  <div className="cc-flag cc-dz-formed">
                    <span className="cc-ft">FORMED BY</span>
                    <span>{vp.formed_by}</span>
                  </div>
                )}
                {shifts.length > 0 ? (
                  <div className="cc-flag cc-dz-move">
                    <span className="cc-ft">WOULD MOVE</span>
                    <span>{shifts.join('; ')}</span>
                  </div>
                ) : (
                  defended && (
                    // An authoring gap worth surfacing rather than hiding: a
                    // defended position with no exit condition is unfalsifiable,
                    // and the operator is the only one who can fix it.
                    <div className="cc-flag cc-lean">
                      <span className="cc-ft">NO EXIT</span>
                      <span>no exit condition named — this position cannot be moved by evidence</span>
                    </div>
                  )
                )}
              </Panel>
            )
          })
        ) : (
          <Panel>
            <p className="cc-muted">No positions were authored for this persona.</p>
          </Panel>
        ))}

      {prefs && (prefs.optimises_for?.length || prefs.dismisses?.length || prefs.persuaded_by?.length) ? (
        <Panel>
          <dl className="cc-dz-dl">
            {!!prefs.dismisses?.length && (
              <div>
                <dt className="cc-label">Will not weigh</dt>
                <dd>{prefs.dismisses.join('; ')}</dd>
              </div>
            )}
            {!!prefs.optimises_for?.length && (
              <div>
                <dt className="cc-label">Optimises for</dt>
                <dd>{prefs.optimises_for.join('; ')}</dd>
              </div>
            )}
            {!!prefs.persuaded_by?.length && (
              <div>
                <dt className="cc-label">Persuaded by</dt>
                <dd>{prefs.persuaded_by.join('; ')}</dd>
              </div>
            )}
          </dl>
        </Panel>
      ) : null}

      {/* The note carries no text from the concern, and cannot: the API strips the field, so the dossier is
          not even told whether one was written. It is phrased for both cases for that reason. The tag says
          Hidden, not "not drawn out", because nothing detects a reveal (MOBILE-UI.md §4.4). */}
      {viewpoints.length > 0 && (
        <Panel edge="stopped">
          <div className="flex items-center justify-between gap-2">
            <Label>Withheld concern</Label>
            <Tag tone="warn">Hidden</Tag>
          </div>
          <p className="cc-sm cc-t2 mt-1.5">
            If a concern was authored behind these positions, it is not shown here, and the dossier is not told
            whether one was. Drawing it out in conversation is the exercise: an operator who can read it off a
            panel has been handed the answer (docs/project/PHASE6-STRUCTURED-PERSONAS.md §1).
          </p>
        </Panel>
      )}

      {formative.length > 0 && (
        <Panel>
          <Label>Formative events</Label>
          <ul className="cc-plain">
            {formative.map((ev, i) => (
              <li key={i}>
                <span className="cc-num">{ev.year ? `${ev.year}: ` : ''}</span>
                {ev.event}
                {ev.lesson && <span className="cc-muted"> → {ev.lesson}</span>}
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel>
        <Label>Persona</Label>
        <p className="cc-sm cc-t2 mt-1.5 whitespace-pre-wrap">{agent.persona || '—'}</p>
      </Panel>

      <Panel>
        <Label>Goals</Label>
        {(dossier?.goals ?? agent.goals).length ? (
          <ul className="cc-plain">
            {(dossier?.goals ?? agent.goals).map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        ) : (
          <p className="cc-muted mt-1.5">No goals specified.</p>
        )}
      </Panel>
    </>
  )

  const latest = [...messages].reverse()
  const memory = (
    <>
      <Panel>
        <Label>Last said</Label>
        {latest.length ? (
          <ul className="cc-plain">
            {latest.slice(0, 3).map((m) => (
              <li key={m.seq}>
                <span className="cc-num cc-muted">
                  #{pad(m.turn)}
                  {m.injected && ' · injected'}{' '}
                </span>
                “{clip(m.content)}”
              </li>
            ))}
          </ul>
        ) : (
          <p className="cc-muted mt-1.5">This agent hasn't spoken yet.</p>
        )}
      </Panel>

      {/* Phase 2c: real captured cognition when present; an honest
          "not captured for this run" state otherwise — never fabricated. */}
      {hasCognition && dossier ? (
        <>
          <Panel>
            <Label>{`Memory stream (${dossier.memory_stream.length})`}</Label>
            {dossier.memory_stream.length ? (
              <ul className="cc-plain">
                {dossier.memory_stream.map((mem) => (
                  <li key={mem.id}>
                    {mem.content}
                    <span className="cc-dz-meta">
                      {(mem.tags || []).join(', ') || 'memory'}
                      {mem.importance != null && ` · importance ${mem.importance.toFixed(2)}`}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="cc-muted mt-1.5">No memories formed.</p>
            )}
          </Panel>

          {dossier.beliefs.length > 0 && (
            <Panel>
              <Label>{`Beliefs / reflections (${dossier.beliefs.length})`}</Label>
              <ul className="cc-plain">
                {dossier.beliefs.map((b) => (
                  <li key={b.id}>{b.content}</li>
                ))}
              </ul>
            </Panel>
          )}
        </>
      ) : (
        <Panel>
          <Label>Cognition</Label>
          <p className="cc-muted mt-1.5">{cognitionNote}</p>
        </Panel>
      )}

      {/* Phase 5: background documents this persona can draw on, and the
          passages it actually did draw on. Both come straight from stored
          state and the document.retrieved audit events — a run without
          retrieval renders nothing here rather than an empty promise. */}
      {retrievals.length > 0 && (
        <Panel>
          <Label>{`Passages drawn on (${retrievedChars.toLocaleString()} chars)`}</Label>
          {retrievals.map((r) => (
            <div key={r.turn} className="cc-dz-turn">
              <div className="cc-dz-meta">
                turn {r.turn} · {r.total_chars?.toLocaleString() ?? 0} chars
                {/* PERSONA-RESEARCH.md §5.1, and the reason this line exists at all: the
                    retrieval floor guarantees a slot per COLLECTION, not per kind of
                    thing in one. Once research writes into a collection somebody curated,
                    their own document competes with the searcher's finds — and on run
                    602ddffe a persona's hand-picked source material lost all three slots.
                    By the floor's accounting nothing went wrong, which is exactly why it
                    has to be said out loud rather than counted by a reader. */}
                {r.researched_passages !== undefined && (
                  <>
                    {' · '}
                    <span className={r.researched_passages === r.passages.length ? 'text-cc-inject' : undefined}>
                      {r.researched_passages} of {r.passages.length} researched
                    </span>
                    <Hint label="researched passages">
                      {r.researched_passages === r.passages.length
                        ? 'Every passage this turn was found by research. Nothing you ' +
                          'uploaded reached this prompt — a collection is guaranteed one ' +
                          'slot, but not one slot per kind of document in it.'
                        : 'Some passages were found by research, some chosen by you.'}
                    </Hint>
                  </>
                )}
              </div>
              <ul className="cc-dz-passages">
                {r.passages.map((p) => (
                  <li key={p.chunk_id}>
                    {/* Opens the source itself, so a reader can check what the persona was
                        actually given rather than trust a title and a score. */}
                    <button
                      type="button"
                      onClick={() => setOpenSource({ documentId: p.document_id, ordinal: p.ordinal })}
                      className="cc-dz-src"
                      aria-description="Read this source"
                    >
                      {p.title} #{p.ordinal}
                    </button>{' '}
                    {/* Controlling authority is called out because it is the tier the
                        floor reserves a slot for: seeing it here is how an operator knows
                        the reservation did something. */}
                    {p.authority === 'controlling' && (
                      <span className="text-emerald-400">
                        [controlling]
                        <span className="sr-only"> a statute, regulation, board ruling or decided case</span>{' '}
                      </span>
                    )}
                    {/* Only when this turn actually mixed the two. A run with no research
                        has nothing to distinguish, so tagging every passage `[yours]`
                        would add a badge to every conversation in the system to say
                        nothing — and the point is to make a DIFFERENCE visible. */}
                    {r.researched_passages !== undefined &&
                      (p.origin === 'researched' ? (
                        <span className="cc-muted">
                          [found]<span className="sr-only"> by pre-conversation research</span>{' '}
                        </span>
                      ) : (
                        <span className="text-sky-400">
                          [yours]<span className="sr-only"> a document you provided</span>{' '}
                        </span>
                      ))}
                    <span className="cc-num cc-muted">score {p.score.toFixed(2)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </Panel>
      )}

      {docs.length > 0 && (
        <Panel>
          <Label>{`Background documents (${docs.length})`}</Label>
          <ul className="cc-plain">
            {docs.map((doc) => (
              <li key={doc.document_id}>
                <b className="cc-dz-t1">{doc.title}</b>
                {doc.cast_wide && <span className="cc-muted"> · whole cast</span>}
                <span className="cc-dz-meta">
                  {doc.chunk_count} chunk{doc.chunk_count === 1 ? '' : 's'} · {doc.char_count.toLocaleString()} chars
                  {doc.media_type ? ` · ${doc.media_type}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {/* The collections it searches. Without these a knowledge-base-bound persona showed no
          background material at all beside passages it had plainly read. */}
      {kbs.length > 0 && (
        <Panel>
          <Label>{`Knowledge bases searched (${kbs.length})`}</Label>
          <ul className="cc-plain">
            {kbs.map((kb) => (
              <li key={kb.id} className="flex items-baseline justify-between gap-2">
                <span className={kb.readable ? 'cc-dz-t1' : 'italic'}>
                  {kb.readable ? kb.name : 'A collection you can no longer read'}
                </span>
                <span className="cc-dz-meta">{kb.scope === 'run' ? 'whole cast' : 'this persona'}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <HudStrip>
        <HudCell label="Messages" value={String(agent.messageCount)} />
        <HudCell
          label="Tokens"
          value={(agent.tokensIn + agent.tokensOut).toLocaleString()}
          sub={`${agent.tokensIn.toLocaleString()} in · ${agent.tokensOut.toLocaleString()} out`}
        />
        <HudCell label="Cost" value={`$${agent.costUsd.toFixed(4)}`} />
      </HudStrip>
    </>
  )

  const threadsTab = (
    <>
      <Panel>
        <div className="flex items-center justify-between gap-2">
          <Label>{`Pending threads (${openThreads} open)`}</Label>
          <Hint label="pending threads">
            A setup, promise or deferred consequence this persona opened, which the run keeps putting back in
            front of the room until someone pays it off. Dangling means still open long after it was raised.
          </Hint>
        </div>
        {threads.length ? (
          <ul className="cc-plain">
            {threads.map((t) => (
              <li key={t.id}>
                {t.description}
                <span className="cc-dz-meta">
                  #{pad(t.origin_turn)} · {t.thread_type.replace(/-/g, ' ')} · {threadState(t)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="cc-muted mt-1.5">
            {!loaded
              ? 'Loading…'
              : failed
                ? 'The dossier could not be loaded.'
                : cognitionKnown && !cognitionConfigured
                  ? 'Threads are tracked only when a run has cognition on, and this one did not.'
                  : 'No threads opened by this persona.'}
          </p>
        )}
      </Panel>

      {dossier && Object.keys(dossier.relationships).length > 0 && (
        <Panel>
          <Label>Relationships</Label>
          <ul className="cc-plain">
            {Object.entries(dossier.relationships).map(([other, view]) => (
              <li key={other}>
                <b className="cc-dz-t1">{other}:</b> <span>{view}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </>
  )

  const why = (
    <>
      {!hasCognition && (
        <Panel>
          <Label>Cognition</Label>
          <p className="cc-muted mt-1.5">{cognitionNote}</p>
        </Panel>
      )}
      <Panel>
        <Label>{`Messages (${messages.length})`}</Label>
        {latest.length ? (
          <div className="mt-2 flex flex-col gap-2">
            {latest.map((m) => (
              <MessageRow key={m.seq} m={m} runId={runId} traceable={hasCognition} />
            ))}
          </div>
        ) : (
          <p className="cc-muted mt-1.5">This agent hasn't spoken yet.</p>
        )}
      </Panel>
    </>
  )

  return (
    <>
      {openSource && (
        <SourceViewer
          runId={runId}
          documentId={openSource.documentId}
          ordinal={openSource.ordinal}
          onClose={() => setOpenSource(null)}
        />
      )}
      <Sheet
        tall
        title={`${agent.name}: dossier`}
        onClose={onClose}
        header={
          <div className="flex items-center gap-3">
            <AvatarBadge name={agent.name} portrait={agent.portrait} portraitUrl={currentUrl} size={56} />
            <div className="min-w-0">
              <b className="cc-disp">{agent.name}</b>
              {structured?.role && <p className="cc-sm cc-t2">{structured.role}</p>}
              <p className="cc-muted">
                {agent.avatarResolved
                  ? currentUrl || agent.portrait
                    ? 'portrait generated'
                    : 'placeholder (avatar unavailable)'
                  : 'avatar pending…'}
              </p>
              <button
                type="button"
                onClick={handleRegenerateAvatar}
                disabled={regeneratingAvatar}
                className="cc-btn cc-sm cc-dz-tap mt-1"
              >
                {regeneratingAvatar ? 'Generating...' : 'Regenerate avatar'}
              </button>
            </div>
          </div>
        }
      >
        <div className="cc-dz">
          <HudStrip>
            <HudCell label="Turns" value={pad(turns)} />
            <HudCell
              label="Stance"
              value={
                <span className={`cc-dz-word ${stance ? STANCE_CLASS[stance] : 'cc-muted'}`}>
                  {stance ? STANCE_LABEL[stance] : 'not yet'}
                </span>
              }
              sub={
                !stance
                  ? 'once summarised'
                  : basis
                    ? basis.source === 'closing'
                      ? 'from closing statement'
                      : 'from summary'
                    : undefined
              }
            />
            <HudCell
              label="Firmness"
              value={<span className="cc-dz-word">{firmest ?? '—'}</span>}
              sub={
                !loaded
                  ? 'loading'
                  : failed
                    ? 'not loaded'
                    : !structured
                      ? 'no structured persona'
                      : viewpoints.length === 0
                        ? 'no positions'
                        : viewpoints.length > 1
                          ? `firmest of ${viewpoints.length}`
                          : undefined
              }
            />
          </HudStrip>

          <div className="cc-htabs" role="tablist" aria-label="Dossier sections">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                id={tabId(t.id)}
                aria-selected={tab === t.id}
                aria-controls={`${uid}-panel`}
                tabIndex={tab === t.id ? 0 : -1}
                className={tab === t.id ? 'cc-on' : undefined}
                onClick={() => setTab(t.id)}
                onKeyDown={onTabKey}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* Keyed by tab so a switch starts at the top, not part-way down the last tab's scroll. */}
          <div
            key={tab}
            role="tabpanel"
            id={`${uid}-panel`}
            aria-labelledby={tabId(tab)}
            tabIndex={0}
            className="cc-dz-panel"
          >
            {tab === 'convictions'
              ? convictions
              : tab === 'memory'
                ? memory
                : tab === 'threads'
                  ? threadsTab
                  : why}
          </div>
        </div>
      </Sheet>
    </>
  )
}

// In words, so state never rests on colour.
function threadState(t: DossierThread) {
  if (t.status === 'open') return t.stale ? 'open, dangling' : 'open'
  return t.resolved_turn != null ? `${t.status} @${pad(t.resolved_turn)}` : t.status
}

function MessageRow({ m, runId, traceable }: { m: FeedMessage; runId: string; traceable: boolean }) {
  const [open, setOpen] = useState(false)
  const [trace, setTrace] = useState<TurnTrace | null>(null)
  const [loading, setLoading] = useState(false)

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && trace === null && !loading) {
      setLoading(true)
      try {
        setTrace(await api.getTurnTrace(runId, m.turn))
      } catch {
        setTrace({ run_id: runId, turn: m.turn, available: false })
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <div className="cc-dz-msg">
      <div className="flex items-start justify-between gap-2">
        <div className="cc-sm min-w-0">
          {/* Marked as the feed marks it: words the operator put in their mouth are not theirs. */}
          <span className="cc-num cc-muted mr-2">
            turn {m.turn}
            {m.injected && ' · injected'}
          </span>
          {m.content}
        </div>
        {traceable && (
          <button
            type="button"
            onClick={toggle}
            className="cc-btn cc-sm cc-dz-tap"
            aria-expanded={open}
            aria-description="Why did it say that?"
          >
            {open ? 'hide' : 'why?'}
          </button>
        )}
      </div>
      {open && (
        <div className="mt-2 border-t border-matrix-border pt-2 text-xs">
          {loading && <p className="text-slate-500">Loading trace…</p>}
          {!loading && trace && !trace.available && (
            <p className="text-slate-500">Trace not available for this turn.</p>
          )}
          {!loading && trace && trace.available && (
            <div className="space-y-1">
              {trace.selection_fallback ? (
                <p className="rounded bg-amber-950/40 p-1 text-[11px] text-amber-300">
                  {trace.selection_fallback === 'call_failed'
                    ? 'The speaker-selection call failed on this turn'
                    : trace.selection_fallback === 'truncated'
                      ? 'The moderator’s reply was cut off before it named anyone'
                      : 'The moderator’s reply named nobody in the cast'}
                  , so this speaker was drawn at random. Nothing chose them.
                </p>
              ) : (
                trace.selection_reason && (
                  <Line label="Chosen because" value={trace.selection_reason} />
                )
              )}
              {trace.rationale && <Line label="Rationale" value={trace.rationale} />}
              {trace.goal_served && <Line label="Goal served" value={trace.goal_served} />}
              {trace.memories && trace.memories.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-slate-500">
                    Memories in context
                  </div>
                  <ul className="list-inside list-disc text-slate-400">
                    {trace.memories.map((mem) => (
                      <li key={mem.id}>{mem.content}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="pt-1 text-[10px] text-slate-600">
                Model-generated introspection captured at generation time.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-[10px] uppercase tracking-wide text-slate-500">{label}: </span>
      <span className="text-slate-300">{value}</span>
    </div>
  )
}
