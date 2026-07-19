# PHASE4-REPORT — TheMatrix Simulation Studio, Phase 4 build

**Spec:** `docs/PHASE4-REQUIREMENTS.md` (Status: APPROVED, working decisions baked).
**Build order:** 4a → 4b → 4d → 4c, each with its own tests + commit + push.
**Baseline before any Phase 4 code:** 207 backend tests passing, 18 frontend tests passing
(verified on this machine, commit 792b5aa).

---

## 1. Verified [verify] findings (read from code BEFORE writing any Phase 4 code)

### 1.1 `_generate_response` — exact names / return shape [verify: CONFIRMED]

`matrix_studio/engine/simulator.py`:

- `_select_next_speaker(topic, agents, conversation, last_speaker, settings, model=None, cognition=None)`
  → returns `(selected_agent_name: str, reason_or_None)`. With cognition ON it is a JSON-mode call
  returning `{"speaker", "reason"}`; OFF is the plain pre-2c prompt.
- `_generate_response(speaker_name, agent, topic, conversation, settings, model=None, cognition=None, retrieved_memories=None)`
  → returns a dict:
  - always: `{"content", "tokens_in", "tokens_out", "cost_usd"}`
  - cognition ON (single JSON-mode call, same call as the utterance — confirmed, NOT a separate
    pass): adds `"rationale"`, `"goal_served"`;
  - memory ON: adds `"memories"` (list of `{content, importance, tags}`, max 2);
  - goals_dynamic ON: adds `"goal_update"` (full new goal list or None);
  - relationships ON: adds `"relationship_updates"` (dict other→stance).
  - The structured-output JSON schema is composed dynamically from the enabled cognition
    sub-flags (a `fields` list built in `_generate_response`), so **the 2c rationale schema CAN be
    extended minimally** for 4b thread fields — confirmed, no redesign needed.
  - Parse failure degrades gracefully: raw text kept as utterance, no cognition keys (never stalls a run).
  - LLM exception path returns `content = "[Error generating response: ...]"` with zero tokens/cost —
    this is an **engine marker, not model output** (relevant to 4a: it must not be "validated").
- The turn loop is `_run_turns(...)` (shared by fresh run + branch resume). Per turn:
  `_select_next_speaker` → emit `speaker.selected` → `_retrieve_memories` (top-K importance+recency)
  → `_generate_response` → append to conversation → update speaker token/cost totals → emit
  `agent.response` → memory.formed / goal.updated / relationship.updated / agent.reflected (each
  gated on its flag) → save per-turn `SimSnapshot` → emit `checkpoint.saved` → cost-cap check.

### 1.2 No pre-emit validation exists [verify: CONFIRMED]

In `_run_turns` the result of `_generate_response` is committed directly: the message is appended to
`conversation` and emitted as `agent.response` with no consistency check between generation and
emission. 4a inserts its gate between `_generate_response` returning and the message being committed.

### 1.3 Interventions — where mutation kinds are enumerated/dispatched [verify: CONFIRMED]

Three layers, all additive:

1. **API validation:** `matrix_studio/api/app.py` — `BranchMutationModel` (fields for all kinds) and
   `_validate_branch_mutation()` (whitelist of kinds; raises HTTP 422 on unsupported/malformed).
2. **DB-facing resolution:** `matrix_studio/branching.py` — `create_branch_run` records
   `config.branch_mutation`; `execute_branch` resolves `promote_aside` → plain `inject_message`
   via `_resolve_promote_aside` before the engine sees it.
3. **Engine dispatch:** `matrix_studio/engine/simulator.py` — `_apply_branch_mutation()` is the
   kind-dispatch (`continue`, `inject_message`, `edit_goal`, `add_persona`, `remove_persona`);
   raises `BranchMutationError` (→ 422) on unknown kind. Called from `resume_simulation` at the
   fork BEFORE generating forward. **4c `adaptive_pressure` is a new kind in this dispatch.**

### 1.4 SimSnapshot state carriage (2c pattern) [verify: CONFIRMED, with one important nuance]

