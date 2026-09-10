# Changelog

All notable changes to TheMatrix Simulation Studio are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Pre-work for the AWS port (`docs/AWS-IMPLEMENTATION-PLAN.md` Phase 0), plus one live
bug the investigation turned up.

### Added
- **Tenancy in the domain model** (plan item 0.2). Every run now has an `owner_sub`,
  and no route serves another user's run. There is no authentication yet — that is
  Phase 1's Cognito pool — but the ownership model, the storage scoping and the route
  authorisation are complete and tested. `AUTH_MODE` selects where the identity comes
  from: `single-user` (the default; one implicit local user, which is what this tool
  has always been) or `jwt` (verified API Gateway claims required, unauthenticated
  requests are 401). Verified claims win in *both* modes, so putting an authorizer in
  front starts attributing runs correctly even if the setting is forgotten — the
  alternative would merge every user's history into one bucket while appearing to work.
  - Run-name uniqueness is now **per user**. It was global, so two people could not
    both have a run called `trusted-robot` — and the collision would have been with a
    row the second user cannot see. The migration drops the old global index
    explicitly; leaving it would have defeated the new one silently.
  - `owner_sub` is a **required keyword-only argument** on every read
    (`get_run_by_ref`, `list_runs`, `name_exists`, `get_run_tree`, `list_branches`), so
    a caller that forgets it raises `TypeError` instead of quietly returning another
    tenant's data. It is defaulted on `create_run` alone, where a forgotten owner fails
    closed (a run nobody can read) rather than open.
  - "Not yours" and "does not exist" are the same 404. Run refs can be memorable names
    from a small generated vocabulary, so a 403 would be a guessable oracle for whether
    a given run exists under another account.
  - Existing databases migrate in place: pre-tenancy rows are adopted by the local
    user rather than left with a NULL owner, which would have been indistinguishable
    from data loss. Verified on the real 38-run database.

### Fixed
- **Vector-only retrieval went silent instead of degrading**, in the two commonest
  cases. `retrieve_for_turn` had a lexical fallback whose stated intent was "degrading
  beats going silent", but it was nested inside the `vec_available` guard as an `elif`
  on the query-embedding branch — so it was unreachable when the optional `sqlite-vec`
  extra is absent (the default for any install that did not opt in) and when documents
  were attached but never embedded. Measured: `mode="vector"` returned zero passages in
  both, where `mode="fts"` over the same corpus returned matches, and the persona then
  announced it had no background material while its documents sat there. The fallback
  now covers every cause and logs which one fired; it deliberately does *not* fire when
  the similarity floor rejected the matches, since that is a configured decision rather
  than a missing capability.

### Changed
- `docs/AWS-IMPLEMENTATION-PLAN.md` item 0.3 (make vector retrieval the default) moved
  to Phase 3, where SQLite and FTS disappear anyway. Flipping the default today would
  change the behaviour of a working product for a benefit that only materialises after
  the port — and, until the fix above, would have exposed every install without the
  optional extra to the silent-retrieval bug.

## [0.6.0] - 2026-09-09

Interface and correctness, not new engine capability. Phase 6 became visible and
authorable, knowledge bases became uploadable, and two silent correctness bugs were
found and fixed — one of which invalidates the absolute values in the project's own
prior retrieval measurements.

### Added
- **Stop a live run.** `POST /api/runs/{ref}/stop` and a **■ Stop** button. A request
  polled between turns rather than a cancellation: the turn in flight finishes and is
  persisted, because those tokens are already spent and cancelling mid-call would also
  leave a partial turn for a later resume to trim. New terminal `stopped` status —
  resumable in place like `interrupted`, but distinguishable from it, so a run list
  still says whether someone chose to end a run or the process died. No auto-summary
  on a stopped run: stopping is a request to stop spending.
- **Upload files as a persona's knowledge base** (`.txt`, `.md`, `.pdf`, `.docx`).
  `POST /api/documents/extract` extracts text and stores nothing, so an uploaded file
  becomes an ordinary `document_texts` entry and rides the existing create-run,
  retrieval and setup-export paths. Run-agnostic by design: a knowledge base must be
  authored *before* the run exists, since cast documents are ingested ahead of turn 1.
  The extracted text is shown for review before use, which matters for PDFs where
  extraction quality varies and a scanned page yields nothing.
  `GET /api/documents/formats` reports what this install can actually read, so the
  picker offers only what will work and names the missing package otherwise.
  Server-enforced caps: `MAX_UPLOAD_BYTES` (10 MB, checked while streaming) and an
  independent `MAX_DOCUMENT_CHARS` (400k, because a small PDF can expand enormously);
  both refuse rather than truncate, since a half-loaded knowledge base looks complete.
- **Start a fresh conversation from an existing run's setup.** `GET /api/runs/{ref}/setup`
  returns the run's definition shaped as a create-run body — the same schema the setup
  importer already reads, so a setup round-trips through either path and the two cannot
  drift. Loaded into the new-run form for editing; submitting creates a fresh ROOT run
  with nothing from the transcript.
- **Import a conversation setup from JSON**, augmented with convictions, plus a
  **persona wizard** that drafts a cast of stakeholders from a one-line brief. Both are
  authoring assistance: they fill the form, and nothing they produce starts a run by
  itself.
- **Convictions are rendered in the dossier**, so Phase 6 is no longer invisible; and
  the new-run screen exposes convictions, documents and per-persona concerns, with an
  explanation on every optional switch.
- `underlying_concern` is persisted as a dedicated field rather than dropped at submit.

