# Requirements — TheMatrix Simulation Studio, Phase 1.5

**Project:** TheMatrix Simulation Studio (working title)
**Phase:** 1.5 — Post-run analysis layer (summary + read-only aside conversations)
**Owner:** CC
**Author:** Main (CABAL)
**Date:** 2026-07-08
**Status:** APPROVED — CC sign-off 2026-07-09 ("phase 1.5 spec looks good, start MasterControl building it"). Cleared for build.
**Spec:** `docs/PROJECT-SPEC.md` (§6, §6a interaction design)
**Builds on:** Phase 1 (COMPLETE, verified live 2026-07-08 — FastAPI+WS backend, React UI, SQLite event store, named runs, replay). Phase 0 engine unchanged.

---

## Plain-English Summary

Phase 1 lets you **watch** a completed multi-agent conversation. Phase 1.5 lets you **make sense of
it and interrogate it** — without changing what happened.

Two capabilities, both **read-only** over a *finished* run:

1. **End-of-run summary** — when a conversation completes, produce a structured summary: the
   **consensus** reached, any **outstanding dissenters** (and what they objected to), and the
   **interesting ideas / facts / thoughts** surfaced. Shown as a summary panel on the run.
2. **Aside conversations** — ask questions about the finished run in a side-thread, targeting either:
   - **the analyst** — a neutral voice answering *about* the whole conversation ("what were the
     strongest arguments against?"), or
   - **a single persona** — that participant answering *in character* ("Dr. Webb, expand on your
     liability point" / "fact-check your earlier claim"), or
   - **the room** — all personas reacting to a prompt (read-only; they do NOT resume the canonical run).

Every aside is a **private side-thread**: it uses the transcript as context but **never alters the
canonical run**, never adds canonical turns, and asides don't see each other. This is deliberately
the read-only half of a larger design (see §Design Model) whose mutating half — *promoting an aside
back into the room* and *restarting/continuing the group* — is **Phase 2**.

**Why this phase now:** it's the "analysis layer over a completed run" — high value for reviewing/
demoing a sim, and mechanically small: every feature here is **one LLM call over the transcript**
with a different system prompt. It touches **no engine loop**, adds **no** simulation capability, and
introduces the **thread/target/mode** data model that makes Phase 2's branching a clean add, not a rebuild.

---

## Design Model (the unifying concept — read before scoping)

All post-run interaction is **scoped threads over a run**, each with a **target** and a **mode**:

- **Target** = who you talk to: **persona** (in-character, that agent's system prompt) · **analyst**
  (neutral voice over transcript) · **room** (all personas).
- **Mode** = whether it mutates canon:
  - **Aside (read-only)** — reply lives in the side-thread only; canonical run untouched. **ALL of Phase 1.5.**
  - **Contribute (mutating)** — reply/input injected back into the canonical conversation; group continues. **Phase 2 (branch-from-checkpoint).**

**Phase 1.5 implements Aside mode only, for all three targets, plus the automatic end-of-run summary
(which is effectively an analyst-aside generated once at completion).** The data model and UI MUST be
built so Phase 2 can later add **Contribute** as a mode toggle + a "bring into conversation" action on
the *same* thread UI — but Phase 1.5 builds none of that mutating behavior.

**Boundary rule (hold this line):** an aside is **ephemeral analysis**. It must never alter the run,
never append a canonical turn, and never ripple to other personas. A persona "fact-checking itself"
in an aside changes nothing in the run — it's a one-shot reflection. The moment a reply is meant to
become "what everyone now knows," that's Phase 2's immutable branch. Do NOT build a half-way mutable
version in 1.5.

---

## Goal

Extend the Phase 1 app so that, for any **completed** run, a user can:
- see an auto-generated **summary** (consensus / dissenters / interesting ideas), whose generation
  behavior is **configurable at run-creation** and **defaults to on/useful**;
- open **aside threads** and ask questions of the **analyst**, a **single persona**, or the **room**,
  getting answers grounded in the transcript, **without mutating the run**;
- revisit persisted asides and summaries later (they're stored, attached to the run, marked non-canon);

adding **zero** OpenClaw coupling, **no** engine-loop changes, and **no** mutating/branching behavior.

---

## In Scope (Phase 1.5)

### 1. End-of-run summary
- **Trigger:** generated automatically when a run reaches `sim.completed` (and available on-demand for
  already-completed historical runs, incl. imported ones, via an explicit "generate summary" action).
- **Content (structured, not free prose):** a JSON object with at least:
  - `consensus` — points of agreement the group converged on (list; may be empty).
  - `dissenters` — outstanding disagreement: who dissented and what they objected to (list of `{speaker, position}`; may be empty).
  - `key_ideas` — interesting ideas / facts / thoughts / novel framings surfaced (list).
  - `open_questions` — unresolved threads worth pursuing (list; optional but useful).
  - `overview` — a 2–4 sentence plain-English overview.
- **Mechanism:** a single `litellm.acompletion` call over the full transcript with an "analyst"
  system prompt, returning strict JSON (validated; retry once; graceful fallback to a plain-text
  overview if JSON parse fails — never crash the run/UI).
- **Configurable at run creation (see §3):** the new-run form/request may specify a summary config
  (enabled? which fields? custom focus/instruction?). **Default = enabled with the standard field set
  above** so a user who specifies nothing still gets a useful summary.
- **Storage:** persisted attached to the run (new `summaries` or reuse `threads` table, see §4) and
  returned by `GET /api/runs/{ref}`. Regenerable (re-run the call) — the latest replaces/append-versions.
- **Honesty gate:** the summary is model-generated *analysis of the transcript* — it must be clearly
  labeled as such (generated summary, not ground truth), and its own token/cost counted honestly, not fabricated.
- **Imported runs:** if a run has a pre-existing source summary (e.g. TheMatrix `summary` field carried
  by the importer), surface it as an **"original (imported) summary"** distinct from a freshly generated
  one. Do not overwrite the imported text; a user may still generate a structured summary alongside it.

### 2. Aside conversations (read-only threads)
- A user opens an **aside thread** on a completed run and picks a **target**:
  - **Analyst** — neutral system prompt; answers *about* the conversation using the full transcript.
  - **Persona** — that agent's **own persona/system prompt** (already stored per-agent in the run's
    cast/snapshot) + the full transcript as context; answers **in character** (expand a point, defend a
    position, self-fact-check). Must use the real stored persona text — never invent a persona.
  - **Room** — all personas; the prompt is posed to each (or to a group construct) and their in-character
    reactions are returned **into the aside thread only**. This must NOT resume or extend the canonical run.
- **Multi-turn:** an aside thread supports follow-up questions; the thread keeps its own message history
  (user + target replies), independent of and invisible to other asides and to the canonical run.
- **Grounding:** every target sees the transcript as read-only context. Persona/room replies are framed
  as "reflecting after the discussion," making clear (in the prompt) that this is a post-hoc aside, not a
  new live turn, so the model doesn't pretend the conversation is continuing canonically.
- **Mechanism:** one `litellm.acompletion` per user message in the thread (for room-target, one call per
  persona, or one structured multi-voice call — implementer's choice, but must stay read-only and bounded).
- **Cost:** each aside call's tokens/cost tracked and shown (asides cost money — surface it honestly; count
  separately from the canonical run's cost so the run's recorded cost stays accurate).
- **Explicitly NOT in 1.5 (Phase 2):** any "add this reply to the conversation," "let the group continue
  with this," "restart the discussion," or otherwise mutating/branching action. The UI may show a disabled/
  "coming in a later version" affordance for "bring into conversation," but must not implement it.

### 3. Summary configuration at run creation (with a useful default)
- The `POST /api/runs` request body and the new-run form gain an **optional** `summary` config, e.g.:
  ```json
  "summary": {
    "enabled": true,
    "fields": ["consensus", "dissenters", "key_ideas", "open_questions", "overview"],
    "focus": "optional free-text steer, e.g. 'emphasize legal and ethical risk'"
  }
  ```
- **Default when omitted:** `enabled: true` with the standard field set and no custom focus — i.e. a
  user who specifies nothing still gets the full, useful summary automatically at completion.
- `enabled: false` skips auto-generation (user can still trigger it manually later).
- The new-run form exposes this as a small, collapsed-by-default "Summary options" section (a checkbox
  + optional focus text) so casual users never have to touch it.

### 4. Backend API additions (all additive; no change to existing Phase 1 routes/schema semantics)
- `POST /api/runs/{ref}/summary` — (re)generate the structured summary for a completed run; returns the summary object. Body may override fields/focus.
- `GET  /api/runs/{ref}/summary` — fetch the stored summary (structured + any imported original).
- `POST /api/runs/{ref}/threads` — create an aside thread; body `{target: "analyst"|"persona"|"room", persona_name?: string}`. Returns `thread_id`.
- `GET  /api/runs/{ref}/threads` — list aside threads for a run (target, created_at, message count).
- `POST /api/threads/{thread_id}/messages` — post a user message to an aside thread; runs the LLM call(s) and returns the target reply(ies). Read-only w.r.t. the canonical run.
- `GET  /api/threads/{thread_id}` — fetch a thread with its full message history.
- **Storage (additive tables — do NOT alter `runs`/`events`/`snapshots` semantics):**
  - `summaries(id, run_id, kind['generated'|'imported'], payload_json, tokens_in, tokens_out, cost_usd, created_at)`.
  - `threads(id, run_id, target, persona_name, mode['aside'], created_at)` — `mode` column present now, always `'aside'` in 1.5, so Phase 2 can add `'contribute'` without migration.
  - `thread_messages(id, thread_id, role['user'|'target'], speaker, content, tokens_in, tokens_out, cost_usd, created_at)`.
  - These are new tables; existing Phase 0/1 tables and queries are untouched (backward compatible).
- **Transport:** aside replies may be returned synchronously from the POST (they're short) or streamed
  over the existing WS pattern — implementer's choice; synchronous is acceptable for 1.5. If streamed,
  use a **thread-scoped** channel, never the run's canonical event stream (asides must not appear as run events).

### 5. Frontend additions
- **Summary panel** on the run view (and history/replay view): renders the structured summary —
  consensus, dissenters (with who/what), key ideas, open questions, overview. Shows imported original
  summary separately when present. A "generate / regenerate summary" button for completed runs.
- **Asides panel / drawer:** start a new aside; pick target (analyst / a specific persona from the cast /
  the room); chat-style thread with follow-ups; clearly labeled **"Aside — not part of the conversation."**
  Per-thread cost shown. Multiple threads listed and revisitable.
- **Clear canon boundary in the UI:** asides and summaries are visually distinct from the canonical
  transcript so a viewer never confuses an aside reply with something a persona said in the actual run.
- A **disabled** "bring into conversation / continue the discussion" affordance may be shown with a
  "available in a later version" label (sets up Phase 2) — but is non-functional in 1.5.

---

## Out of Scope (defer)

**Phase 2 (mutating / branching):** Contribute mode in any form — promoting an aside reply into the
canonical conversation, injecting user input so the group continues, restarting/continuing the larger
discussion, add/edit/remove persona mid-run, checkpoint scrubber, branch tree. All are branch-from-
checkpoint operations on the event-sourced engine (PROJECT-SPEC §3.7, §6a). Phase 1.5 is read-only.

**Phase 2 (rich dossier):** memory streams, reflections/beliefs, relationship graph, goal hierarchy,
"why did it say that?" trace — unchanged from Phase 1's deferral.

**Phase 3 (release polish):** in-browser BYO-key entry, enforced spend caps (incl. for asides), docs site.

**Also cut (project spec §9):** spatial map/sprites; OpenClaw dependency; synchronous/UI-blocking
simulation stepping; re-running/reproducing a branch.

---

## Acceptance Criteria (how we verify "done")

- [ ] Phase 0 CLI (`python -m matrix_studio run <req.json>`) and Phase 1 UI behavior are unchanged (regression); all existing tests pass.
- [ ] A completed run auto-generates a structured summary (consensus / dissenters / key_ideas / open_questions / overview) by default when the request specifies no summary config.
- [ ] Summary config is honored: `enabled:false` skips auto-generation; `focus` steers the summary; a summary can be generated on demand for an already-completed run (incl. an imported one).
- [ ] Imported runs with a source summary show the original separately from any generated summary; the original is not overwritten.
- [ ] An aside thread can be opened against **the analyst** and answers about the transcript, grounded (no fabrication of transcript content).
- [ ] An aside thread can be opened against **a specific persona** and answers **in character** using that agent's real stored persona text (verify the persona text is actually used, not invented).
- [ ] An aside thread can be opened against **the room** and returns per-persona reactions **into the thread only**.
- [ ] **Read-only boundary holds:** creating/using any aside or summary does NOT add canonical `events`, does NOT modify the run's `conversation`/snapshot, does NOT change the run's recorded cost, and does NOT appear in the run's canonical event stream/replay. Verify by diffing the run's events/snapshot/cost before vs after aside activity.
- [ ] Aside threads are multi-turn (follow-ups work) and independent (one aside does not see another's messages).
- [ ] Aside and summary token/cost are tracked and shown, counted separately from the canonical run cost. Costs are real, not fabricated.
- [ ] No mutating/branching action is implemented; any "bring into conversation" affordance is disabled/labeled "later version."
- [ ] New tables are additive; existing Phase 0/1 tables/queries/rows are untouched and still work.
- [ ] Live end-to-end check against real Bedrock: generate a summary and run analyst + persona + room asides on a real completed run (the imported `bridge-kibble` vet-diet run is a good fixture). State which checks were live vs mocked; do NOT fabricate cost/latency.
- [ ] `grep -ri openclaw matrix_studio/ frontend/src` returns nothing; no new OpenClaw coupling.
- [ ] `docker build .` still succeeds (or NOT-VERIFIABLE-HERE stated honestly if Docker absent).

---

## Constraints & Notes

- **Honesty gate (carried from Phase 0/1):** summaries and aside replies are model-generated analysis —
  label them as such; never present them as ground truth or as canonical persona statements. No fabricated
  metrics/costs; state live vs mocked.
- **Read-only is the whole point.** Nothing in 1.5 may mutate a run. If an implementation choice makes an
  aside/summary write to `events`, `conversation`, or the run's cost, it is wrong — use the new tables.
- **Reuse stored persona text.** Persona-target asides must load the agent's actual persona/system prompt
  from the run's cast/snapshot. Never regenerate or approximate a persona.
- **Keys stay server-side** (Phase 1 rule): aside/summary LLM calls use server-side `.env` keys; browser never sees keys.
- **Model default** is `bedrock/global.anthropic.claude-haiku-4-5-...`; summary/aside calls default to the
  run's configured model (or that default). The EOL `claude-3-5-sonnet-20241022` must not reappear.
- **Forward-compat for Phase 2:** `threads.mode` exists now (always `'aside'`); the UI thread abstraction
  and API shapes anticipate a future `'contribute'` mode + a promote-to-run action, but 1.5 builds neither.
- **Tests:** backend tests for summary generation (mocked LLM, JSON-parse + fallback), aside thread lifecycle
  (analyst/persona/room, multi-turn, isolation), and the read-only invariant (events/snapshot/cost unchanged
  by aside activity). Minimal frontend smoke tests for the summary panel + asides drawer. Keep LLM calls mocked
  in the suite (real env has a live key).
- **License/SPDX:** new source files carry the Apache-2.0 SPDX header, consistent with Phase 0/1.

---

## Handoff

- **Gate:** DRAFT. Do NOT hand off to MasterControl until CC signs off on this doc (hold the DRAFT gate — Phase 0's did not).
- **No open blocking decisions** — the web stack, storage (SQLite additive tables), and transport all follow Phase 1. One implementer's-choice item (aside replies synchronous vs WS-streamed on a thread-scoped channel) is noted inline and does not block.
- **On sign-off:** Main writes the MasterControl brief (repo `/root/projects/matrix-sim-studio`, this doc as authoritative acceptance criteria), MasterControl builds Phase 1.5 on top of Phase 1, commits (does not push), and reports each acceptance checkbox as PASS / FAIL / NOT-VERIFIABLE-HERE with live-vs-mocked noted. Main independently verifies (as with Phase 1) before relaying to CC.
- **Good live fixture:** the imported `bridge-kibble` run (vet-diet reauthorization, 24 turns, 8 personas) — exercise summary + analyst/persona/room asides against it.
