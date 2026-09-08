// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api } from '../api'
import { Hint } from '../components/Hint'
import { buildStructured } from '../lib/convictions'
import { parseSetup, parseSetupObject, ImportError } from '../lib/importSetup'
import { blankPersona, type DraftDoc, type DraftPersona } from './newRunTypes'

interface Props {
  onStarted: (runId: string) => void
  onCancel: () => void
  /**
   * Prefill from an existing run's setup ("start fresh from this conversation").
   * The form is the editor: everything loaded here is editable before anything runs,
   * and submitting creates a brand-new ROOT run — this is not a branch, and the
   * source run is not touched.
   */
  fromRunId?: string
}

const EXAMPLE = {
  topic: 'The merits and drawbacks of artificial intelligence in creative work',
  cast: [
    {
      name: 'Maya',
      persona:
        'A traditional artist who values human creativity and emotional authenticity. Skeptical of AI in art but open to thoughtful discussion.',
      goals: 'Express concerns about AI replacing human artists\nAdvocate for human experience in art',
    },
    {
      name: 'Alex',
      persona:
        'A tech-optimist and AI researcher who sees AI as a tool for expanding creative possibilities. Pragmatic and forward-thinking.',
      goals: 'Demonstrate how AI can augment human creativity',
    },
  ],
}