### Fixed
- **BM25 scores were computed over the whole database, not the run.** `bm25()` is
  evaluated by FTS5 over the index it is handed, and `run_id` was only an outer filter
  on already-scored rows, so a run's retrieval scores moved when unrelated runs were
  added. Measured: -0.0000 with a run alone in the database, **-1.8331** once an
  unrelated second run existed; on the real 37-run database the same query scored
  -2.8631 scoped versus -2.3807 whole-database. Now scored in a scratch FTS index over
  the run's slice with the same tokenizer — the same ranking function over a corrected
  corpus, pinned by a test asserting the two agree exactly when the database holds one
  run. Overhead +0.34 ms at 30 chunks, +7.30 ms at 1500, against a model call of 1000 ms
  or more. **Consequence: the absolute values in this project's prior Phase 5 retrieval
  measurements are not reproducible as recorded**, since they were gathered against a
  database that grew between runs.
- **A relative `DATA_DIR` resolved against the working directory.** Starting the server
  from a subdirectory silently created a second, empty database; the UI truthfully
  reported no previous conversations while 37 runs sat untouched in the real file — a
  cwd mistake was indistinguishable from data loss. Now anchored to the checkout root,
  and startup logs the absolute path plus the run count, at WARNING when it created the
  file.
- **Terminal events other than `sim.completed`/`sim.failed` left the UI showing a live
  run** — Stop still offered, thinking indicator stuck, analysis hidden until reload.
  `sim.capped` had that hole since Phase 3, so a cost-capped run has been reading as
  still running. A capped or stopped run can now also be scrubbed and asked about,
  which previously required a clean completion.
- **Truncated summaries.** The analyst summary shared the per-turn token budget (2048)
  and overflowed, so a strict parse returned nothing and the UI showed an empty summary.
  Own budget (`SUMMARY_MAX_TOKENS`, 8000) plus partial-JSON recovery.
- **A blank page after a frontend rebuild**, from a cached `index.html` pointing at
  deleted asset hashes. No-cache shell, immutable hashed assets.
- **Order-dependent test isolation.** `clean_env` cleared the `.env`-derived variables,
  then a fixture below it imported litellm, whose `load_dotenv()` put them straight
  back. The full suite passed only because litellm was already imported by collection
  time; a single file failed. Whether a test saw code defaults or local config depended
  on which other files were selected.

### Changed
- The scrubber has **one branch button** with the change options always visible, rather
  than "Branch" and "Intervene" presented as rival actions when they are one operation.
- Cognition is **on by default** in the new-run form, with every sub-feature. A run
  without it cannot answer "why did it say that?", which is the point of the tool. The
  engine default stays off, so programmatic and CLI runs are unchanged.
- `python-multipart` is now a core dependency: uploading a `.txt` needs nothing else,
  so gating the base case behind an extra was arbitrary.
- **One authoritative version.** `matrix-studio --version` carried its own literal and
  reported `0.1.0` through four releases; `__init__.py` carried a third copy. The
  runtime version is now read from the installed package metadata, so it cannot drift
  from `pyproject.toml`.

### Decided
- **Cast-wide documents: will not implement for now.** The narrow legitimate case
  (shared material too long for the topic prompt) does not justify it, and the cost of
  the workaround was measured rather than argued: duplicating a shared document across
  8 personas collapses its BM25 score from -4.2821 to -0.0000 and takes the persona's
  own private document down with it, because the shared text then occupies 8 of 16
  chunks. Reasoning and numbers kept in `docs/BACKLOG.md` so the decision is
  revisitable.

### Testing
709 Python tests (was 609 at v0.5.0) and 122 frontend tests (was 18) — both baselines
measured by checking the tag out, not taken from a commit message. Every fix above is
mutation-tested — the guard is reverted and the suite must fail. Two tests in this
release initially passed against the bug they were written for and were rewritten:
the model-clobber race needed the model list to resolve *after* the setup, and the stop
feature needed a test that stops a *resumed* run.

## [0.5.0] - 2026-09-06

Phases 5 and 6. Two capabilities, and a measurement discipline applied to both:
every behavioural claim here was checked against a live model, and the ones that
did not survive are recorded as withdrawn rather than quietly dropped.

### Added - Phase 5 (Per-Persona Document Retrieval)
- **Attach background documents to a specific persona** without the document sitting in the prompt context on every call — the problem the phase exists to solve. `documents: ["./background/spec.pdf"]` on a cast member, ingested at run start; PDF and Word via optional `[documents]` extra, `.txt`/`.md` need nothing installed. New `matrix_studio/documents.py` (extraction + paragraph-aware chunking; pure, synchronous, no LLM and no network, so ingestion costs nothing).
- **`max_chars` is the feature, not a safety valve.** A hard per-turn ceiling on retrieved document text is what keeps a forty-page attachment out of the per-call context. Off by default (`config.retrieval`): a run with no retrieval block never queries the index and never adds a prompt block.
- **Three retrieval modes.** `fts` (default) is SQLite FTS5 / BM25 — lexical, needs no embedding provider. `vector` and `hybrid` need the optional `[vectors]` extra (`sqlite-vec`) and an embedding provider; `hybrid` fuses the two by **Reciprocal Rank Fusion**, by *rank* rather than score, because BM25 (negative-better) and L2 distance (smaller-better) are not comparable in magnitude.
- **The index lives in the same SQLite file as the event log.** Chosen on atomicity — index and events commit in one transaction, one file to back up — over FAISS or a vector service. Measured justification in `docs/PHASE5-RETRIEVAL-DESIGN.md`: at 10³-10⁴ chunks exhaustive search costs 0.57 ms against a 4-7 s turn, so an ANN index optimises something already free at this scale.
- **A rebuild path, raised unprompted by all three premise-validation arms as a hard prerequisite.** `POST /api/runs/{ref}/documents/reindex` and `matrix-studio docs reindex` rebuild the lexical index from `doc_chunks` in one statement. An index is a cache; a second representation of state will drift from the event log.
- **Retrieval inspection endpoint** (`GET /api/runs/{ref}/documents/search?q=…`) returning the sanitised query, passages and scores. This is the measurement instrument: it makes retrieval quality checkable without running a simulation.
- **Causal document refs.** Retrieved passages are the turn's `document_refs` and are recorded in a `document.retrieved` event with the query and passages, so what a persona drew on can be *audited* rather than trusted. Same discipline as `memory_refs` (2c) and `thread_refs` (4b).
- **Unsupported-claim disclosure** (`disclose_unsupported`, off by default). When retrieval ran and found nothing, the persona is asked to say in its own voice that it is speaking from experience rather than a source. Deliberately about **provenance, not evidentiary support**: the engine only knows nothing was retrieved, and at ~0.82 recall the supporting passage may exist and have been missed, so "no documentation supports this" would be wrong ~18% of the time.
- **Citation provenance** (`matrix_studio/citations.py`), modelling **attributed hearsay rather than suppression**. Fixes an observed real failure: one persona lifted another's document label out of the transcript and asserted a technical claim from a document it had never had access to. A citation is legitimate when first-hand (retrieved this turn) or second-hand (credited to a participant who really did cite it); anything else attributive is `unverified`. Forbidding second-hand citation outright would destroy information the discussion needs — evidence legitimately travels through people. Added as a `citation_integrity` principle in the Phase 4a gate, and `citation_provenance` on the response event so an evidence chain is machine-readable.
- **New CLI subcommand** `matrix-studio docs attach|list|search|reindex|embed`. **New dossier fields** `documents` and `document_retrievals`, sourced only from real events. **New docs:** `PHASE5-RETRIEVAL-DESIGN.md`, `PHASE5-RETRIEVAL-MEASUREMENT.md`, `PHASE5-PREMISE-VALIDATION.md`, `BACKLOG.md`.
- **Documents survive a fork.** A branch inherits the parent's attachments, copied rather than shared, so deleting a parent's document cannot alter a recorded branch.