- `SimSnapshot` (`matrix_studio/state.py`) is a Pydantic model persisted whole via
  `snapshot.model_dump_json()` (`Database.save_snapshot`, INSERT OR REPLACE keyed on
  `(run_id, turn)`), one full snapshot per turn. Any new field with a default added to
  `SimSnapshot`/`AgentState` serializes automatically AND old stored snapshots still parse
  (Pydantic default applies). So 4b's `pending_threads` rides the **scrubber** (snapshot API) for free.
- **Nuance found in code (matters for 4b):** branch/resume state reconstruction does NOT load the
  snapshot — `branching.reconstruct_at_turn()` REPLAYS the parent event log, and it replays ONLY
  `agent.response` events (cast, transcript, token/cost totals). It deliberately does not rebuild
  memory streams/goals/relationships (pre-existing limitation, unchanged by Phase 4). Therefore for
  4b threads to "survive branch/scrubber reconstruction" (acceptance criterion), replaying the new
  `thread.opened/resolved/abandoned` events in `reconstruct_at_turn` is required — the thread event
  payloads carry the full thread entries, so replay is lossless. Implemented that way (additive).

### 1.5 Events table is generic [verify: CONFIRMED]

`storage/database.py`: `events(run_id, turn, seq, event_type TEXT, agent_name, payload TEXT/JSON,
created_at)`. New event types (`validation.checked`, `validation.flagged`, `thread.*`,
`pressure.applied`) need **no migration**. The frontend event folder
(`frontend/src/lib/simState.ts`) switches on known `event_type`s and silently ignores unknown
types, so new events are additive for the UI too.

### 1.6 Cost cap as the template [verify: CONFIRMED]

`settings.max_run_cost_usd` (default 0 = OFF, byte-for-byte pre-Phase-3 when off) is the pattern
for 4a's `validation_enabled` and 4c's `adaptive_pressure_enabled` off-switches, and
`tests/test_cost_cap.py::test_cost_cap_off_unchanged_behavior` is the pattern for the 4a
regression lock.

### 1.7 Baseline event stream captured for the byte-for-byte lock

Before writing any Phase 4 code I ran the pre-4a engine (mocked litellm, deterministic 3-turn run)
and captured the exact `(turn, seq, event_type, agent_name, payload)` stream:
`sim.started` → per turn (`speaker.selected` → `agent.response` → `checkpoint.saved`) →
`sim.completed`, seq 0..10, and exactly 6 LLM calls (2 per turn). That expected stream is
hard-coded in the 4a regression test and must be reproduced exactly with `validation_enabled=OFF`.

### 1.8 Environment note

The repo `.env` is root-owned and unreadable by the test user; the suite must run with
`_MSS_TEST_MODE=1` **exported before pytest starts** (the conftest monkeypatch is too late for
modules imported at collection time in this environment). With that, the Phase 3 baseline of
207 passed is reproduced exactly.

---

## 2. What was built (per sub-phase)

### 2.1 — 4a: Priority-hierarchy validation gate

**New module `matrix_studio/validation.py`** + a pre-emit block in `_run_turns`
(`matrix_studio/engine/simulator.py`), between `_generate_response` returning and the message
being committed/emitted.

- **Heuristic-first checks, in hierarchy order** (highest violated principle reported):
  - *coherence* — speaker emits dialogue attributed to another cast member (`"Ben: ..."`);
  - *continuity* — verbatim repeat of a recent message (only for utterances ≥ 40 normalized chars;
    short replies like "I agree." legitimately repeat);
  - *agency* — utterance flatly negates a participant's freedom of choice (small unambiguous phrase
    list; `check_agency()` is exported and reused by the 4c pressure guard);
  - *character consistency* — first-person claim to BE another cast member.
- **Selective LLM check (cost control):** one fuzzy heuristic (near-duplicate by token Jaccard
  ≥ 0.8) only raises a *suspicion*; a suspicion triggers exactly one small JSON confirmation call.
  Clean turns pay zero extra LLM cost. A failing confirmation call is **fail-open** (suspicion
  dropped) so a broken checker can never spiral a run.
