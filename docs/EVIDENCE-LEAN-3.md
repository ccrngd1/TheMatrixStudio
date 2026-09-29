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