### Measured - Phase 5
- **FTS5 alone was measured as inadequate** for the engine's real query shape (recall@5 0.40-0.51), which is why `vector` exists. Vectors: recall@5 **0.817**, recall@1 0.017 → 0.367, at $0.0014 per corpus.
- **Two free query-side fixes were tried first and both measured HARMFUL** — recall fell on all three arms, worst on the arm each targeted. Retained but default off (`term_limit=0`, `score_ratio=0`) with the negative result in their docstrings, so they are not re-enabled on intuition.
- **The absolute score floor is an off-topic guard, not a relevance filter**, and the distinction is measured over 180 retrievals: correct matches span cosine 0.228-0.870 and incorrect 0.166-0.699 — near-total overlap, so no threshold separates a right passage from a wrong one. Shipped at `min_similarity: 0.15`, below the lowest observed genuine hit, costing 0 of 137 measured hits. A test locks the default.
- **A chunker defect was found by reading cited passages**, not by a metric: 90% of chunks in one document began mid-sentence, and a persona quoted a fragment verbatim and inferred the *opposite* of the source's meaning. Sentence-aligned overlap fixed it (fragments 90% → 1%), and re-labelling showed 0 clear misreads in 28 — which is why entailment checking was **deferred with evidence** rather than built.

### Added - Phase 6 (Structured Personas)
- **Convictions, not just goals.** Cast members may carry a `structured` block — `background.formative_events[].lesson`, `preferences.optimises_for` / `dismisses` / `persuaded_by`, and `viewpoints[]` with `position` / `formed_by` / `firmness` / `evidence_that_shifts` / `underlying_concern`. Goals are *satisfiable*, so a persona holding one can be talked into any plan satisfying it; convictions are *defended*. Additive to the `persona` prose string, never a replacement. New module `matrix_studio/personas.py` (pure models + rendering: no LLM, no database, no state), new `AgentState.structured`, new `PersonaConfig` (`config.personas`). Off by default: with `enabled` false a `structured` block is ignored and prompts are **byte-identical** to pre-Phase-6, asserted by diffing real prompts rather than inspecting the renderer.
- **`underlying_concern` is withheld, enforced by the code path.** Two renderings: `render_private()` for the speaker's own system prompt, `render_public()` (role + `optimises_for`, one line) for the moderator's persona list. The moderator prompt is the one place every persona appears at once, so rendering the private block there would put each withheld concern one prompt away from the whole cast — and drawing that concern out is the exercise. Also stripped from the `persona.structured` event and the dossier API (both are exported/rendered surfaces). Tests scan *every* prompt in a real run.
- **`validity` reaches no prompt at all** — an operator calibration note (are the firmest positions also the soundest? they should not be), used only for post-run scoring. `tests/test_examples.py` asserts the shipped example stays calibrated.
- **A re-tuned dismissal rule**, which was the premise validation's explicit ship condition, because Arm C's naive "judge only against your own priorities" rule degraded discussion into repetitive parallel monologues (talking-past 4/5, within-speaker similarity +37%). *(The first re-tune shipped here was later measured as suppressing dismissal to the control's rate and replaced — see "Fixed - Phase 6 dismissal rule" below. Both wordings are retained as named variants so the negative result stays reproducible.)*
- **`requires-escalation` firmness** kept rather than narrowed away: it says the speaker lacks the *authority* to concede, so being overruled produces "I'll have to take this further", not agreement — the one firmness level giving a persona something honest to do other than agree or repeat itself.
- **A defended position with no exit condition is named as such** in the prompt ("you have not named anything that would change your mind … do not invent a condition you do not have"), because silence lets the model either stonewall or fabricate. Found by reading a real rendered prompt.
- **New event:** `persona.structured` (turn 0, per cast member with structure, emitted only when the feature is on). **New dossier field:** `structured`. **New example:** `examples/structured-personas.json` — the validated Arm B cast in the shipped schema. **New doc:** `docs/PHASE6-STRUCTURED-PERSONAS.md`.
- Convictions survive a fork (`reconstruct_at_turn` seeds `structured` from the stored cast, private fields included) and an in-place resume.

### Changed
- An unknown `firmness` is **rejected**, not silently downgraded — 422 at the API boundary (`PersonaModel.structured` is typed as the real model, not a loose dict), raised at run start from the CLI. The block is parsed even when the feature is off, so a typo surfaces immediately rather than the day someone enables the flag.

### Measured - Phase 6 (Arm D, live, 2026-09-06)
- **Fourth validation arm** added to `scripts/build_validation_arms.py` and `scripts/score_validation.py`: `arm-d-shipped` is the control prose + a `structured` block + `personas.enabled`, deliberately *not* Arm B's rendered persona string (reusing that would measure the hand-written prose and the engine's rendering at once). Arm D is the only arm that legitimately differs in `config`, so the byte-identity test now enumerates its two permitted extra keys by set difference rather than skipping it. Scored when present, skipped when absent, so the original three-arm comparison stays reproducible.
- **Every quantitative claim from this single run was subsequently WITHDRAWN at n = 3** — see "Measured - Phase 6 at n = 3" below. Retained here only as the record of what one run appeared to show: evidence-driven position change 2 of 2, more divergent than the prose control, less divergent than Arm B. The first two did not replicate; the third is below the noise floor. Arm C's parallel-monologue failure genuinely did not reproduce (talking-past 2 vs C's 4), and that has held up.
- **Two properties could not be tested.** `requires-escalation` never fired because the room never overruled the persona holding it. The withheld concern was never drawn out because nobody asked why — zero verbatim leaks (withholding works), zero "why" questions in 15 turns. The second is a **design finding**: nothing in a run creates pressure to ask a stakeholder why, so `underlying_concern` may be inert in practice.
- Cost: $0.0719 run + $0.0207 judge. Six follow-ups in `docs/BACKLOG.md`, led by "repeat at several seeds" — `n = 1` on a non-deterministic model.

