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

## 3. Test counts

- Baseline (pre-Phase 4): 207 backend + 18 frontend, all passing.

---

## 4. Honest limitations

_(filled in at the end; includes the real-LLM benchmark statement)_
