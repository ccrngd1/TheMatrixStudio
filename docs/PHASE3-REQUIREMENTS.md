# Requirements — TheMatrix Simulation Studio, Phase 3

**Status:** DRAFT — for CC approval (2026-07-09)
**Spec:** `docs/PROJECT-SPEC.md` (§6 Phase 3, §7 release-scope multipliers, §8 open questions)
**Builds on:** Phases 0/1/1.5/2a/2b/2c (feature-complete engine + UI).

---

## Summary

Phase 3 is **release polish** — the work that turns a feature-complete tool into
something a stranger can install, run safely, and understand in five minutes.
No new simulation capability. An audit (2026-07-09) shows much of §7 is already
scaffolded during earlier phases; this phase *finishes and hardens* it rather
than starting from scratch.

Already present (verified): Apache-2.0 `LICENSE` + `pyproject.toml` (pip-install,
py3.11+, no autogen), a working multi-stage `Dockerfile` (frontend build → single
API+static image), `python -m matrix_studio serve|run` CLI, `.env.example`,
`examples/`, and a live token/$ **cost meter**.

Remaining work is: (1) cost guards (spend cap, not just a meter), (2) BYO-key
setup UX + secure-handling docs, (3) a real docs pass (README is stale at
"Phase 1.5"), (4) example templates for 5-minute value, (5) release hygiene.

---

## Decisions carried in (from §8, already resolved)
- **License: Apache-2.0** (chosen; patent grant suits a customer-facing tool). No change.
- **Packaging: pip/pyproject + Docker**, single-node. Both exist; Phase 3 verifies + documents.
- **Avatar image model: Stability SD3.5 on Bedrock**, non-photorealistic (anime default), optional.
- **Storage: SQLite** single-file (embeddable). No change.

---

## In Scope (Phase 3)

### 1. Cost guards (the one genuinely new behavior)
- **Optional hard spend cap per run.** A configurable USD ceiling
  (`settings.max_run_cost_usd`, default 0 = off). The engine checks accumulated
  cost after each turn; when the cap is reached it stops generating and ends the
  run in a distinct terminal state (`sim.capped` event; run status `capped`),
  never crashing or silently truncating. Additive to the loop; off = today's
  behavior byte-for-byte.
- **Warn threshold** surfaced in the UI cost meter (already has `warnThreshold`)
  and echoed at run creation ("this run may cost up to ~$X at N turns").
- **Honesty:** the cap acts on *real* accumulated `cost_usd`; when litellm does
  not report a per-call cost we count $0 for that call and say so (no fabricated
  spend). The cap is best-effort on providers that omit cost.

### 2. BYO-key configuration UX + secure handling
- **First-run/config clarity.** A `/api/health`-style readiness check that
  reports which providers are configured (has-key booleans only — NEVER the key
  values) so the UI can tell a new user "no model key detected; set one in .env".
- **Docs:** a single, unambiguous "bring your own key" section — where keys go
  (`.env`, env vars, or the boto3/Bedrock chain), that they stay server-side, and
  that nothing is ever sent to the browser or logged. Reaffirm the allowlist
  (`/api/models`) only exposes model *strings*, never secrets.
- **No key material in the client, logs, events, snapshots, or summaries** —
  add a test asserting `.env`-style secrets never appear in API responses.

### 3. Documentation pass
- **README refresh** — currently says "Phase 1.5". Rewrite to describe the
  shipped tool (live control room, checkpointing/branching + interventions,
  cognition/dossier + why-trace, model selection, avatars), with an accurate
  feature list and screenshots/gifs placeholder.
- **5-minute quickstart** — pip install OR `docker run`, set one key, open the
  UI, load an example, hit Run. Both paths verified end-to-end.
- **Configuration reference** — every `settings` field (model, temperature,
  max tokens, avatars/style, cognition flags, cost cap, host/port, data dir).
- **Cognition + honesty note** — one short doc section: cognition is
  model-generated introspection captured in-loop, not ground truth; cost impact.