### Fixed - premise-validation scorer (measurement instruments)
- **Length bias in the headline similarity metric.** Every overlap metric on accumulated text grows with volume: the same arm truncated to 300-char turns scores 0.105 and at full length 0.160, with identical speakers and positions. `normalised_similarity` now truncates every speaker to a common token volume before comparing. Three other fixes were tried and rejected first, each recorded in the scorer with the measurement that killed it: stream subsampling (equalises count, not vocabulary size), vocabulary subsampling (worse), and TF-cosine (also length-sensitive).
- **A too-aggressive robustness check.** The first budget-sensitivity sweep included 40-80 token budgets, where arm ordering scrambles completely; eighty content tokens is a couple of sentences. Including them reported *every* pair as uncallable and hid a real result. Now floored at 100 tokens. The scorer reports which orderings survive the budget and refuses to rank the rest.
- **`DISMISSAL` phrase list missed bare-possessive idiom** ("not mine", "their job to own", "your call to make … not mine"). Arms B and D were **hand-labelled first** (`docs/labels/dismissal-labels.json`), then patterns patched until they reproduced the reading — that order matters, because tuning patterns against a number rather than a reading is how the defect got in.
- **Repeat-run support and a noise gate.** The scorer now discovers `<arm>.run2.json`, `.run3.json`, reports per-arm within-arm spread, and **refuses to call any between-arm gap smaller than one arm's own run-to-run variation**. Without that, more runs only produce more confident nonsense. With n = 1 it says so explicitly instead of ranking arms.
- `tests/test_validation_scoring.py` locks all of the above, including a test asserting the raw metric IS length-biased, so the normalisation cannot be quietly dropped as redundant.

### Measured - Phase 6 at n = 3 (2026-09-06): behavioural case NOT established
- Arms B and D were each run **three times**. Within-arm spread (B cross-speaker 0.1117-0.1289, spread 0.0172) turned out to **exceed most between-arm gaps**.
- **Below noise, therefore withdrawn as findings:** divergence vs the control (gap 0.0133), divergence vs Arm B (0.0123), accommodation rate, citation rate, and mean turn length. Even "Arm D writes 37% longer turns" — the observation the length-normalisation work was built around — is within run-to-run variation.
- **Did not replicate:** "first arm to produce evidence-driven position change, 2 of 2" was a single run; runs 2 and 3 produced zero. It was the strongest argument for the feature.
- **One callable finding, and it is negative:** the re-tuned dismissal rule **suppressed dismissal** to 0.067 across three runs against Arm B's 0.355 (gap 0.289 > noise 0.200), with **two of three runs containing no dismissal of any kind** — verified by a deliberately broad idiom sweep, not the patched regex. 0.067 is the control's rate, so the retune appears to have given back the five-fold gain that made `dismisses` the premise validation's highest-value field. Confounded between the rule wording and the rendering; isolating it needs a variant the current `dismissal_rule` flag cannot express.
- **Unexplained:** `distinct_positions` fell to 3 in Arm D runs 2 and 3 against 5 in every other run ever scored — the only signal pointing at a real downside of rendered structure.
- **What remains sound and tested:** the schema and its honesty properties — withholding with zero leaks, per-persona scoping, convictions surviving a fork, invalid `firmness` rejected, off by default.
- **Resolution floor for this harness:** at 15 turns and 5 personas it cannot resolve differences below ~0.02 in cross-speaker similarity or ~0.2 in the rate metrics. Several previously published conclusions, including the original experiment's headline 0.183-vs-0.160, sit inside that band.

