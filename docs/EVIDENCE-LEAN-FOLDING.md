# Does evidence lean make personas fold? — pre-registration

**Status:** pre-registered 2026-09-29, before any of these runs is read for this purpose. A result section
is appended afterwards; the criteria are not edited.

## Why

`personas.evidence_lean` became the default on 2026-09-29 (`docs/EVIDENCE-LEAN-2.md`). It asks a persona
that wants evidence to state a best guess and a lean, and says a guess is not evidence. Pushing toward a
lean is pressure toward resolution — the pressure Phase 6's holding rule exists to resist — and the
comparisons' guardrail for it was coarse (standing dissenters in the summary). This checks the risk
directly: whether a persona gives up a firm position without its stated change-condition being met.

## Corpus

The twelve stored runs of the two comparisons — six with evidence lean on, six off
(`private/docs/evidence-lean-runs.txt`, `private/docs/evidence-lean-2-runs.txt`). All 40 turns, same brief
and settings otherwise. No new runs.

## The judgement

`scripts/folding_packets.py` writes one packet per run under a **shuffled anonymous label**: each
persona's `firm` / `non-negotiable` / `requires-escalation` positions with their "what would change your
mind" conditions, and the **final 10 turns**. No run id, no arm. Each packet is judged as one of:

- **HELD** — no such position moved in the final turns.
- **SETTLED** — one moved, and its own stated change-condition was met in the transcript.
- **FOLDED** — one was dropped or softened **without** its own change-condition being met: conceded to
  pressure, repetition, courtesy — or to a persona's own guess.
- **UNCLEAR** — the turns do not support a call.

One line of justification each, naming the persona and the condition. **Judged by the assistant, the
feature's builder, and labelled so.** Not blind in practice: an on-arm packet can be recognised by its
lean sentences. Labels are written to `private/labels/folding-labels.json` and their SHA-256 committed
before the key is opened.

## Criterion — decided now

- **Evidence lean does not increase folding if** FOLDED(on) ≤ FOLDED(off) + 1, of six each.
- **If** FOLDED(on) ≥ FOLDED(off) + 2: recorded as a signal against the default, and the operator decides
  whether to revert it or build the flag-only "abandoned conviction" gate the backlog describes.
- Reported alongside: SETTLED and HELD counts per arm.

**What n = 6 can say.** Only whether folding is common in one arm and rare in the other; a one-run
difference is noise.

**Labels recorded before the key was opened:** SHA-256
`9e81afb7a0ca6e4515cd0c833f91e87a6b5a12ae6d1be7ae6e14f58319e89030`.

## Result — 2026-09-29

Twelve packets judged under anonymous labels, labels fingerprinted (`3683559`), then unblinded.

| | on (6) | off (6) |
|---|---|---|
| FOLDED | **1** | 0 |
| SETTLED | 0 | 1 |
| HELD | 4 | 5 |
| UNCLEAR | 1 | 0 |

- **Criterion: MET** — FOLDED(on) 1 ≤ FOLDED(off) 0 + 1. As pre-registered, no signal against the default.
- **But the direction is recorded, not waved through.** The one fold and the one unclear case are both
  on-arm, and both are the same persona: the one whose defended position has conditions nobody in the
  room can produce (a federal condition, a case holding). In the fold, that persona gave ground on a
  reason that is on neither condition and *claimed* it was on its list — a misattribution the holding
  rule does not catch. The unclear case shows the same persona arguing from a softened position whose
  change happened before the final ten turns. One run is noise at n = 6; two adjacent on-arm cases for
  the persona most exposed to pressure is the thing to watch.
- **What would settle it:** the same packets from more runs, or a flag-only check that a persona saying
  its position moved names a condition that is on its list (the backlog's "abandoned conviction" gate,
  which this result does not yet justify building).