- **Reject-and-regenerate, retry budget 1** (`settings.validation_retry_budget`): a violating turn
  is regenerated once; a still-violating final attempt is emitted **verbatim** with a
  `validation.flagged` event (failing principle + reason). The utterance is NEVER edited in place.
  A rejected attempt's real token/cost is still counted on the speaker (it happened; no fabricated
  zero-cost).
- **Events:** `validation.checked` per gated attempt (`{speaker, attempt, passed, [method],
  [principle], [reason]}`), `validation.flagged` on budget exhaustion. Generic events table — no
  migration.
- **Off-switch:** `settings.validation_enabled` (default ON). The gate block is skipped entirely
  when OFF.
- **Engine error markers** (`"[Error generating response: ...]"`) are skipped by the gate — they are
  engine artifacts, not model output; validating/regenerating them would be meaningless.

**Regression lock:** `tests/test_validation_gate.py::test_validation_off_byte_for_byte_pre_4a`
asserts the exact 11-event `(turn, seq, event_type, agent_name, payload)` stream captured from the
pre-4a engine (§1.7), the exact LLM call count (6), and that no validation key appears anywhere in
the result. 11 new tests total: OFF-lock, clean-pass, regenerate-then-pass, budget-exhausted-flag
(emitted verbatim), hierarchy ordering, continuity + short-exemption, identity-claim, shared agency
helper, selective-LLM-confirm (exactly one extra call), confirm-failure fail-open, error-marker skip.

---

### 2.2 — 4b: Latent/pending-thread state (setups & payoffs ledger)

**New state:** `PendingThread` (`matrix_studio/state.py`) — `{id (12-hex), description,
thread_type (setup|promise|faction-action|deferred-consequence), origin_turn, origin_agent,
status (open|resolved|abandoned), resolved_turn}`. Global ledger `SimSnapshot.pending_threads`
(default `[]` — old stored snapshots still parse).

- **Opt-in via cognition config:** `cognition.threads` (default OFF — cognition-enabled runs keep
  their pre-4b structured schema byte-for-byte unless threads are turned on) +
  `cognition.thread_stale_after` (default 5 turns).
- **Causally real, both directions:**
  - *In:* every turn's generation prompt lists the OPEN threads with their ids ("Unresolved
    threads..."); the listed ids are recorded as the turn's `thread_refs` on `agent.response`
    (the exact analogue of 2c `memory_refs`). Proven by a test that reads the actual prompts:
    an open thread appears in the next turn's prompt; a resolved thread stops appearing.
  - *Out:* the 2c structured output schema is extended (only when threads are ON) with
    `thread_updates: {open: [{description, thread_type}], resolved: [ids], abandoned: [ids]}` —
    same single JSON-mode call, no separate pass.
- **Honesty guards:** `resolved`/`abandoned` ids are accepted ONLY if that thread was genuinely
  open and in-context this turn (no fabricated payoffs of unseen threads); opens are capped at 2
  per turn; unknown `thread_type` degrades to `setup`; parse failure discards thread updates with
  the rest of the structured fields (graceful, run never stalls).
- **Events:** `thread.opened` / `thread.resolved` / `thread.abandoned` (payload carries the full
  entry, making event replay lossless). Generic table, no migration.
- **Snapshot/branch/scrub survival:** the ledger rides every per-turn/mutation/capped/final
  snapshot. `branching.reconstruct_at_turn` now returns `(topic, agents, conversation,
  pending_threads)` — the ledger is replayed from thread events (honouring the fork point), the
  fork snapshot is seeded with it, and `execute_branch`/`resume_run_in_place` pass the run's own
  cognition config + ledger into `resume_simulation` so the branch keeps evolving them forward.
  (Pre-4b, branches passed `cognition=None`; for runs without a cognition config the parsed
  default is identical, so pre-existing branch behavior is unchanged — full suite confirms.)