### Fixed - Phase 6 dismissal rule (pre-registered, measured 2026-09-06)
- **`dismissal_rule` is now a named variant** — `mandatory` (default) | `retuned` | `blunt` | `off` — instead of a boolean, because separating "the wording is wrong" from "the rendering is wrong" requires emitting different wordings against the same structured data. Booleans still accepted (`True → "mandatory"`, `False → "off"`); an unknown name raises rather than falling back, since a typo'd variant would make a measurement arm quietly test the wrong wording.
- **The new default passes a pre-registered two-condition criterion.** `docs/PHASE6-DISMISSAL-RETUNE.md` was committed **before any candidate wording existed** (`6927607`), because the previous round's error was choosing what counted as success after seeing output. Results: dismissal rate **0.333** across three runs (Arm B is 0.355) with **no run at zero**, and talking-past **1.00** — the best engagement score of any arm measured, and the only arm scoring 1 on all three runs. Both conditions met.
- **The transferable finding, from four data points:** *a rendered persona instruction must **require an utterance**, not license an omission.* Arm B's *"Ignore the things you consider not your problem"* yields a 0.355 dismissal rate in hand-written prose and **0.000 across three runs** through this renderer — identical words. `retuned`'s *"say once, briefly"* yields 0.067. `mandatory`'s *"you MUST say plainly … every time it comes up … not optional"* yields 0.333. Arm B's prose got away with a permission only because it wrapped that sentence in a block of conduct imperatives supplying force the sentence lacks alone. A test asserts the shipped rule still demands rather than permits.
- **New arms E (`mandatory`) and F (`blunt`)**, generated from the same script and verified to differ from Arm D *only* in `config.personas.dismissal_rule`. `blunt` is the isolation arm; its total failure is what proved the rendering is not a blanket blocker while Arm B's exact wording is. Both failing wordings are retained verbatim and locked by tests so their negative results stay reproducible.
- **Fixed an engine bug the new tests caught:** `dismissal_rule=bool(personas.dismissal_rule)` — a leftover from the boolean field — collapsed every variant to the default wording. Silent: an arm would have run, produced numbers, and tested nothing.

### Fixed - two shipped features were silently broken against real models
Both found by running cognition for the first time. Both fail **silently** — the run reports `complete` — which is why neither surfaced earlier, and why `PHASE4-REPORT.md` §4's "unmeasured against a live model" was hiding them.
- **Strict JSON parsing made cognition completely inert.** Claude Haiku 4.5 wraps structured output in a markdown fence; `json.loads` rejected it and `_generate_response` fell through to its degradation path. Measured over 30 turns and five personas: **0 memories, 0 reflections, 0 rationales, 0 `goal_served`**, all 30 transcript utterances were JSON blobs rather than speech, and the run cost **more** than not using cognition. Cognition has shipped since v0.2.
- **The same root cause silently disabled the Phase 4a validation gate.** Its LLM confirmation call catches broad exceptions and fails open, so every `JSONDecodeError` became `violation: False` — the selective confirmation had never confirmed anything against that model.
- **Provider parameter restrictions wrote error text into the transcript as dialogue.** `bedrock/global.anthropic.claude-sonnet-5` accepts only `temperature=1`; the engine passes 0.7 (settings), 0.3 (speaker selection) and 0.0 (validation gate, reflection). Every path raised `UnsupportedParamsError`, and the error string was stored as the character's speech — with the run reporting `complete` and **$0.0000 cost**, because no call had succeeded. Fixed with `litellm.drop_params = True`.
- New `matrix_studio/jsonio.py` — one tolerant `extract_json_object` used by all five call sites (engine ×2, validation gate, `analysis.py`, `naming.py`). Tries the whole string, then a fenced block, then the widest `{...}` span; never raises; returns `None` rather than a non-dict. `analysis.py` and `naming.py` had each solved this independently while the engine and the gate never learned it — that duplication is why the lesson did not spread, so there is now one implementation.
- The two defects are **independent and both required**: the JSON fix alone still breaks on Sonnet, `drop_params` alone still breaks on Haiku. `tests/test_jsonio.py` locks both, starting from the literal payload observed in the broken run, and asserts a strict `json.loads` **would** have failed on it so the tolerance cannot later be removed as unnecessary.
- Default model switched to `bedrock/global.anthropic.claude-sonnet-5`; Haiku 4.5 remains in `AVAILABLE_MODELS`.

### Measured - cognition × structured personas (2026-09-06)
First time cognition has been run in any measurement arm. Six runs at 30 turns on Sonnet 5, $2.588. Pre-registered before the arm existed (`docs/PHASE6-COGNITION-INTERACTION.md`).
- **The pre-registered correctness risk does not occur.** `underlying_concern` is withheld by the code path, but nothing stopped a persona *forming a memory* encoding it and having that memory injected into its own later prompts — a route no existing test covers, since they all check prompt construction rather than generated content. All **100** memories and reflections were **read**, not filtered: **zero leaks**. Each persona's memories reference its *condition*, never the private worry behind it, so the concern drives behaviour without being voiced. That is the design's central claim, holding under the condition most likely to break it. Consequently the defensive fix that had been sketched (stripping withheld content from memory formation) is **deliberately not built**.
- **Two of three predictions were wrong**, recorded as such: the predicted leak did not happen, and cognition did **not** raise the accommodation rate. Only "dismissal rate not materially changed" survived, and only as consistent-but-unproven.
- **One callable metric:** cognition makes turns **~30% shorter** (807 → 563 chars, gap 244 > noise 100). Cause unmeasured.
- **`requires-escalation` fired for the first time in fifteen runs** — 2 of 3 cognition-on runs, 0 of 3 without, counting only the persona holding the cast's sole such viewpoint. Plausible mechanism: escalation needs *sustained* pressure to concede, and without memory every turn starts fresh. **Suggestive, not established** (2/3 vs 0/3 at n=3 is Fisher ≈ 0.4, and the all-persona count is below the noise gate).
- **Reflections reinforce convictions rather than eroding them**, contrary to the second pre-registered worry.
- **Instrument caveat:** the `ACCOMMODATION` / `DISMISSAL` phrase lists were hand-validated against Haiku at 15 turns. Rates from this experiment must **not** be compared to the 15-turn numbers, and the pre-registered ≥0.30 dismissal criterion does not transfer across turn counts. Within-experiment comparisons are unaffected.

