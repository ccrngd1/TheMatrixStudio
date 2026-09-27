# Asking personas to cite inline — pre-registration and result

**Status:** pre-registered 2026-09-27 (`1ffbcad`), before any run; **result recorded below** — primary
MISSED (off arm too high), both guardrails met. The criteria were not edited after the runs started.

## Why

Personas are shown retrieved passages labelled `title #n`, with the instruction "quote or cite it by
name when it supports a claim". Measured on 25 stored runs (2026-09-27): 816 of 824 messages had
passages in their prompt and **none** cited one by its label or title. So a claim can be traced only
to the few passages that were in view — verbatim quotation recovers the source for ~6% of messages
(`matrix_studio/attribution.py`) and nothing recovers it for paraphrase.

The same measurement found that the citation checker could not have recognised such a citation
anyway: `CITATION_RE` requires a file extension, and knowledge-base and researched titles have none.
That is fixed in the same change (`citations._citation_spans` also matches the exact titles in the
turn's context), so this experiment can measure what it asks about.

## The intervention

`retrieval.cite_inline: true` appends one instruction to the persona's documents block
(`retrieval.CITE_INLINE_RULE`): end a sentence that relies on a passage with its label in square
brackets, exactly as listed, and cite only passages actually used. Off by default.

## Design

Two arms, identical except for `retrieval.cite_inline`: `examples/citeInline/run.off.json` and
`run.on.json`. Two personas, each with one pasted document from `examples/background/` titled
**without** a file extension (the knowledge-base case), 12 turns, deployment default models. **Two
runs per arm.** The topic does not mention citing, so the off arm is the normal behaviour.

Unit: a message with passages in view (`document.retrieved` for its turn and speaker). Pooled across
an arm's two runs.

## Criteria — decided before running

- **Primary — cite rate.** Share of in-view messages whose `citation_provenance` contains at least
  one first-hand or second-hand citation. **Success: on ≥ 0.50 and off ≤ 0.10.**
- **Guardrail 1 — no invented labels.** Among the on arm's attributive citations, share judged
  `unverified` ≤ 0.20. A rule that makes personas cite things they were not given is worse than none.
- **Guardrail 2 — it does not take over the speech.** Median words per message in the on arm within
  ±25% of the off arm.
- **Decision.** All three met → recommend making it the default (a separate change, decided by the
  operator). Primary met, a guardrail missed → keep it opt-in and say which. Primary missed → keep it
  off; the wording does not work.

n = 2 runs per arm is small and the verdict is labelled as such. It can support "this wording moves
the rate a lot, or does not"; it cannot estimate the rate precisely.

## Result — 2026-09-27

Runs: off `d9190686`, `01505752`; on `17538934`, `adc944e6`. All four complete, 12 turns, $0.33 forecast
each. Scored by `scripts/analyse_cite_inline.py`, written before the runs finished.

| | off | on |
|---|---|---|
| in-view messages | 24 | 24 |
| **cite rate** | **0.21** (5/24) | **1.00** (24/24) |
| attributive citations, unverified | 3, 0 | 33, 0 |
| median words per message | 101 | 98.5 |

- **Primary: MISSED.** The on arm cleared its bar (1.00 ≥ 0.50); the **off** arm did not stay under
  its cap (0.21 > 0.10). As pre-registered, a missed primary means the rule stays **off by default**.
- **Guardrail 1: met** — no citation of a passage the speaker was not given.
- **Guardrail 2: met** — messages were the same length (−2.5%).

**Why the off arm cited.** Its citations are genuine — "the filter from Cost observations #1". With two
short, plain titles, personas sometimes cite unprompted. The 25 stored runs the cap was set from had
long, similar titles ("Source material — …") and showed none. So the cap was set from a baseline this
design does not reproduce; that is a flaw in the pre-registration, recorded rather than repaired.
What the probe does show, at n = 2 per arm: the rule takes citing from occasional to every message,
without invented labels and without longer speech.

**Two defects found by the probe, both fixed:**

1. A bracketed knowledge-base label ("[Cost observations #1]") read as a bare mention, not an
   attributive citation, because the bracket pattern also assumed a file extension — so the citation
   gate never checked such citations for borrowed authority. The attributive counts above are from
   the fixed detector, re-applied offline to the stored messages; as recorded live, the on arm showed
   6 attributive citations, not 33.
2. (Pre-existing, found while designing this.) Knowledge-base titles were not recognised as citations
   at all — `CITATION_RE` requires a file extension.

**Decision.** Off by default, per the rule above. Offered as an opt-in on the launch form. The next
test that would decide the default is the same comparison on a realistic cast — long, similar titles
and 40 turns, the setting where the baseline was 0/816 — pre-registered with the off-arm cap taken
from that setting.
