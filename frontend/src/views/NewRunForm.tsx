// SPDX-License-Identifier: Apache-2.0
import { useEffect, useState } from 'react'
import { api, type Forecast } from '../api'
import { CostForecast } from '../components/CostForecast'
import { CastTemplates } from '../components/CastTemplates'
import { Hint } from '../components/Hint'
import { buildStructured } from '../lib/convictions'
import { parseSetup, parseSetupObject, ImportError } from '../lib/importSetup'
import {
  blankPersona, CEILING_TURNS, DEFAULT_REPLICATES, MAX_MEMBERS, MIN_REPLICATES,
  type DraftDoc, type DraftPersona, type Method, type RunType,
} from './newRunTypes'
import { KbPicker } from '../components/KbPicker'

interface Props {
  onStarted: (runId: string) => void
  /**
   * Where an ensemble goes once it is fanned out. Separate from `onStarted` because an
   * ensemble is not a run — it has no transcript and no live stream, and sending the
   * operator to a run view would show them one of five conversations as though it were the
   * result.
   */
  onEnsembleStarted: (ensembleId: string) => void
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

export function NewRunForm({ onStarted, onEnsembleStarted, onCancel, fromRunId }: Props) {
  const [topic, setTopic] = useState('')
  // One conversation, or the same brief several times. A run TYPE rather than another
  // speaker method: a method decides who talks inside one conversation, this decides how
  // many conversations exist and produces a different artefact.
  const [runType, setRunType] = useState<RunType>('single')
  const [replicates, setReplicates] = useState(DEFAULT_REPLICATES)
  // A second cell that differs ONLY in speaker method. Off by default, because the design
  // doc's whole argument is that replicates answer the question an operator is asking and a
  // varied cell answers a narrower one.
  const [compareHybrid, setCompareHybrid] = useState(false)
  const [hybridReplicates, setHybridReplicates] = useState(MIN_REPLICATES)
  // What the avatar toggle was before the ensemble default turned it off, so switching back
  // to a single run restores the operator's choice rather than leaving it off silently.
  // Same idiom as `turnsBeforeCeiling`.
  const [avatarsBeforeEnsemble, setAvatarsBeforeEnsemble] = useState<boolean | null>(null)
  const [cast, setCast] = useState<DraftPersona[]>([blankPersona()])
  // Phase 6: collections every persona in the run may search — the cast-wide case,
  // which generalises Phase 5's `persona_name IS NULL` exactly.
  const [runKbs, setRunKbs] = useState<string[]>([])
  // Pre-conversation research (docs/PERSONA-RESEARCH.md §7). OFF by default and that is
  // deliberate rather than cautious: it searches the open web and costs a few minutes and
  // a quarter of a dollar per run, so it is a decision the operator makes each time.
  const [research, setResearch] = useState(false)
  // Ask personas to cite each passage they rely on, inline. Opt-in: docs/CITE-INLINE.md.
  const [citeInline, setCiteInline] = useState(false)
  const [researchShared, setResearchShared] = useState(true)
  const [researchPersonas, setResearchPersonas] = useState(true)
  // Sources taken per query. The one genuine dial: it decides how wide the search goes.
  // `queries per viewpoint` is deliberately NOT exposed — see the note by the toggle.
  const [researchResults, setResearchResults] = useState(5)
  const [maxMessages, setMaxMessages] = useState(10)
  // Let the moderator end the run when nobody has anything left to add. When it is on the
  // turn count stops being a plan and becomes a ceiling, so the number is raised and
  // relabelled — a 10-turn "budget" would cut a converging conversation off long before it
  // converged, and the whole point is that the run decides its own length.
  // `moderated` (a model picks one speaker per turn) or `simultaneous` (everyone is asked
  // every round, passes are dropped). Not a checkbox: there are two named methods and a
  // third is plausible, and "☐ simultaneous" would leave the default mode unnamed.
  const [method, setMethod] = useState<Method>('moderated')
  // Only used by `hybrid`. Two by default, from the one live simultaneous run: round 1 put
  // every position on the table, round 2 was the strongest of eight, and the parallel
  // restatement starts at round 3.
  const [openingRounds, setOpeningRounds] = useState(2)
  const [stopWhenConverged, setStopWhenConverged] = useState(false)
  // One final round when the run hits its ceiling without finishing.
  const [closingRound, setClosingRound] = useState(false)
  // What the turn count was before the toggle raised it, so turning it off puts it back
  // rather than leaving the operator with a 100-turn bill they did not choose.
  const [turnsBeforeCeiling, setTurnsBeforeCeiling] = useState<number | null>(null)
  // ON by default, matching the engine default (`enable_avatars`): a cast board of
  // placeholder initials is the first thing an operator sees, and the form used to send
  // an explicit `false` that overrode the deployment default nobody had turned off.
  // Still a toggle, so a run that does not want the image-model spend can decline.
  const [avatars, setAvatars] = useState(true)
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
          ...blankPersona(),
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
    if (setup.stopWhenConverged !== undefined) setStopWhenConverged(setup.stopWhenConverged)
    if (setup.model) setModel(setup.model)
    if (setup.generateAvatars !== undefined) setAvatars(setup.generateAvatars)
    // Same "absent is not empty" rule as avatars: a setup file written before knowledge
    // bases existed must not clear a selection the operator has already made.
    if (setup.knowledgeBases !== undefined) setRunKbs(setup.knowledgeBases)
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
  const anyKnowledgeBases =
    runKbs.length > 0 || cast.some((c) => c.knowledgeBases.length > 0)

  // How many collections a research pass would build: the shared one, plus one per persona
  // who actually has a viewpoint to research. §7 asks for this on the button — research is
  // "1 + N corpora" and an operator should see "researching 7 collections" BEFORE agreeing
  // to it, for the same reason the ensemble button names the run count. A number that
  // appears only in the bill is a number nobody consented to.
  const researchCollections =
    (researchShared ? 1 : 0) +
    (researchPersonas
      ? cast.filter((c) => {
          const s = buildStructured(c)
          return c.name.trim() && s !== undefined && (s.viewpoints?.length ?? 0) > 0
        }).length
      : 0)

  // The cast as the API takes it: complete personas only. Used to launch, to forecast, and to save as a
  // template, so all three see the same cast.
  const buildCast = () =>
    cast
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
          // Omitted when empty rather than sent as `[]`, matching every other optional
          // field here: an empty array would make the server validate a binding list
          // nobody chose.
          ...(c.knowledgeBases.length ? { knowledge_bases: c.knowledgeBases } : {}),
        }
      })

  // The launch request, built once for both launching and forecasting, so the price shown is the
  // price of exactly what would start. `null` when the form cannot launch yet.
  const buildRequest = () => {
    const validCast = buildCast()
    if (!topic.trim() || validCast.length === 0) return null
    const body = {
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
        // Only sent when asked for: it defaults off server-side while it is being
        // validated, and an absent block means "use the deployment default".
        // Sent only when it differs from the server's default, so a plain run's config
        // stays as small as it was before either option existed.
        selection:
          method !== 'moderated' || stopWhenConverged || closingRound
            ? {
                ...(method !== 'moderated' ? { method } : {}),
                ...(method === 'hybrid' ? { hybrid_opening_rounds: openingRounds } : {}),
                ...(stopWhenConverged ? { stop_when_converged: true } : {}),
                ...(closingRound ? { closing_round: true } : {}),
              }
            : undefined,
        // Retrieval has to be ON for a binding to do anything: `retrieve_for_turn` is
        // never called with it disabled, so a run that bound three collections and
        // pasted no documents would search none of them and say nothing about why.
        //
        // `research` is included in that condition for exactly the same reason, and it is
        // the more dangerous case: research creates collections and binds them itself, so
        // an operator who enabled research and attached nothing else would pay for a full
        // search pass and then run a conversation that never queried it. The corpus would
        // be there, complete, and invisible — which is the failure this project keeps
        // finding. `authority_floor` rides along because a floor of 0 is off, and a run
        // that went looking for statutes should not then let them lose every slot.
        retrieval:
          anyDocuments || anyKnowledgeBases || research
            ? {
                enabled: true,
                ...(research ? { authority_floor: 1 } : {}),
                ...(citeInline ? { cite_inline: true } : {}),
              }
            : undefined,
        // Sent only when asked for. `targets` is never sent: the server resolves it and
        // overwrites whatever arrives, because it names collections to WRITE into.
        research: research
          ? {
              enabled: true,
              shared: researchShared,
              personas: researchPersonas,
              results_per_query: researchResults,
            }
          : undefined,
        ...(runKbs.length ? { knowledge_bases: runKbs } : {}),
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
    }

    // `cells` is sent even for the replicates-only case rather than omitted. The server
    // would default to exactly this, but an explicit spec is what the report header
    // renders, and a spec that says `n: 5` is the difference between a reader knowing
    // five runs were asked for and inferring it from how many exist.
    const cells = [
      { label: 'base', n: replicates },
      // Only `selection.method` and its opening-round count differ. Anything else —
      // turn count, fairness — would confound the comparison, and the server refuses
      // it with the reason.
      ...(compareHybrid
        ? [
            {
              label: 'hybrid',
              n: hybridReplicates,
              overrides: {
                'selection.method': 'hybrid',
                'selection.hybrid_opening_rounds': openingRounds,
              },
            },
          ]
        : []),
    ]
    return { body, cells }
  }

  // The forecast is asked for whenever the request would change, debounced. Documents are left out
  // of what is sent: the forecast does not read them, and a pasted document would otherwise be
  // uploaded on every keystroke.
  const built = buildRequest()
  const forecastKey = built
    ? JSON.stringify({
        runType,
        body: {
          ...built.body,
          cast: built.body.cast.map(({ document_texts: _docs, ...c }) => c),
          ...(runType === 'ensemble' ? { cells: built.cells } : {}),
        },
      })
    : ''
  const [forecast, setForecast] = useState<Forecast | null>(null)
  const [forecastLoading, setForecastLoading] = useState(false)
  const [forecastError, setForecastError] = useState<string | null>(null)
  useEffect(() => {
    if (!forecastKey) {
      setForecast(null)
      setForecastError(null)
      return
    }
    const { runType: kind, body } = JSON.parse(forecastKey)
    const ctl = new AbortController()
    setForecastLoading(true)
    const id = setTimeout(() => {
      // Through a promise chain, so a client or deployment without the route degrades to
      // "unavailable" instead of an uncaught error in the form.
      Promise.resolve()
        .then(() =>
          kind === 'ensemble'
            ? api.forecastEnsemble(body, ctl.signal)
            : api.forecastRun(body, ctl.signal),
        )
        .then((f) => {
          setForecast(f)
          setForecastError(null)
        })
        .catch((e) => {
          if (!ctl.signal.aborted) setForecastError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (!ctl.signal.aborted) setForecastLoading(false)
        })
    }, 600)
    return () => {
      clearTimeout(id)
      ctl.abort()
    }
  }, [forecastKey])

  const submit = async () => {
    setError(null)
    const built = buildRequest()
    if (!built) {
      setError('A topic and at least one persona (name + persona) are required.')
      return
    }
    setSubmitting(true)
    try {
      const { body, cells } = built
      if (runType === 'ensemble') {
        const ens = await api.createEnsemble({ ...body, cells })
        onEnsembleStarted(ens.ensemble_id)
        return
      }

      const res = await api.createRun(body)
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
          {stopWhenConverged ? 'Turn ceiling' : 'Max messages'}
          <Hint label={stopWhenConverged ? 'turn ceiling' : 'max messages'}>
            {stopWhenConverged ? (
              <>
                A safety net, not a target. The run ends when the moderator judges the
                discussion finished, so this only stops a conversation that never gets
                there. Leave it high.
                <br />
                <br />
                You are still billed for the turns actually generated, so a run that
                converges at 32 costs 32 turns — but a run that never converges costs the
                whole ceiling, which is why there is one.
              </>
            ) : (
              <>
                Turn budget for the run. Cost scales roughly linearly with it — one turn is
                two model calls. With five personas, 15 turns gives each about three turns,
                which is often too few for a position to be challenged and held; 30 gives
                about six. Longer runs also make rate-style measurements more meaningful,
                since a single turn is a smaller fraction of the total.
              </>
            )}
          </Hint>
          <input
            type="number"
            min={1}
            max={stopWhenConverged ? 300 : 100}
            value={maxMessages}
            onChange={(e) => setMaxMessages(Number(e.target.value))}
            className="w-20 rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          Run
          <Hint label="run type">
            <strong>Once</strong> — one conversation, one transcript.
            <br />
            <br />
            <strong>Several times</strong> — the same brief run N times with{' '}
            <em>nothing varied</em>, then a report over all of them saying which conclusions
            held in every run and which appeared in only one.
            <br />
            <br />
            Running the same thing repeatedly sounds pointless and is the most useful mode.
            Six existing conversations were compared as three pairs with byte-identical
            settings, and two of the pairs disagreed on the biggest questions in the brief —
            one pair diverged on whether the work's scope expanded, another on whether the
            central legal question was ever settled. Nothing was varied to cause that, so it
            is the brief's own ambiguity. A single run hands you either answer with equal
            confidence.
            <br />
            <br />
            Replicates are also the only way to read the comparison below: a difference
            between two settings means nothing unless you know how much each setting varies
            on its own.
            <br />
            <br />
            Costs N times one conversation. The report adds one model call per run plus one
            over all of them.
          </Hint>
          <select
            value={runType}
            onChange={(e) => {
              const next = e.target.value as RunType
              setRunType(next)
              // Avatars OFF by default for an ensemble. They are generated per run, so the
              // same cast's faces would be drawn N times over — and image spend is not
              // counted in a run's reported cost, only voice calls are, so it would be both
              // multiplied and invisible. Nothing is confounded either way: every member
              // still gets the same config as every other.
              //
              // A default, not a hardcode: the checkbox still shows and still works, which is
              // the rule the avatar toggle already follows.
              if (next === 'ensemble') {
                setAvatarsBeforeEnsemble(avatars)
                setAvatars(false)
              } else if (avatarsBeforeEnsemble !== null) {
                setAvatars(avatarsBeforeEnsemble)
              }
            }}
            className="rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
          >
            <option value="single">Once — a single conversation</option>
            <option value="ensemble">Several times — same brief, then a report</option>
          </select>
        </label>
        {runType === 'ensemble' && (
          <div className="ml-6 space-y-2 border-l border-matrix-border pl-4">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              How many times
              <Hint label="replicates">
                Five by default. That supports three coarse verdicts per conclusion —
                held in every run, split, or raised only once — and nothing finer. It will not
                support reading 3-of-5 against 2-of-5 as a difference, and the report says so
                in its own body rather than letting you infer precision that is not there.
                <br />
                <br />
                Two is the minimum: with one run a group has no internal variation, and that
                variation is the only thing separating a real finding from a coin flip.
              </Hint>
              <input
                type="number"
                min={MIN_REPLICATES}
                max={MAX_MEMBERS}
                value={replicates}
                onChange={(e) => setReplicates(Number(e.target.value))}
                className="w-16 rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
              />
            </label>
            <label className="flex items-start gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                className="mt-1"
                checked={compareHybrid}
                onChange={(e) => setCompareHybrid(e.target.checked)}
              />
              <span>
                Also compare against hybrid
                <Hint label="compare against hybrid">
                  Adds a second group that differs in <em>one</em> thing: the speaker method.
                  Everything else — turn count, fairness, the cast, the brief — is held
                  identical, and the report counts each group separately rather than pooling
                  them.
                  <br />
                  <br />
                  Separate counts are the point. A conclusion that holds in every moderated
                  run and in no hybrid run is a strong, method-dependent finding; pooled, the
                  same numbers read as a weak half-and-half split. Those call for opposite
                  decisions.
                  <br />
                  <br />
                  Hybrid is the comparison worth making because blind opening rounds change
                  what a persona has <em>seen</em> when they speak, so they can reach a
                  conclusion that hearing someone else first would have suppressed. Turn count
                  and fairness are deliberately not offered: a shorter run does not disagree,
                  it just never arrives, and fairness is already settled.
                </Hint>
              </span>
            </label>
            {compareHybrid && (
              <label className="flex items-center gap-2 pl-6 text-sm text-slate-300">
                Hybrid runs
                <input
                  type="number"
                  min={MIN_REPLICATES}
                  max={MAX_MEMBERS}
                  value={hybridReplicates}
                  onChange={(e) => setHybridReplicates(Number(e.target.value))}
                  className="w-16 rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
                />
                <span className="text-[11px] text-slate-500">
                  (uses the opening-round count below)
                </span>
              </label>
            )}
            <p className="text-[11px] text-slate-500">
              {replicates + (compareHybrid ? hybridReplicates : 0)} conversations, each up to{' '}
              {maxMessages} turns.
              {compareHybrid
                ? ' Only the speaker method differs between the two groups.'
                : ' Nothing differs between them.'}
            </p>
          </div>
        )}
        <label className="flex items-center gap-2 text-sm text-slate-300">
          Conversation method
          <Hint label="conversation method">
            <strong>Moderated</strong> — a model reads the room each turn and picks one
            speaker. One voice call per turn; who speaks is a judgement, and everything in
            docs/SPEAKER-SELECTION-EVALUATION.md is about making that judgement fairer.
            <br />
            <br />
            <strong>Rotation</strong> — everyone speaks once per round, in order, each one
            seeing what the earlier speakers in that round said. Turn share is equal by
            construction rather than by prompt, and the conversation stays cumulative
            because nobody is blind.
            <br />
            <br />
            <strong>All talk</strong> — everyone is asked every round against the state as it
            stood when the round OPENED, so none of them can see the others' contributions
            that round. Genuinely concurrent, and measurably more parallel: the one live run
            has four personas opening a round by answering the same question, none of them
            acknowledging the others.
            <br />
            <br />
            <strong>Hybrid</strong> — all-talk rounds to open, then moderated. The two live
            runs failed in opposite directions: all-talk opened superbly and degenerated into
            restatement by round 3, while moderated takes 8–11 turns to introduce the cast but
            stays cumulative.
            <br />
            <br />
            In every method except moderated, anyone with nothing to add passes and a pass
            never reaches the transcript, and a round where everybody passes ends the run.
            Rounds cost N voice calls instead of one, and the cost per surviving turn RISES as
            the room quietens — a round where five of six pass still costs six calls. Only
            moderated has numbers behind it; the rest are new.
          </Hint>
          <select
            value={method}
            onChange={(e) => setMethod(e.target.value as Method)}
            className="rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
          >
            <option value="moderated">Moderated — a model picks one speaker per turn</option>
            <option value="rotation">Rotation — everyone once per round, in order</option>
            <option value="simultaneous">All talk — everyone at once, blind to each other</option>
            <option value="hybrid">Hybrid — all talk to open, then moderated</option>
          </select>
        </label>
        {method === 'hybrid' && (
          <label className="flex items-center gap-2 pl-6 text-sm text-slate-300">
            Opening rounds
            <Hint label="opening rounds">
              How many all-talk rounds before the moderator takes over. Two by default,
              from the one live all-talk run: round 1 put every position on the table,
              round 2 was the strongest of the eight, and the parallel restatement — four
              personas answering the same question — starts at round 3.
              <br />
              <br />
              The moderator inherits those turns, so when it takes over everyone already
              has an equal share on the board, which is the state the fairness prompt
              spends tokens trying to reach.
            </Hint>
            <input
              type="number"
              min={1}
              max={10}
              value={openingRounds}
              onChange={(e) => setOpeningRounds(Number(e.target.value))}
              className="w-16 rounded border border-matrix-border bg-matrix-bg p-1 text-sm"
            />
          </label>
        )}
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={closingRound}
            onChange={(e) => setClosingRound(e.target.checked)}
          />
          Closing round when the ceiling is reached
          <Hint label="closing round">
            A run that hits its turn ceiling stops mid-argument. With this on it gets one
            extra round — asked of everybody at once, in either method — for final positions.
            <br />
            <br />
            It asks each persona to state where they now stand, what they can accept from
            what others proposed, and what they cannot accept and why. It deliberately does
            NOT ask them to reach a consensus: the Phase 6 work measured the same sentence
            moving a persona's behaviour from 0.000 to 0.333 on framing alone, so an
            instruction to agree would produce agreement every time and nothing would
            separate a real resolution from a manufactured one.
            <br />
            <br />
            Costs one extra round. Never runs after a conversation that ended on its own —
            everyone had already said they had nothing to add.
          </Hint>
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={stopWhenConverged}
            disabled={method === 'rotation' || method === 'simultaneous'}
            onChange={(e) => {
              const on = e.target.checked
              setStopWhenConverged(on)
              if (on) {
                setTurnsBeforeCeiling(maxMessages)
                if (maxMessages < CEILING_TURNS) setMaxMessages(CEILING_TURNS)
              } else if (turnsBeforeCeiling !== null) {
                setMaxMessages(turnsBeforeCeiling)
              }
            }}
          />
          End when the conversation is finished
          {(method === 'rotation' || method === 'simultaneous') && (
            <span className="text-[11px] text-slate-500">
              (automatic — a round where everyone passes ends the run)
            </span>
          )}
          <Hint label="stop when converged">
            The moderator picks the next speaker every turn; with this on it may also say
            nobody has anything substantive left, which ends the run. Measured on one run:
            32 turns of a 40 ceiling, zero turns of "confirmed, nothing to add", 23% cheaper
            than the same conversation padded to 40.
            <br />
            <br />
            Two guards, because stopping early is worse than stopping late: it cannot fire
            until every persona has spoken at least once, and it needs the moderator to
            decline twice in a row.
            <br />
            <br />
            Still being validated — one live run on one cast — so it is off by default and
            the turn ceiling is what stops a run that never converges.
          </Hint>
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input type="checkbox" checked={avatars} onChange={(e) => setAvatars(e.target.checked)} />
          Generate avatars
          {runType === 'ensemble' && !avatars && (
            <span className="text-[11px] text-slate-500">
              (off for ensembles — they would be redrawn once per conversation)
            </span>
          )}
          <Hint label="generate avatars">
            Anime-style portraits for each persona, generated once before the run via
            Stability SD3.5 on Bedrock. Purely cosmetic and entirely optional: it adds an
            image call per persona, and if it fails or no image provider is configured the
            cards fall back to initials without affecting the run.
            <br />
            <br />
            <strong>Off by default for an ensemble.</strong> Avatars are generated per run, so
            the same cast's faces would be drawn once for every conversation — and image spend
            is not counted in a run's reported cost, only voice calls are, so it would be both
            multiplied and invisible. Still switchable: turning it on applies to every member
            equally, so nothing about the comparison changes either way.
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
        <CastTemplates
          getCast={buildCast}
          hasCast={cast.some((c) => c.name.trim() || c.persona.trim())}
          onLoad={(loaded, warnings) => {
            setCast(loaded)
            setImportWarnings(warnings)
          }}
        />
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

                <div className="mt-3 flex items-center gap-2">
                  <span className="text-xs font-semibold text-slate-400">
                    Knowledge bases — this persona only
                  </span>
                  <Hint label="persona knowledge bases">
                    A collection indexed ONCE and searchable from any conversation that
                    binds it — unlike the pasted documents above, which belong to this run
                    alone. Bind one here and only this persona may search it.
                    <br />
                    <br />
                    A collection shared with you is bindable; that is what sharing is for.
                    If its owner revokes the grant before the run starts, creation is
                    refused and says which collection — and if they revoke mid-run, the
                    next turn simply stops searching it.
                  </Hint>
                </div>
                <div className="mt-1">
                  <KbPicker
                    level="persona"
                    personaName={p.name || `persona ${i + 1}`}
                    selected={p.knowledgeBases}
                    onChange={(ids) => updatePersona(i, { knowledgeBases: ids })}
                  />
                </div>
              </details>
            </div>
          ))}
        </div>
      </div>

      <div className="mt-6 rounded-lg border border-matrix-border bg-matrix-panel p-4">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-slate-300">
            Knowledge bases — the whole cast
          </h2>
          <Hint label="run knowledge bases">
            Bound at run level, so every persona may search these. The effective scope for
            a speaker is the run's collections plus its own, intersected with what you are
            actually allowed to read — and that intersection is re-checked on every turn,
            not just when the run is created.
          </Hint>
        </div>
        <div className="mt-2">
          <KbPicker level="run" selected={runKbs} onChange={setRunKbs} />
        </div>
        {(anyDocuments || anyKnowledgeBases || research) && (
          <label className="mt-3 flex items-center gap-2 text-xs text-slate-300">
            <input
              type="checkbox"
              checked={citeInline}
              onChange={(e) => setCiteInline(e.target.checked)}
            />
            Ask personas to cite their sources inline
            <Hint label="inline citations">
              Each persona ends a sentence that relies on a passage with its label, like
              [Cost observations #1], and the conversation view links it to the passage. Without
              this, personas rarely say which source a claim came from, so a claim can be traced
              only to the passages that were in view.
              <br />
              <br />
              Measured on a small comparison (docs/CITE-INLINE.md): every message with a source in
              view cited it, none cited a passage it was not given, and messages were no longer.
              Off by default until it has been tried on a larger conversation.
            </Hint>
          </label>
        )}
      </div>

      {/* Research sits next to the KB pickers on purpose: it is a knowledge-base AUTHORING
          step, and the bindings it produces are the same bindings chosen above. */}
      <div className="mt-6 rounded-lg border border-matrix-border bg-matrix-panel p-4">
        <label className="flex items-center gap-2 text-sm font-semibold text-slate-300">
          <input
            type="checkbox"
            checked={research}
            onChange={(e) => setResearch(e.target.checked)}
          />
          Research the subject before starting
          <Hint label="pre-conversation research">
            Before turn 1, a researcher searches the open web for the AUTHORITIES on your
            topic — statutes, regulations, board opinions, decided cases — and each persona
            researches their own position <em>and the evidence they said would change their
            mind</em>. What it finds is ingested into knowledge bases and bound here, so the
            conversation reads it like any other collection.
            <br />
            <br />
            This exists because of a measured gap: across five replicate runs of one brief,
            the room asked for the same missing citation every time and never got it. A
            conversation cannot answer that from inside itself.
            <br />
            <br />
            Sources are tiered — <strong>controlling</strong> (a statute, a regulation, a
            board ruling), persuasive, commentary — and a controlling one is given a
            reserved slot in every prompt, so it cannot be crowded out by a blog post that
            happens to match your topic's wording more closely.
            <br />
            <br />
            When nothing controlling is found, that is recorded <em>as a finding</em> and
            ingested too. "Nobody looked" and "we looked and there is nothing" are different
            facts, and only the second one is reusable.
            <br />
            <br />
            It costs a few minutes and roughly $0.25 for a six-persona cast, before the
            conversation itself. You are not made to wait on a screen: the run is created
            immediately and researches before it talks.
          </Hint>
        </label>

        {research && (
          <div className="mt-3 space-y-2 border-l-2 border-matrix-border pl-3">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                aria-label="Shared research"
                checked={researchShared}
                onChange={(e) => setResearchShared(e.target.checked)}
              />
              Shared research — one corpus every persona can search
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                aria-label="Per-persona research"
                checked={researchPersonas}
                onChange={(e) => setResearchPersonas(e.target.checked)}
              />
              Per-persona research — their case, and the case against it
              <Hint label="per-persona research">
                Two queries per viewpoint: one for the position, one for whatever the persona
                declared would change their mind. The second is not separately switchable,
                and that is the point — a persona who only ever sees support for what they
                already think cannot be moved by evidence, and measuring whether they would
                be is what this tool is for.
                <br />
                <br />
                A persona with no viewpoints is skipped rather than given an empty
                collection.
              </Hint>
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              Sources per query
              <input
                type="number"
                aria-label="Sources per query"
                min={1}
                max={20}
                value={researchResults}
                onChange={(e) =>
                  setResearchResults(Math.max(1, Math.min(20, Number(e.target.value) || 1)))
                }
                className="w-16 rounded border border-matrix-border bg-matrix-bg px-2 py-1 text-sm"
              />
              <Hint label="sources per query">
                How wide each search goes. Higher finds more and costs more, in both search
                calls and the reading that follows.
                <br />
                <br />
                There is deliberately no date filter. A case decided in 2011 is not stale and
                a blog from 2011 usually is — which is a question about what a source IS, not
                when it was written, and the authority tiers answer it better than a cutoff
                would.
              </Hint>
            </label>
            {!researchShared && !researchPersonas && (
              <p className="text-xs text-amber-400">
                Both tiers are off, so nothing would be researched.
              </p>
            )}
          </div>
        )}
      </div>

      <CostForecast forecast={forecast} loading={forecastLoading} error={forecastError} />

      <button
        onClick={submit}
        disabled={submitting}
        className="mt-3 w-full rounded-lg bg-matrix-accent py-3 font-semibold text-matrix-bg hover:bg-sky-400 disabled:opacity-50"
      >
        {submitting
          ? 'Starting…'
          : runType === 'ensemble'
            // Names the multiplier on the button itself. Five conversations is five times the
            // spend, and a button reading "Run simulation" would not say so at the one moment
            // it matters.
            ? `▶ Run ${replicates + (compareHybrid ? hybridReplicates : 0)} simulations`
            // Same principle for research, per §7: it is 1 + N corpora, and the count is the
            // thing that scales with the cast. "Research 7 collections, then run" is a
            // sentence an operator can decline; "Run simulation" is not.
            : research && researchCollections > 0
              ? `▶ Research ${researchCollections} collection${
                  researchCollections === 1 ? '' : 's'
                }, then run simulation`
              : '▶ Run simulation'}
      </button>
    </div>
  )
}