### Added - Import a conversation setup
- **Load a conversation from JSON on the new-run screen** — file picker or paste. The format is deliberately **not a new schema**: it is the create-run request body, so anything the API can run a file can express, and the two cannot drift. Only `topic` and each persona's `name`/`persona` are required.
- **Augmented beyond the bare shape** with everything the form can now author: `structured.viewpoints[]` (convictions with `firmness`, `evidence_that_shifts` and the withheld `underlying_concern`), `structured.preferences.dismisses`, per-persona `document_texts`, and top-level `config` / `name` / `description`. A `structured` block round-trips into the form's editable line format and back out to an identical payload.
- **Loads into the form rather than starting a run.** The setup files people actually have carry no convictions and no config; adding those before running is the point of importing rather than executing.
- **Tolerant on the way in, explicit about what it dropped.** A persona missing `name` or `persona` is skipped *with a warning naming it*; a duplicate name is skipped with a warning, because duplicates otherwise collide in the engine's agent dict and silently lose a persona at run start; `documents` (server file paths) are reported as unreadable-from-a-browser with the paths listed. Silent dropping was the failure mode being designed against.
- **Refusals name the actual problem** — the JSON parse error, or which required field is missing — rather than "invalid file".
- **New example:** `examples/import-augmented.json`, a complete eight-stakeholder setup generated *from* a bare one so the two cannot drift. Tests assert it stays calibrated (the firmest positions are not uniformly the soundest) and that all eight personas dismiss different things, which are the two properties that stop a panel converging.
- Tests are anchored on the operator's real sample file rather than an invented fixture, which is what surfaced that empty `goals` arrays and absent `config` are the normal case rather than the edge case.

### Added - Convictions panel in the agent dossier (Phase 6 UI)
- Structured personas were shipped but **invisible**: a run's convictions could only be read via the API or the event log. The Dossier drawer now renders them — role, each position with its firmness badge and named exit condition, `formed_by`, `optimises_for` / `will not weigh` / `persuaded_by`, and formative events with the lesson each taught. Defended positions are visually distinguished from negotiable ones.
- **A defended position with no exit condition is flagged as unfalsifiable**, because that is an authoring gap only the operator can fix and hiding it helps nobody. Mirrors the warning the engine already renders into the persona's own prompt.
- **The withheld concern cannot reach the panel.** `underlying_concern` and `validity` are stripped by the API, and the UI is a second line of defence rather than a consumer trusting that: a test feeds the component a payload that *does* contain both and asserts neither is displayed. That test is **mutation-verified** — deliberately rendering the concern makes it fail — so it is not vacuous. Drawing the real concern out in conversation is the whole exercise; an operator who can read it off a panel has been handed the answer.
- Verified end to end against a real run: engine → API (both private fields absent from the payload) → rendered panel. 22 frontend tests, up from 18.

### Verified - the two long-standing "never verified" gaps are closed
- **Frontend typecheck, build and tests, run for the first time.** Node 18.20.8 installed; `tsc --noEmit` typechecks **29 source files with zero errors** — including the Phase 5c and Phase 6 Dossier/types changes that had only ever been reviewed by eye — `npm run build` succeeds, and **all 18 vitest tests pass**.
- **Docker build verified for the first time in any environment**, having carried a "NOT been verified" warning in the README since Phase 3. The image builds (`matrix_sim_studio-0.5.0`) and the container serves end to end: `/api/health`, `/api/runs`, `/api/models` all 200, plus the built UI at `/` with the same JS asset hash as the local build. Docker was in fact already installed on the development machine — the blocker was that nobody had run it.
- **No credentials are baked into the image.** No `.env` inside it, and the only credential-shaped string in its filesystem is the README's placeholder `AWS_BEARER_TOKEN_BEDROCK=your_bearer_token`. Worth checking now that the image is something people may actually run.

### Still not established (Phase 6 behaviour, n = 3)
- Divergence, accommodation, citation rate and turn length differences are all **below** the harness's noise floor. Within-arm spread (0.0172 on cross-speaker similarity) exceeds the between-arm gaps. Even "Arm D writes 37% longer turns" — which motivated the length-normalisation work — is within run-to-run variation.
- The early "evidence-driven position change, 2 of 2" **did not replicate** (0 in both repeats; noisy across the rule arms too).
- **`distinct_positions` is unstable in every rendered arm** — E scored 5, 5, 3 and F scored 2, 2, 5, against Arm B's consistent 5, 5, 5. Third appearance of this signal, and the only one pointing at a real cost to rendering convictions from data rather than prose. Judge variance at n = 3 unknown; recorded, not concluded. Now the highest-value open Phase 6 question.
- `requires-escalation` and the concern-reveal path have had **no trigger in any of nine runs**.
- **Resolution floor:** at 15 turns and 5 personas this harness cannot resolve differences below ~0.02 in cross-speaker similarity or ~0.2 in the rate metrics.

### Not claimed
- The premise validation's null result is **unchanged**: Arm D also scored 5 distinct positions and 5/5 specificity. A fourth arm at the same ceiling is more evidence the metric cannot express a gain, not evidence there is none.

## [0.4.0] - 2026-07-19