- **Staleness surfacing:** new read-only API `GET /api/runs/{ref}/pending-threads` (ledger +
  per-thread `stale` flag, `stale_after`, `as_of_turn`), and the agent dossier gains
  `pending_threads` (threads planted by that agent, with `stale`). Sourced only from real snapshot
  state; an empty ledger returns `[]`, never a synthesized thread.

**Tests:** 7 new — threads-off (no schema/events/refs + empty ledger), full
open→feed-forward→resolve lifecycle (prompt-level proof of causality + `thread_refs`),
abandon + fabricated-resolution-ignored + open-cap + type-degradation, branch reconstruction
losslessness at two fork points, branch-continues-ledger-forward (prompt-level proof on the
branch), old-snapshot-parses, staleness API + dossier.

### 2.3 — 4d: Structured output view (optional, derived)

**New module `matrix_studio/structured_view.py`** (`build_structured_view` — pure function, no
LLM/DB/mutation) + endpoint `GET /api/runs/{ref}/turns/{turn}/structured`.

- **Four sections, all sourced from canonical data only:**
  - *Narrative* — the turn's `agent.response` utterance(s), **verbatim** (never paraphrased),
    each with the `source_seq` of its backing event;
  - *Consequences* — immediate (goal/relationship changes, thread resolutions/abandonments,
    validation flags, cost-cap) and deferred (threads opened this turn); **every line carries
    `source_seq`** — a line without a backing canonical event cannot be constructed;
  - *Updated State* — goal/relationship/memory/belief deltas from real state events;
  - *Possibilities* — the OPEN pending threads as of the turn's snapshot (4b ledger),
    explicitly `non_limiting: true`; never generated suggestions.