### 4. Example simulations / templates
- Ship 2–3 ready-to-run `examples/*.json` (e.g. a debate, a design review, a
  negotiation) that demonstrate cast + goals + (optionally) cognition on, so a
  new user gets value immediately. Verify each runs from the CLI and loads in
  the new-run form.

### 5. Release hygiene
- Version bump (`0.1.0` → a release version), a short `CHANGELOG.md` covering
  Phases 0–3, and a cleanup pass (dead files, stray TODOs, `requirements.txt` vs
  pyproject reconciliation). Confirm `Development Status` classifier.

---

## Explicitly NOT in Phase 3
- **New simulation/cognition capability.** Phase 3 is polish only. Embedding-based
  memory retrieval (deferred in 2c) stays deferred.
- **In-browser key entry / multi-tenant secret storage.** Keys stay server-side
  (`.env`/env/role chain). No key UI that puts secrets in the browser — that would
  fight the security model. "BYO-key UX" = clear docs + a has-key readiness signal.
- **Hosted/multi-node deployment, auth, user accounts.** Single-node tool.
- **Live per-token streaming caps mid-call.** The cap is checked per completed
  turn (the engine's unit), not mid-generation.
- **A docs website / static-site generator.** In-repo Markdown + README for v1;
  a docs site is a later nicety, not release-blocking.

---

## Acceptance criteria
- With `max_run_cost_usd = 0` (default), runs behave byte-for-byte as pre-Phase-3
  (no cap checks change events/snapshots/cost). Regression-locked by test.
- With a cap set below a run's projected cost, the run stops at/under the cap,
  emits `sim.capped`, ends in status `capped`, and the parent/immutability rules
  are unaffected. Test covers cap-hit and cap-not-hit.
- `/api/health` (or equivalent) reports per-provider has-key booleans and NEVER
  a secret; a test asserts no `.env` secret appears in any API response body.
- README no longer says "Phase 1.5"; the quickstart (pip and docker) each run
  end-to-end to a live UI on a clean checkout (verified, not assumed).
- At least 2 example templates run from the CLI and load in the new-run form.
- Full backend + frontend suites pass; new tests cover the cost cap (on/off) and
  the secret-never-leaks assertion.
- `CHANGELOG.md` present; version bumped; no autogen/dead deps.

---

## Proposed build order (incremental, each shippable)
1. **Cost guard** — `max_run_cost_usd` + per-turn check + `sim.capped` terminal
   event + status; UI meter shows cap/warn; creation-time cost estimate. Tests:
   off = unchanged, cap-hit, cap-not-hit. (The only real code slice.)
2. **BYO-key readiness + secret-safety** — has-key readiness endpoint + UI hint;
   secret-never-leaks test; wire the terminal `capped`/readiness states into the
   frontend status handling (reuse the `connecting/complete` badge work).
3. **Examples + templates** — 2–3 curated `examples/*.json`, verified both paths.
4. **Docs pass** — README rewrite, quickstart (pip + docker), configuration
   reference, cognition/honesty note.
5. **Release hygiene** — version bump, CHANGELOG (Phases 0–3), cleanup, final
   full-suite + clean-checkout smoke (pip install and docker build).

Each step: additive, all prior tests green, its own tests + commit + push
(same cadence as 2a/2b/2c).

---

## Open questions (for CC)
1. **Release version** — cut `1.0.0` at end of Phase 3, or a conservative
   `0.2.0`/`0.3.0`? (Leaning 0.3.0 — "feature-complete, pre-1.0 polish"; 1.0
   implies API stability guarantees we may not want yet.)
2. **Cost-cap default** — ship off (0) so nothing surprises existing behavior
   (my lean), or ship a sane non-zero default (e.g. $1.00) to protect new users
   from runaway spend out of the box?
3. **Public repo now or later** — is the GitHub repo going public at end of
   Phase 3, or staying private a while? (Affects how hard we polish docs/screens.)
4. **Screenshots/gifs** — want me to capture live UI screenshots for the README
   (requires a running server + a demo run), or leave placeholders for you?