### Added - Phase 4 (Deeper Cognition & Steering)
- **4a — Priority-hierarchy validation gate:** An enforced pre-emit consistency pass over each generated turn, keyed on the hierarchy (world coherence > causality > continuity > agent agency > character consistency > dramatic impact > novelty). Heuristic-first; a small LLM confirmation call is made only on suspected violations (cost control), fail-open on checker errors. A violating turn is regenerated once (`validation_retry_budget`, default 1), then emitted **verbatim** with a `validation.flagged` event — model output is never rewritten in place. New events: `validation.checked` (per attempt), `validation.flagged`. New settings: `validation_enabled` (default ON; OFF is byte-for-byte pre-4a, regression-locked by test), `validation_retry_budget`. The turn trace API now surfaces the validation trail.
- **4b — Pending-thread ledger (setups & payoffs):** First-class `pending_threads` state (`PendingThread` on `SimSnapshot`), opt-in via `config.cognition.threads`. Agents plant/resolve/abandon threads through the same single structured generation call (`thread_updates`); open threads are fed into subsequent turn prompts (causally real) and recorded as the turn's `thread_refs`. Resolutions are accepted only for threads genuinely in-context (no fabricated payoffs). New events: `thread.opened` / `thread.resolved` / `thread.abandoned` (lossless replay). The ledger rides every snapshot and survives branch/scrub/resume reconstruction. New API: `GET /api/runs/{ref}/pending-threads` with staleness flags (`thread_stale_after`, default 5 turns); dossier lists the agent's planted threads.
- **4d — Structured output view (optional):** `GET /api/runs/{ref}/turns/{turn}/structured` projects a turn's canonical events into Narrative / Consequences / Updated State / Possibilities. Narrative is verbatim utterances; every consequence/state line carries the `source_seq` of its backing canonical event; Possibilities are the open 4b threads (non-limiting); absent data is stated, never invented. Default OFF (`structured_output` setting or `?opt_in=true` per request); a derived read-only view — canonical events unchanged.
- **4c — Adaptive-pressure intervention (EXPERIMENTAL, opt-in):** New `adaptive_pressure` branch-mutation kind. Observes run-level signals at the fork (repetition, stale threads, remaining budget — real state only) and injects ONE narrator-voiced world event as a branch turn, preceded by a `pressure.applied` audit event. Hard agency guard (shared with 4a): pressure modulates the world only; generated text negating participant choice is regenerated once, then the whole intervention is rejected (HTTP 422) — nothing emitted, nothing rewritten. Default OFF (`adaptive_pressure_enabled`), refused at both API and engine layers while disabled. Marked experimental in README and UI.

### Changed
- Version bumped 0.3.0 -> 0.4.0 (new simulation capability).
- Branches / in-place resumes now carry the run's cognition config (and 4b thread ledger) forward; runs without a cognition config behave exactly as before.

## [0.3.0] - 2026-07-09

### Added - Phase 3 (Release Polish)
- **Cost guards:** Optional per-run hard spend cap (`max_run_cost_usd` setting, default 0 = OFF). Engine checks accumulated cost after each turn; when cap is reached, run ends in terminal `capped` status with `sim.capped` event. Additive, opt-in feature; cap=0 behaves byte-for-byte identical to pre-Phase-3.
- **Provider readiness check:** `/api/health` endpoint now returns per-provider has-key booleans (openai, anthropic, bedrock) for BYO-key setup UX. NEVER exposes key values.
- **Secret safety tests:** Comprehensive test suite asserting no `.env`-style credential values ever appear in any API response (health, models, runs, events).
- **Example templates:** 4 curated examples demonstrating different use cases:
  - `minimal.json`: Simple 2-person conversation
  - `debate.json`: 3-person debate on AI in creative work
  - `coffeeshop.json`: 4-person reunion conversation
  - `design-review.json`: 3-person design review with cognition enabled (showcases Phase 2c features)
- **Documentation:** Complete README rewrite describing shipped v0.3.0 tool (control room, branching, cognition, avatars), 5-minute quickstart (pip + Docker), full configuration reference, cognition/honesty note, usage guide, architecture.
- **Screenshot placeholders:** Labeled TODO markers for control-room and dossier screenshots.
- **Test mode:** `_MSS_TEST_MODE` env var to disable `.env` loading in tests (avoids permission issues in restricted environments).

### Changed
- Version bumped from 0.1.0 to 0.3.0 (feature-complete, pre-1.0 polish)
- Terminal events now include `sim.capped` (WebSocket closes on cost-cap-hit)
- README no longer says "Phase 1.5" — accurately describes all shipped features through Phase 2c + Phase 3

### Fixed
- Settings class now respects test mode to avoid `.env` permission errors in test environments

## [0.2.0] - 2026-07-09

### Added - Phase 2c (Agent Cognition)
- **Memory system:** Agents form, retrieve, and cite memories (importance-scored, tagged, timestamped). Top-K retrieval by importance + recency; retrieved items are the turn's causal `memory_refs`.
- **Reflection:** Periodic higher-level belief formation (every N turns, configurable via `cognition.reflection_every`). Emits `agent.reflected` events.
- **Dynamic goals:** Agents can update their own goals mid-run (via `goal_update` field in structured output). Emits `goal.updated` events.
- **Relationships:** Per-agent stance tracking toward other participants. Emits `relationship.updated` events.
- **Why-trace:** "Why did they say that?" — rationale + goal_served captured per turn when cognition is enabled.
- **Rich dossier API:** `/api/runs/{ref}/agents/{name}/dossier` returns full agent state (memory stream, goals, relationships, conversation history).
- **Trace API:** `/api/runs/{ref}/trace?turn={N}` returns the causal chain for a specific turn (speaker selection reason, retrieved memories, rationale, goal served).
- **Cognition config:** Per-run opt-in via `config.cognition` object (enabled, memory, reflection_every, goals_dynamic, relationships, retrieval_k). All default to pre-2c behavior when disabled.