export function NewRunForm({ onStarted, onCancel, fromRunId }: Props) {
  const [topic, setTopic] = useState('')
  const [cast, setCast] = useState<DraftPersona[]>([blankPersona()])
  const [maxMessages, setMaxMessages] = useState(10)
  const [avatars, setAvatars] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  // Phase 1.5 summary options (collapsed by default; useful default = enabled).
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [summaryEnabled, setSummaryEnabled] = useState(true)
  const [summaryFocus, setSummaryFocus] = useState('')
  // Phase 2c cognition options. ON by default with every sub-feature, because a
  // run without cognition cannot answer "why did it say that?" — the dossier and
  // the why-trace are both empty — and that introspection is the point of the tool.
  // The engine default stays OFF (see CognitionConfig) so programmatic and CLI runs
  // are unchanged; this is a UI default for the interactive path, where the extra
  // 20-40% token cost is a deliberate, visible trade for something you can inspect.
  const [cognitionOpen, setCognitionOpen] = useState(false)
  const [cognitionEnabled, setCognitionEnabled] = useState(true)
  const [cogMemory, setCogMemory] = useState(true)
  const [cogReflect, setCogReflect] = useState(true)
  const [cogGoals, setCogGoals] = useState(true)
  const [cogRelationships, setCogRelationships] = useState(true)
  const [model, setModel] = useState('')
  const [models, setModels] = useState<{ id: string; label: string }[]>([])
  const [suggesting, setSuggesting] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.getModels().then((m) => {
      setModels(m.models)
      // Only fill in the default if nothing has chosen a model yet. A loaded setup
      // carries its own model, and these two requests race — assigning
      // unconditionally would silently reset it whichever way the race landed.
      setModel((current) => current || m.default)
    }).catch(() => undefined)
  }, [])

  const suggest = async () => {
    if (!topic.trim()) return
    setSuggesting(true)
    try {
      const s = await api.suggestName(topic)
      setName(s.name)
      setDescription(s.description)
    } catch {
      /* naming never blocks — leave fields for the user */
    } finally {
      setSuggesting(false)
    }
  }

  const loadExample = () => {
    setTopic(EXAMPLE.topic)
    setCast(EXAMPLE.cast.map((c) => ({ ...blankPersona(), ...c })))
    setMaxMessages(12)
  }

  const updatePersona = (i: number, patch: Partial<DraftPersona>) =>
    setCast((prev) => prev.map((p, idx) => (idx === i ? { ...p, ...patch } : p)))

  // Persona wizard. Authoring assistance only: it fills the form, and the operator
  // edits and submits. Nothing it returns starts a run by itself.
  const [wizardBrief, setWizardBrief] = useState('')
  const [wizardCount, setWizardCount] = useState(5)
  const [wizardBusy, setWizardBusy] = useState(false)
  const [wizardError, setWizardError] = useState<string | null>(null)

  const runWizard = async () => {
    setWizardError(null)
    setWizardBusy(true)
    try {
      const res = await api.suggestPersonas(wizardBrief.trim(), wizardCount, model || undefined)
      // REPLACES the cast rather than appending. A wizard that appends to whatever
      // is already there leaves a half-authored persona mixed into a drafted panel,
      // which is worse than either.
      setCast(
        res.cast.map((c) => ({
          name: c.name,
          persona: c.persona,
          goals: (c.goals || []).join('\n'),
          positions: (c.structured?.viewpoints || [])
            .map((v) => {
              const shifts = (v.evidence_that_shifts || []).join('; ')
              return `[${v.firmness}] ${v.position}${shifts ? ` -> ${shifts}` : ''}`
            })
            .join('\n'),
          // Index-aligned with positions above, including blanks, so a persona with a
          // concern on its second position only does not have it slide onto the first.
          concerns: (c.structured?.viewpoints || [])
            .map((v) => v.underlying_concern || '')
            .join('\n'),
          dismisses: (c.structured?.preferences?.dismisses || []).join('\n'),
          documents: [],
        })),
      )
      // The topic is usually the brief, and retyping it is pure friction.
      if (!topic.trim()) setTopic(wizardBrief.trim())
    } catch (e) {
      setWizardError(e instanceof Error ? e.message : 'The wizard failed. Try rephrasing.')
    } finally {
      setWizardBusy(false)
    }
  }

  // Import a conversation SETUP (a definition that has not run yet). Loads into the
  // form rather than starting a run, because the setup files people actually have
  // carry no convictions and no config — adding those before running is the point.
  const [importWarnings, setImportWarnings] = useState<string[]>([])
  const [importError, setImportError] = useState<string | null>(null)

  const applyParsed = (setup: ReturnType<typeof parseSetup>, extraWarnings: string[] = []) => {
    setTopic(setup.topic)
    setCast(setup.cast)
    if (setup.name) setName(setup.name)
    if (setup.description) setDescription(setup.description)
    if (setup.maxMessages) setMaxMessages(setup.maxMessages)
    if (setup.model) setModel(setup.model)
    if (setup.generateAvatars !== undefined) setAvatars(setup.generateAvatars)
    if (setup.cognition) {
      setCognitionEnabled(setup.cognition.enabled)
      setCogMemory(setup.cognition.memory ?? true)
      setCogReflect(setup.cognition.reflection ?? true)
      setCogGoals(Boolean(setup.cognition.goals_dynamic))
      setCogRelationships(Boolean(setup.cognition.relationships))
    }
    setImportWarnings([...extraWarnings, ...setup.warnings])
  }

  const describeFailure = (e: unknown, fallback: string) =>
    e instanceof ImportError || e instanceof Error ? e.message : fallback

  const applySetup = (text: string) => {
    setImportError(null)
    setImportWarnings([])
    try {
      applyParsed(parseSetup(text))
    } catch (e) {
      setImportError(describeFailure(e, 'Could not read that file.'))
    }
  }

  const onFile = async (file: File | undefined) => {
    if (!file) return
    applySetup(await file.text())
  }

  // "Start fresh from this conversation": load the source run's setup into the form.
  // Deliberately loaded for EDITING rather than run directly — re-running an
  // identical setup is what branching with no change already does, so the reason to
  // come here is to change something first.
  const [prefilling, setPrefilling] = useState(Boolean(fromRunId))
  const [prefilledFrom, setPrefilledFrom] = useState<string | null>(null)

  useEffect(() => {
    if (!fromRunId) return
    let cancelled = false
    setPrefilling(true)
    setImportError(null)
    setImportWarnings([])
    api.getRunSetup(fromRunId)
      .then((res) => {
        if (cancelled) return
        applyParsed(parseSetupObject(res.setup), res.warnings)
        setPrefilledFrom(res.setup.name || fromRunId)
      })
      .catch((e) => {
        if (!cancelled) setImportError(describeFailure(e, 'Could not load that setup.'))
      })
      .finally(() => {
        if (!cancelled) setPrefilling(false)
      })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromRunId])

  // Knowledge-base file upload. The server extracts the text and stores nothing, so
  // an uploaded file becomes an ordinary pasted-document entry: one ingest path, and
  // the operator can read and correct what was extracted before running. That review
  // step is the reason not to attach files blind — PDF extraction quality varies, and
  // a scanned page yields nothing at all.
  const [formats, setFormats] = useState<
    { suffix: string; available: boolean; needs: string | null }[]
  >([])
  const [maxUploadBytes, setMaxUploadBytes] = useState(0)
  const [uploading, setUploading] = useState<number | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)

  useEffect(() => {
    api.getDocumentFormats()
      .then((f) => {
        setFormats(f.formats)
        setMaxUploadBytes(f.max_upload_bytes)
      })
      .catch(() => undefined)
  }, [])

  const usableFormats = formats.filter((f) => f.available).map((f) => f.suffix)
  const missingFormats = formats.filter((f) => !f.available)

  // Functional update: several files are appended one at a time, and building the
  // next list from a captured `p.documents` would keep only the last file.
  const appendDocuments = (i: number, docs: DraftDoc[]) =>
    setCast((prev) =>
      prev.map((p, idx) => (idx === i ? { ...p, documents: [...p.documents, ...docs] } : p)),
    )

  const uploadDocuments = async (i: number, files: FileList | null) => {
    if (!files || files.length === 0) return
    setUploadError(null)
    setUploading(i)
    const failures: string[] = []
    try {
      for (const file of Array.from(files)) {
        try {
          const doc = await api.extractDocument(file)
          appendDocuments(i, [{ title: doc.title, text: doc.text }])
        } catch (e) {
          // Reported per file rather than aborting the batch: one unreadable PDF
          // should not discard the files that did extract.
          failures.push(`${file.name}: ${e instanceof Error ? e.message : 'failed'}`)
        }
      }
    } finally {
      setUploading(null)
      if (failures.length) setUploadError(failures.join(' · '))
    }
  }

  const anyConvictions = cast.some((c) => buildStructured(c) !== undefined)
  const anyDocuments = cast.some((c) => c.documents.some((d) => d.text.trim()))

  const submit = async () => {
    setError(null)
    const validCast = cast
      .filter((c) => c.name.trim() && c.persona.trim())
      .map((c) => {
        const structured = buildStructured(c)
        const docs = c.documents.filter((d) => d.text.trim())
        return {
          name: c.name.trim(),
          persona: c.persona.trim(),
          goals: c.goals
            .split(/[\n;]+/)
            .map((g) => g.trim())
            .filter(Boolean),
          ...(structured ? { structured } : {}),
          ...(docs.length
            ? {
                document_texts: docs.map((d, n) => ({
                  title: d.title.trim() || `pasted-${n + 1}.txt`,
                  text: d.text,
                })),
              }
            : {}),
        }
      })
    if (!topic.trim() || validCast.length === 0) {
      setError('A topic and at least one persona (name + persona) are required.')
      return
    }
    setSubmitting(true)
    try {
      const res = await api.createRun({
        topic: topic.trim(),
        cast: validCast,
        config: {
          max_messages: maxMessages,
          generate_avatars: avatars,
          cognition: cognitionEnabled
            ? {
                enabled: true,
                memory: cogMemory,
                reflection_every: cogReflect ? 4 : 0,
                goals_dynamic: cogGoals,
                relationships: cogRelationships,
              }
            : undefined,
          // Only sent when the cast actually authored the relevant content, so a
          // plain run's config stays as small as it was before these features
          // existed. Enabling a feature nobody configured would cost tokens for
          // an empty prompt block.
          personas: anyConvictions ? { enabled: true } : undefined,
          retrieval: anyDocuments ? { enabled: true } : undefined,
        },
        model: model || undefined,
        name: name.trim() || undefined,
        description: description.trim() || undefined,
        // Only send a summary config when it differs from the useful default
        // (enabled, no focus) — omitting it lets the server apply the default.
        summary:
          !summaryEnabled || summaryFocus.trim()
            ? { enabled: summaryEnabled, focus: summaryFocus.trim() || undefined }
            : undefined,
      })
      onStarted(res.run_id)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-100">
          {fromRunId ? 'New simulation from an existing setup' : 'New simulation'}
        </h1>
        <div className="flex gap-2">
          <button onClick={loadExample} className="rounded border border-matrix-border px-3 py-1 text-sm hover:border-matrix-accent">
            Load example
          </button>
          <button onClick={onCancel} className="rounded border border-matrix-border px-3 py-1 text-sm hover:border-matrix-accent">
            Cancel
          </button>
        </div>
      </div>

      {prefilling && (
        <p className="mb-3 rounded border border-matrix-border bg-matrix-panel p-2 text-sm text-slate-300">
          Loading the setup from that conversation…
        </p>
      )}

      {prefilledFrom && !prefilling && (
        <p className="mb-3 rounded border border-matrix-accent/40 bg-matrix-accent/10 p-2 text-sm text-slate-200">
          Prefilled from <strong>{prefilledFrom}</strong>. Edit anything below — the
          topic, the cast, their convictions, documents. Starting this creates a
          brand-new conversation; the original is untouched and nothing from its
          transcript carries over.
        </p>
      )}

      {error && <p className="mb-3 rounded bg-red-950/50 p-2 text-sm text-red-300">{error}</p>}

      <label className="block text-sm font-semibold text-slate-300">Topic</label>
      <textarea
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        rows={2}
        className="mt-1 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
        placeholder="What should the cast discuss?"
      />

      <div className="mt-4 grid grid-cols-1 gap-3 rounded-lg border border-matrix-border p-3 sm:grid-cols-[1fr_auto]">
        <div>
          <label className="block text-sm font-semibold text-slate-300">Run name (codename)</label>
          <div className="mt-1 flex gap-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
              placeholder="auto-generated (editable)"
            />
            <button
              onClick={suggest}
              disabled={suggesting || !topic.trim()}
              className="whitespace-nowrap rounded bg-matrix-accent/20 px-3 py-1 text-sm text-matrix-accent hover:bg-matrix-accent/30 disabled:opacity-40"
              title="Generate a topical codename"
            >
              {suggesting ? '…' : '🎲 Re-roll'}
            </button>
          </div>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
            placeholder="one-line description (auto-suggested, editable)"
          />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm text-slate-300">
          Max messages
          <Hint label="max messages">
            Turn budget for the run. Cost scales roughly linearly with it — one turn is
            two model calls. With five personas, 15 turns gives each about three turns,
            which is often too few for a position to be challenged and held; 30 gives
            about six. Longer runs also make rate-style measurements more meaningful,
            since a single turn is a smaller fraction of the total.
          </Hint>
          <input
            type="number"
            min={1}
            max={100}
            value={maxMessages}
            onChange={(e) => setMaxMessages(Number(e.target.value))}
            className="w-20 rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input type="checkbox" checked={avatars} onChange={(e) => setAvatars(e.target.checked)} />
          Generate avatars
          <Hint label="generate avatars">
            Anime-style portraits for each persona, generated once before the run via
            Stability SD3.5 on Bedrock. Purely cosmetic and entirely optional: it adds an
            image call per persona, and if it fails or no image provider is configured the
            cards fall back to initials without affecting the run.
          </Hint>
        </label>
        {models.length > 0 && (
          <label className="flex items-center gap-2 text-sm text-slate-300">
            Model
            <Hint label="model">
              Which model drives every persona, the moderator and the analyst. This is not
              only a cost/quality dial — models differ in ways that change behaviour, and
              measured differences here have been large. Worth re-checking a run's
              behaviour after switching rather than assuming it carries over.
            </Hint>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className="mt-4 rounded-lg border border-matrix-border p-3">
        <button
          type="button"
          onClick={() => setSummaryOpen((o) => !o)}
          className="flex w-full items-center justify-between text-left text-sm font-semibold text-slate-300"
        >
          <span className="flex items-center gap-2">
            Summary options
            <Hint label="summary options">
              A post-run analyst pass over the finished transcript: consensus, dissenters,
              key ideas, open questions. Useful when you care about the conclusions more
              than the conversation. Costs one extra call at the end, and you can always
              generate it later from the run page instead.
            </Hint>
          </span>
          <span className="text-xs text-slate-500">
            {summaryEnabled ? 'auto-summary on' : 'auto-summary off'} {summaryOpen ? '▲' : '▼'}
          </span>
        </button>
        {summaryOpen && (
          <div className="mt-3 space-y-2">
            <p className="text-[11px] text-slate-500">
              When the run completes, generate a structured analyst summary (consensus,
              dissenters, key ideas, open questions, overview). Model-generated analysis — you
              can also generate it later on the run page.
            </p>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={summaryEnabled}
                onChange={(e) => setSummaryEnabled(e.target.checked)}
              />
              Auto-generate summary at completion
            </label>
            <input
              value={summaryFocus}
              onChange={(e) => setSummaryFocus(e.target.value)}
              disabled={!summaryEnabled}
              placeholder="Optional focus (e.g. emphasize legal and ethical risk)"
              className="w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm disabled:opacity-40"
            />
          </div>
        )}
      </div>

      <div className="mt-4 rounded-lg border border-matrix-border p-3">
        <button
          type="button"
          onClick={() => setCognitionOpen((o) => !o)}
          className="flex w-full items-center justify-between text-left text-sm font-semibold text-slate-300"
        >
          <span className="flex items-center gap-2">
            Cognition (introspectable engine)
            <Hint label="cognition">
              Turn this on when you want to know <em>why</em> an agent said something, not
              just what it said. Each turn additionally produces a first-person rationale
              and the goal it served, which is what powers the dossier and the "why did it
              say that?" trace. Costs roughly 20-40% more tokens per turn, and measurably
              shortens turns (agents spend fewer characters on the utterance itself). Leave
              it off for a fast, cheap conversation.
            </Hint>
          </span>
          <span className="text-xs text-slate-500">
            {cognitionEnabled ? 'on' : 'off'} {cognitionOpen ? '▲' : '▼'}
          </span>
        </button>
        {cognitionOpen && (
          <div className="mt-3 space-y-2">
            <p className="text-[11px] text-slate-500">
              When on, each agent produces a genuine per-turn rationale, forms a memory
              stream, and (optionally) reflects, evolves goals, and tracks relationships —
              enabling the per-agent dossier and the “why did it say that?” trace. This adds
              tokens/cost per turn. Model-generated introspection, not ground truth.
            </p>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={cognitionEnabled}
                onChange={(e) => setCognitionEnabled(e.target.checked)}
              />
              Enable cognition
            </label>
            <div className="grid grid-cols-2 gap-2 pl-6">
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input type="checkbox" checked={cogMemory} disabled={!cognitionEnabled}
                  onChange={(e) => setCogMemory(e.target.checked)} />
                Memory stream
                <Hint label="memory stream">
                  Each agent records what it just learned or decided, and its most relevant
                  memories are fed back into later turns. This is what gives an agent
                  continuity — it can refer to what someone committed to eight turns ago
                  instead of starting fresh each time. Expect roughly one memory per turn.
                </Hint>
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input type="checkbox" checked={cogReflect} disabled={!cognitionEnabled}
                  onChange={(e) => setCogReflect(e.target.checked)} />
                Reflection (every 4 turns)
                <Hint label="reflection">
                  Every fourth turn, the speaker condenses its recent memories into a
                  higher-level belief. Measured effect: reflections tend to <em>harden</em> a
                  position rather than erode it, so this is worth enabling when you want
                  participants who dig in rather than drift toward agreement. Costs one
                  extra call each time it fires.
                </Hint>
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input type="checkbox" checked={cogGoals} disabled={!cognitionEnabled}
                  onChange={(e) => setCogGoals(e.target.checked)} />
                Dynamic goals
                <Hint label="dynamic goals">
                  Lets an agent rewrite its own goal list mid-run when the conversation
                  genuinely changes what it wants. Enable it to watch priorities shift under
                  pressure; leave it off if you need each agent to keep pursuing the same
                  thing so runs stay comparable.
                </Hint>
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input type="checkbox" checked={cogRelationships} disabled={!cognitionEnabled}
                  onChange={(e) => setCogRelationships(e.target.checked)} />
                Relationships
                <Hint label="relationships">
                  Each agent maintains a one-line stance toward every other participant, and
                  updates it as the conversation goes. Useful when the interpersonal dynamic
                  is the thing you are studying — who trusts whom, who has written whom off.
                  Shown in the dossier.
                </Hint>
              </label>
            </div>
          </div>
        )}
      </div>

      {/* Import a setup file. Above the wizard and the cast, since it replaces both. */}
      <div className="mt-5 rounded-lg border border-matrix-border p-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-300">Import a setup</h2>
          <Hint label="import a setup">
            Load a conversation from a JSON file. The format is exactly what the run API
            accepts — <code>{'{ "topic": ..., "cast": [{ "name", "persona", "goals" }] }'}</code>
            — so anything you can run, a file can describe. Optional per persona:{' '}
            <code>structured</code> for convictions and <code>document_texts</code> for
            background. Optional at the top level: <code>config</code>,{' '}
            <code>name</code>, <code>description</code>.
            <br />
            <br />
            It loads into this form so you can add convictions or turn on cognition
            before running. Replaces the topic and the whole cast.
          </Hint>
          <label className="ml-auto cursor-pointer rounded border border-matrix-border px-3 py-1 text-xs hover:border-matrix-accent">
            Choose file…
            <input
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(e) => {
                void onFile(e.target.files?.[0])
                // Cleared so choosing the SAME file twice re-fires change; otherwise a
                // re-import after editing the form silently does nothing.
                e.target.value = ''
              }}
            />
          </label>
        </div>
        <textarea
          onPaste={(e) => {
            const text = e.clipboardData.getData('text')
            if (text.trim().startsWith('{')) {
              e.preventDefault()
              applySetup(text)
            }
          }}
          placeholder="…or paste the JSON here"
          rows={2}
          className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 font-mono text-xs"
        />
        {importError && <p className="mt-2 text-xs text-red-400">{importError}</p>}
        {importWarnings.length > 0 && (
          // Shown rather than swallowed: an operator who pasted eight personas and got
          // seven needs to know which one vanished and why.
          <ul className="mt-2 list-inside list-disc text-xs text-amber-500/90">
            {importWarnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        )}
      </div>

      {/* Persona wizard. Sits above the cast because it REPLACES it — putting it
          below would imply it adds to what you have already typed. */}
      <div className="mt-5 rounded-lg border border-matrix-accent/30 bg-matrix-accent/5 p-3">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-300">Draft a cast for me</h2>
          <Hint label="draft a cast">
            Describe the situation in a sentence or two and this drafts a panel of
            stakeholders who would genuinely disagree — including the convictions,
            which are the fiddly part to write by hand. It is a <em>draft you edit</em>:
            nothing runs until you press Run, and every field below stays editable.
            It deliberately does not make the firmest positions the correct ones, so
            you cannot win the discussion by agreeing with whoever pushes hardest.
          </Hint>
        </div>
        <textarea
          value={wizardBrief}
          onChange={(e) => setWizardBrief(e.target.value)}
          placeholder="e.g. We're deciding whether to move our on-prem product to a hosted SaaS model next year."
          rows={2}
          className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
        />
        <div className="mt-2 flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Stakeholders
            <input
              type="number"
              min={2}
              max={7}
              value={wizardCount}
              onChange={(e) => setWizardCount(Number(e.target.value))}
              className="w-16 rounded border border-matrix-border bg-matrix-bg p-1 text-xs"
            />
          </label>
          <button
            type="button"
            onClick={runWizard}
            disabled={wizardBusy || !wizardBrief.trim()}
            className="rounded border border-matrix-accent px-3 py-1 text-xs font-semibold text-matrix-accent hover:bg-matrix-accent/10 disabled:opacity-40"
          >
            {wizardBusy ? 'Drafting…' : '✨ Draft cast'}
          </button>
          <span className="text-[11px] text-slate-500">Replaces the cast below.</span>
        </div>
        {wizardError && (
          <p className="mt-2 text-xs text-red-400">{wizardError}</p>
        )}
      </div>

      <div className="mt-5">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Cast</h2>
          <button
            onClick={() => setCast((p) => [...p, blankPersona()])}
            className="rounded border border-matrix-border px-2 py-1 text-xs hover:border-matrix-accent"
          >
            + Add persona
          </button>
        </div>
        <div className="space-y-3">
          {cast.map((p, i) => (
            <div key={i} className="rounded-lg border border-matrix-border p-3">
              <div className="flex items-center gap-2">
                <input
                  value={p.name}
                  onChange={(e) => updatePersona(i, { name: e.target.value })}
                  placeholder="Name"
                  className="w-40 rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
                />
                {cast.length > 1 && (
                  <button
                    onClick={() => setCast((prev) => prev.filter((_, idx) => idx !== i))}
                    className="ml-auto text-xs text-slate-500 hover:text-red-400"
                  >
                    remove
                  </button>
                )}
              </div>
              <textarea
                value={p.persona}
                onChange={(e) => updatePersona(i, { persona: e.target.value })}
                placeholder="Persona description"
                rows={2}
                className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
              />
              <textarea
                value={p.goals}
                onChange={(e) => updatePersona(i, { goals: e.target.value })}
                placeholder="Goals (one per line)"
                rows={2}
                className="mt-2 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-sm"
              />

              {/* Phase 6 convictions and Phase 5 documents. Both optional and both
                  collapsed, because a two-persona coffee-shop chat should not have to
                  scroll past them — but discoverable, which they were not at all
                  before: they existed only in hand-written config files. */}
              <details className="mt-2 rounded border border-matrix-border/60 p-2">
                <summary className="cursor-pointer text-xs font-semibold text-slate-400">
                  Convictions &amp; background documents{' '}
                  <span className="font-normal text-slate-600">(optional)</span>
                </summary>

                <label className="mt-2 flex items-center gap-2 text-xs text-slate-400">
                  Positions this persona defends
                  <Hint label="positions">
                    Goals are <em>satisfiable</em> — an agent will accept any plan that meets
                    one. Convictions are <em>defended</em>. One per line. Optionally prefix{' '}
                    <code>[firm]</code>, <code>[non-negotiable]</code> or{' '}
                    <code>[requires-escalation]</code>, and add{' '}
                    <code>-&gt; what would change their mind</code>. Without a firmness tag a
                    position is negotiable and will shift on a good argument.
                  </Hint>
                </label>
                <textarea
                  value={p.positions}
                  onChange={(e) => updatePersona(i, { positions: e.target.value })}
                  placeholder={'[firm] No feature may add an external service -> an embedded index that is a file\nShip this quarter'}
                  rows={3}
                  className="mt-1 w-full rounded border border-matrix-border bg-matrix-bg p-2 font-mono text-xs"
                />

                <label className="mt-2 flex items-center gap-2 text-xs text-amber-500/80">
                  What is really behind them — withheld
                  <Hint label="withheld concerns">
                    The real worry under each position, usually personal stakes: what it
                    costs <em>them</em> if they are wrong. One per line,{' '}
                    <strong>matched to the positions above by line number</strong>.
                    <br />
                    <br />
                    The persona knows this and it shapes what it argues for, but it will
                    not volunteer it — it comes out only if someone asks why it holds the
                    position. Drawing it out is the exercise, which is why it never
                    appears in the moderator's view, the event log or the dossier.
                  </Hint>
                </label>
                <textarea
                  value={p.concerns}
                  onChange={(e) => updatePersona(i, { concerns: e.target.value })}
                  placeholder={'I own the failure when a customer never reaches a working run'}
                  rows={2}
                  className="mt-1 w-full rounded border border-amber-600/30 bg-matrix-bg p-2 text-xs"
                />

                <label className="mt-2 flex items-center gap-2 text-xs text-slate-400">
                  Will not weigh
                  <Hint label="will not weigh">
                    Concerns this persona declines to <em>weigh</em> — not to engage with. It
                    still has to answer the substance of a challenge, then say once that the
                    concern is not its to weigh. This is what stops five personas politely
                    agreeing with each other; measured as the highest-value field of the lot.
                    One per line.
                  </Hint>
                </label>
                <textarea
                  value={p.dismisses}
                  onChange={(e) => updatePersona(i, { dismisses: e.target.value })}
                  placeholder={'retrieval answer quality\nshipping schedule'}
                  rows={2}
                  className="mt-1 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-xs"
                />

                <div className="mt-3 flex items-center gap-2">
                  <span className="text-xs font-semibold text-slate-400">
                    Background documents
                  </span>
                  <Hint label="background documents">
                    This persona's knowledge base: material only it can draw on. Upload a
                    file or paste text. It is indexed and retrieved a few passages at a
                    time, so a long document does not sit in the prompt on every call —
                    that context budget is the whole point of the feature. The persona
                    cites what it actually retrieved, and the dossier shows which passages
                    it used.
                    <br />
                    <br />
                    Uploaded files are <strong>read and converted to text</strong>, not
                    stored — so what you see below is exactly what the persona will have.
                    Worth a glance for PDFs, where extraction quality varies and a scanned
                    page yields no text at all.
                    {usableFormats.length > 0 && (
                      <>
                        <br />
                        <br />
                        Accepted here: {usableFormats.join(', ')}
                        {maxUploadBytes > 0 &&
                          `, up to ${Math.round(maxUploadBytes / (1024 * 1024))} MB each`}
                        .
                      </>
                    )}
                  </Hint>
                  <label
                    className="ml-auto cursor-pointer rounded border border-matrix-border px-2 py-0.5 text-[11px] hover:border-matrix-accent"
                    title={`Upload a knowledge-base file for ${p.name || 'this persona'}`}
                  >
                    {uploading === i ? 'reading…' : '⬆ upload file'}
                    {/* A real file input, kept visually hidden rather than replaced by a
                        button + click(): it stays keyboard-reachable and the label's text
                        is its accessible name. */}
                    <input
                      type="file"
                      multiple
                      className="sr-only"
                      accept={usableFormats.join(',') || undefined}
                      disabled={uploading !== null}
                      onChange={(e) => {
                        void uploadDocuments(i, e.target.files)
                        // Cleared so choosing the same file twice fires onChange again.
                        e.target.value = ''
                      }}
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() =>
                      updatePersona(i, {
                        documents: [...p.documents, { title: '', text: '' }],
                      })
                    }
                    className="rounded border border-matrix-border px-2 py-0.5 text-[11px] hover:border-matrix-accent"
                  >
                    + paste text
                  </button>
                </div>

                {missingFormats.length > 0 && (
                  <p className="mt-1 text-[11px] text-amber-400">
                    {missingFormats.map((f) => f.suffix).join(' and ')} upload needs{' '}
                    {[...new Set(missingFormats.map((f) => f.needs))].join(' and ')} on the
                    server (<code>pip install 'matrix-sim-studio[documents]'</code>). Paste
                    the text instead until then.
                  </p>
                )}

                {uploadError && (
                  <p className="mt-1 rounded bg-red-950/50 p-1 text-[11px] text-red-300">
                    {uploadError}
                  </p>
                )}
                {p.documents.map((d, di) => (
                  <div key={di} className="mt-2 rounded border border-matrix-border/60 p-2">
                    <div className="flex items-center gap-2">
                      <input
                        value={d.title}
                        onChange={(e) =>
                          updatePersona(i, {
                            documents: p.documents.map((x, xi) =>
                              xi === di ? { ...x, title: e.target.value } : x,
                            ),
                          })
                        }
                        placeholder="title (e.g. distribution-constraints.md)"
                        className="w-full rounded border border-matrix-border bg-matrix-bg p-1 text-xs"
                      />
                      <button
                        type="button"
                        onClick={() =>
                          updatePersona(i, {
                            documents: p.documents.filter((_, xi) => xi !== di),
                          })
                        }
                        className="text-[11px] text-slate-500 hover:text-red-400"
                      >
                        remove
                      </button>
                    </div>
                    <textarea
                      value={d.text}
                      onChange={(e) =>
                        updatePersona(i, {
                          documents: p.documents.map((x, xi) =>
                            xi === di ? { ...x, text: e.target.value } : x,
                          ),
                        })
                      }
                      placeholder="Paste the document text here"
                      rows={4}
                      className="mt-1 w-full rounded border border-matrix-border bg-matrix-bg p-2 text-xs"
                    />
                  </div>
                ))}
              </details>
            </div>
          ))}
        </div>
      </div>

      <button
        onClick={submit}
        disabled={submitting}
        className="mt-6 w-full rounded-lg bg-matrix-accent py-3 font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-50"
      >
        {submitting ? 'Starting…' : '▶ Run simulation'}
      </button>
    </div>
  )
}