- **Absent data is stated, not filled:** each section has an honest `note` ("No state deltas were
  recorded for this turn.") instead of invented content.
- **Default OFF:** `settings.structured_output` (env `STRUCTURED_OUTPUT`) gates the endpoint
  globally; a per-request `?opt_in=true` enables it per call. Enabling it performs zero writes —
  a test snapshots the canonical event stream before/after a view request and asserts equality.
- Not the canonical record: no engine change at all in 4d — it is entirely a read-side projection.

**Tests:** 8 new — default-off 403, opt-in leaves canonical events untouched, global-setting
enable, every-line-backed-by-real-event + verbatim-narrative (per-turn, resolved against the real
event log), possibilities-from-ledger + honest empty notes, unknown-turn 404, formatter unit fold
over all event kinds, empty-turn honesty.

### 2.4 — 4c: Adaptive-pressure intervention (EXPERIMENTAL, opt-in, built last)

**New module `matrix_studio/pressure.py`** + a new `adaptive_pressure` kind in the 2b
branch-mutation family (`_apply_branch_mutation` dispatch + `BranchMutationModel` /
`_validate_branch_mutation` at the API).

- **Branch-from-checkpoint only** (2b family): observe signals at the fork → generate ONE
  narrator-voiced world event → inject it as a real branch turn (reuses the `inject_message`
  mechanics with `source: "pressure"`), preceded by a `pressure.applied` audit event carrying the
  observed signals + attempts + real token/cost verbatim. History is never edited in place; the
  parent run is untouched (asserted by test).
- **Signals from real state only** (`observe_signals`, pure): repetition (max pairwise token
  Jaccard over the last 6 messages), stale open threads (4b ledger, default age 5), open-thread
  count, remaining turn budget.
- **HARD agency guard:** every generated pressure text is checked with the same
  `validation.check_agency` the 4a gate uses. Violating text is regenerated once; if the retry
  still violates, the WHOLE intervention is rejected (`PressureRejectedError` →
  `BranchMutationError` → HTTP 422). Nothing is emitted and nothing rewritten on rejection
  (asserted end-to-end: no `pressure.applied`, no injected turn). A generation failure also
  rejects — no fabricated fallback event.
- **Off by default, double-gated:** `settings.adaptive_pressure_enabled=False` → the API refuses
  the mutation kind with 422 BEFORE any branch run row is created, and the engine dispatch
  refuses it independently (defense in depth).
- **Experimental labeling:** README section marked "⚠ EXPERIMENTAL"; the Scrubber intervention
  picker labels the option "Adaptive pressure (experimental)" with an inline warning; the engine
  refusal message says "experimental and disabled".

**Tests:** 8 new — API refusal when disabled (+ no orphan run row), engine refusal when disabled,
`observe_signals` pure unit (repetition/stale/budget/empty), full ON branch (audit event + injected
narrator turn + parent untouched), guard regenerate-then-accept, guard reject-after-budget,
end-to-end rejected-pressure-emits-nothing, generation-failure rejects. Frontend suite (18) still
green including the new Scrubber option; `tsc -b` clean.

## 3. Test counts

| Stage | Backend tests | Delta | Frontend tests |
|---|---|---|---|
| Baseline (v0.3.0, pre-Phase 4) | 207 | — | 18 |
| After 4a (validation gate) | 219 | +12 | 18 |
| After 4b (pending threads) | 226 | +7 | 18 |
| After 4d (structured view) | 234 | +8 | 18 |
| After 4c (adaptive pressure) | 242 | +8 | 18 |

Final full-suite run at v0.4.0: **242 backend passed, 18 frontend passed, 0 failures** —
all 207 pre-existing tests still pass unmodified. `tsc -b` clean.

Note on running the suite in this environment: the repo `.env` is unreadable by the test user, so
the suite must be invoked as `_MSS_TEST_MODE=1 python3 -m pytest tests/` (env exported before
pytest starts; the conftest sets it too, but too late for import-time settings loads here).

---

## 4. Honest limitations

- **Real-LLM behavior: not benchmarked.** All Phase 4 tests run against mocked litellm responses
  (the established suite pattern; no billable calls). How often the 4a heuristics fire on real
  model output, how often the selective LLM confirmation is triggered, how well real models use
  the 4b `thread_updates` schema, and the quality of 4c pressure events have NOT been measured
  against a live model. No real-LLM percentages are claimed anywhere in this report or the docs.
- **4a heuristics are deliberately narrow.** They catch four concrete, high-precision failure
  shapes (speaking-as-another, verbatim repeats, explicit choice-negation phrases, first-person
  identity claims) plus one fuzzy near-duplicate signal. They have no semantic world-model: subtle
  coherence/causality violations pass undetected, and the agency phrase list is English-only and
  literal (a paraphrased negation slips through; a quoted phrase can false-positive — absorbed by
  the bounded regenerate-then-flag design, never by rewriting).
- **Validation of the top two principles is thin.** "World coherence" and "causality" are only
  guarded by the frame-break and near-duplicate checks respectively; the full hierarchy semantics
  remain a design principle enforced where cheap, honest signals exist.
- **4b thread quality depends on the model.** The engine guarantees mechanics (causal feed-forward,
  honest resolution, snapshot/branch survival), not that a model plants good threads or pays them
  off sensibly. Thread retrieval is "all open threads" (runs are short); no recency/importance
  ranking and no embedding retrieval (still deferred, as per spec).
- **4c is experimental for a reason.** The agency guard is the same literal phrase check as 4a —
  necessary, not sufficient; a pressure event can still be dramaturgically heavy-handed without
  tripping it. Signals are simple (token-overlap repetition, thread age); there is no automatic
  trigger — pressure is operator-initiated per branch, by design.
- **Pre-existing limitation, unchanged:** branch reconstruction replays transcript/cost and (new)
  thread events, but NOT memory streams/goals/relationships — a branch's agents keep their cast
  persona/goals plus the transcript, as in 2c/3. Phase 4 did not extend cognition-state replay.
- **Install:** source-only (`git clone` + `pip install -e .`); this package is not published to
  PyPI/npm.

---

## 5. Push status

All four sub-phase commits (+ this release commit) are on local `master`. `git push origin master`
fails in this environment: the remote is HTTPS GitHub, no usable credential is available to this
user (`gh` CLI absent for this user; root's `gh` credential store is not readable), so pushes
error with "could not read Username for 'https://github.com'". Commits are ready to push as soon
as a credential is available; nothing else is blocked on it.