### Added - Phase 2b (Interventions)
- **Inject message:** Branch from turn N and inject a message (user or narrator) into the conversation. Persisted as a real branch turn with `injected` flag.
- **Continue:** Extend the turn budget (`add_budget`) to let the group keep talking.
- **Edit goal:** Change a persona's goals at the branch point (in-place state mutation).
- **Add persona:** Introduce a new character mid-conversation (in-place state mutation).
- **Remove persona:** Remove a character from the cast (in-place state mutation, requires ≥1 persona remain).
- **Promote aside:** Promote an aside conversation's reply into the main timeline as an injected message, then continue the discussion.
- **Branch tree UI:** Visual branch tree in frontend showing parent/child relationships.

### Added - Phase 2a (Checkpointing & Branching)
- **Per-turn checkpointing:** Full `SimSnapshot` persisted after every turn (run_id, turn, topic, agents, conversation, status). Stored in `snapshots` table with `UNIQUE(run_id, turn)`.
- **Branch primitive:** Fork the event log at turn N → resume forward as a new run (new `run_id`, `parent_run_id`, `branch_turn` columns). Original run immutable.
- **Checkpoint scrubber/replay:** UI timeline scrubber to jump to any turn; reconstruct state by loading snapshot at that turn.
- **Resume simulation:** `resume_simulation` engine entry point continues from a checkpoint (distinct from fresh-start `run_simulation`).
- **Branch API:** `POST /api/runs/{ref}/branch` creates a new branch run from a parent at a specific turn.

## [0.1.5] - 2026-07-08

### Added - Phase 1.5 (Post-Run Analysis Layer)
- **Structured summary:** Auto-generated end-of-run summary (consensus, dissenters, key ideas, open questions, overview). Configurable field set + optional focus lens. On by default, persisted as JSON blob attached to run.
- **Aside conversations:** Read-only side-threads over completed runs (never mutate canonical timeline):
  - **Analyst mode:** Ask a neutral summarizer/analyst about the run.
  - **Persona mode:** Ask a specific persona (in-character, using their stored persona).
  - **Room mode:** Ask all personas (group response into the thread).
- **Thread model:** Scoped threads with target (analyst / persona / room) and mode (aside = read-only, contribute = mutating, Phase 2 feature). Data model anticipates Contribute from day one.
- **Summary regeneration:** `/api/runs/{ref}/summary:regenerate` endpoint to regenerate summary with different config (field set, focus, instructions).
- **Thread API:** `/api/runs/{ref}/threads` (list), `POST /api/runs/{ref}/threads` (create), `/api/runs/{ref}/threads/{id}/messages` (retrieve), `POST /api/runs/{ref}/threads/{id}/messages` (send).
- **Thread persistence:** Threads + messages stored in `threads` and `thread_messages` tables (Phase 1.5 schema extension).
- **Cost tracking:** Aside token/cost tracked separately from canonical run; summary cost added to `runs.summary_cost_usd`.

## [0.1.0] - 2026-07-08

### Added - Phase 1 (Control-Room Web UI)
- **Web UI:** Cast board with character cards (avatar + persona + goals), live-scrolling conversation feed, active-speaker highlight, cost meter, searchable run history.
- **New-run form:** Topic + cast builder, model picker (from `AVAILABLE_MODELS`), cognition config (Phase 2c forward-looking), avatar toggle, summary config.
- **Per-agent dossier:** Click a card to view that agent's full state (as rich as engine exposes; Phase 2c deepens this).
- **Live WebSocket stream:** `/api/runs/{ref}/stream` replays historical events then subscribes for live tail. Late joiners catch up automatically.
- **Run codenames:** Every run gets a memorable two-word codename (e.g., `trusted-robot`) via LLM-generated topically-resonant suggestions with random-wordlist fallback. Shown in UI, used as URL ref (`/api/runs/{codename}`).
- **Cost meter:** Live token/$ display in UI, accumulated per agent and per run.
- **UI-only playback:** Pause / resume / step / reveal-speed controls operate on client-side buffered stream (engine always runs to completion at full speed).
- **Avatar generation:** Parallel portrait generation via Stability SD3.5 on Bedrock (`stability.sd3-5-large-v1:0`). Mandatory initials/color fallback; avatars are optional eye-candy. Emits `avatar.ready` events as each finishes.
- **Model selection:** Per-run model override (picker in new-run form), persisted in `runs.config.model`.
- **FastAPI backend:** REST API + WebSocket, serves built React frontend as static assets from same process (one container, one port).

### Added - Phase 0 (Standalone CLI + Event-Sourced Storage)
- **Standalone CLI:** `matrix-studio run <file.json>` to run simulations headlessly; `matrix-studio serve` to start web server.
- **Provider-agnostic engine:** Hand-rolled async litellm loop (not AutoGen). Two-phase turn: (1) select speaker via LLM, (2) generate response via LLM. Supports any LiteLLM model (OpenAI, Anthropic, Bedrock, OpenRouter, Ollama).
- **Event-sourced storage:** Append-only event log + snapshots in SQLite (`./data/matrix_studio.db`). Tables: `runs`, `events`, `snapshots`.
- **Configuration:** Pydantic settings loaded from env vars or `.env` file (precedence: env > .env > defaults). Settings: `LITELLM_MODEL`, `LITELLM_TEMPERATURE`, `LITELLM_MAX_TOKENS`, AWS/OpenAI/Anthropic credentials, `MAX_MESSAGES`, `DATA_DIR`, `MATRIX_HOST`, `MATRIX_PORT`, avatar config.
- **Dockerfile:** Multi-stage build (Node stage builds React frontend, Python stage installs package + serves API+UI).
- **License:** Apache-2.0 (patent grant suits a customer-facing tool).
- **Test suite:** 186 backend tests (all mocked; no live LLM calls).

## [Unreleased]

### Planned
- Embedding-based memory retrieval (deferred from Phase 2c)
- Multi-modal inputs (images, audio)
- Hosted/multi-tenant deployment

---

**Note:** Phases 0-3 built 2026-07-07 through 2026-07-09 by MasterControl (orchestrated by CC). Each phase is a shippable increment; all existing tests remain green at every commit.
