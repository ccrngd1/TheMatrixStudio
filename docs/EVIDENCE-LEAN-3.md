# Evidence lean on a second brief — pre-registration

**Status:** pre-registered 2026-09-29, before any run. Criteria not to be edited after the runs start.

## Why

`personas.evidence_lean` became the default after `docs/EVIDENCE-LEAN-2.md`, and every measurement so far used
one brief: a legal-exposure question. This asks whether it holds on a different kind of discussion — the
operator's stored product-direction brainstorm (six structured personas, private definitions).

## Design

The brainstorm definition as stored, with two changes made for cost and comparability and recorded here: the
voice model is the deployment default (Sonnet 5, as in both earlier comparisons) rather than the Opus 5 it names,
and avatars are off. Its own settings otherwise (36 turns, hybrid method with a closing round, retrieval,
cognition). Two arms differing only in `personas.evidence_lean` (false / true, set explicitly). **Three runs per
arm, launched together.** Scored by `scripts/analyse_evidence_lean.py --criteria 2 --repeats 3`, unchanged.

## Criteria — the same as EVIDENCE-LEAN-2, decided now

- **Primary:** runs stating a lean by majority of three passes — **on ≥ 2 of 3, and on − off ≥ 1.**
- **Guardrails:** mean standing dissenters on ≥ off − 1; median words within ±25%; on cost ≤ 1.15 × off.
- **Reported:** lean passes per arm; best-guess share (noisy); `position.shift` flags per arm and how many name
  none of the persona's stated conditions (new since EVIDENCE-LEAN-FOLDING; not decisive at this n).
- **Decision.** Met → the default stands with a second brief behind it. Primary missed → the default stands on
  the first brief's evidence but is recorded as not generalising here, and the operator decides.

## Result — 2026-09-29

Runs: off `7c5d0d9a` `f530d119` `28a593d5` ; on `77e9525e` `c3744afe` `1bc4c032` . All six complete (36 turns plus the brief's opening and closing rounds; 47 responses
each), launched together after `529fec1`. Scored by `scripts/analyse_evidence_lean.py --criteria 2 --repeats 3`
as committed (the analyst is a model).

| | off | on |
|---|---|---|
| **runs stating a lean, by majority of 3 passes** | **1 of 3** | **3 of 3** |
| passes stating a lean | 2 of 9 | 9 of 9 |
| mean standing dissenters | 3.7 | 5.0 |
| median words per message | 162 | 167 |
| total cost | $3.92 | $3.99 |
| best-guess share, pooled (noisy; reported only) | 0.19 | 0.76 |
| `position.shift` flags (naming none of the persona's stated conditions) | 3 (1) | 1 (1) |

- **Primary: MET** — 3 of 3 against 1 of 3, and unanimous across every on-arm pass.
- **Guardrails: met.** Standing dissent went *up* (5.0 vs 3.7), words +3%, cost +2%.
- **Shift flags** (reported, not decisive): fewer on the on arm, one per arm naming none of the persona's
  conditions — no sign here that the default increases unconditioned moves.
- **Decision, as pre-registered:** the default stands, now with a second, different kind of brief behind it.
