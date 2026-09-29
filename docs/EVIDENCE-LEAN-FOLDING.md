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
