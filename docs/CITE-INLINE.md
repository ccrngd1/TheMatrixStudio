# Asking personas to cite inline — pre-registration and result

**Status:** pre-registered 2026-09-27 (`1ffbcad`), before any run; **result recorded below** — primary
MISSED (off arm too high), both guardrails met. The criteria were not edited after the runs started.
**Default: ON for new runs since 2026-09-28, by operator decision** — see the end of this document.

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

## Operator decision — 2026-09-28

The operator turned the rule **on by default for new runs**, overriding the pre-registered "primary
missed → keep it off". That is the operator's call to make, and it is recorded as a decision, not as
a result: the probe above did not meet its criterion, and nothing here re-scores it.

How the default is applied: `RetrievalConfigModel.cite_inline` (the API request model) defaults to
true and is written into each new run's stored config. The engine's own default
(`RetrievalConfig.cite_inline`) stays false, so a run created before the change — whose stored config
has no value — keeps its prompt when resumed or branched. The launch form's checkbox starts ticked
and sends an explicit false when unticked.

The realistic-cast comparison is still the test that would say whether this default helps on long
conversations; with the default on, its off arm is now the one that has to be asked for.


---

# Comparison 2 — a realistic cast (pre-registered 2026-09-28, before any run)

The default is now on (above), decided on a 12-turn, two-persona probe with short plain titles. This
asks whether it holds where it matters, and what it costs through the citation gate, which now sees
every citation (`citation_integrity` in `validation.py`; a rejected turn is regenerated once).

**Design.** A private six-persona definition kept out of the repository: 40 turns, a knowledge base
bound to each persona plus one shared, long and similar titles — the setting in which 25 stored runs
showed no label citations at all. Voice model: the deployment default (Sonnet 5), identical in both
arms; the definition's own Opus setting is removed for cost. Two arms differing only in
`retrieval.cite_inline` (false / true). **Two runs per arm.** Forecast before launch: ~$1.05 a run.

**Criteria — decided before running.** The first comparison's flaw was a cap on the off arm set from
a baseline its design did not reproduce, so the primary criterion here is on the DIFFERENCE.

- **Primary.** On-arm cite rate ≥ 0.50 **and** on − off ≥ 0.40 (same definition as comparison 1: share
  of in-view messages with a first- or second-hand citation, as recorded live).
- **Guardrail 1.** Among the on arm's attributive citations, `unverified` ≤ 0.20.
- **Guardrail 2.** Median words per message within ±25% of the off arm.
- **Guardrail 3 — the gate's cost.** On-arm total cost ≤ 1.15 × off-arm total cost. Reported alongside:
  `citation_integrity` rejections per 100 turns in each arm.
- **Decision.** All met → the default stays on, with this as its evidence. Primary missed → recommend
  reverting the default to off. A guardrail missed → say which, and recommend reverting or a fix.

**Descriptive, not a criterion.** Every `citation_integrity` rejection is read and judged correct or a
false positive (P2 "citation gate false-positive rate"). The judgement is made by the assistant, not a
human, and is labelled so.

## Result — comparison 2 (2026-09-28)

Runs: off `849574ce`, `c4350cbb`; on `4ad4cd8a`, `f9ee3339`. All complete, 40 turns each. Scored by
`scripts/analyse_cite_inline.py --comparison 2`.

| | off | on |
|---|---|---|
| in-view messages | 80 | 80 |
| **cite rate** | **0.00** | **0.26** (21/80) |
| attributive citations, unverified | 0, — | 23, 1 (0.04) |
| median words per message | 152 | 138 (−9%) |
| in-run cost, both runs | $2.37 | $2.25 |
| `citation_integrity` rejections | 0 | 2 — one turn, rejected then flagged (2.5 per 100 turns) |

- **Primary: MISSED.** On − off = 0.26, short of 0.40; the on arm is also under 0.50.
- **Guardrails 1–3: met** — few unverified citations, messages slightly shorter, no extra cost.
- **Pre-registered decision: recommend reverting the default to off.** That recommendation goes to the
  operator, who set the default; nothing is changed by this result on its own.

**What it shows.** Without the rule, a realistic cast cites nothing (0/80, matching the 0/816 of the
stored runs). With it, about a quarter of messages cite — against every message on the short probe.
Every bracketed citation was recognised (22 of 22 messages with a bracket), so the shortfall is in what
personas write, not in detection. This definition also runs cognition (JSON utterances) and structured
personas; the probe ran neither, and a much longer prompt is the likeliest reason one instruction
carries less weight. That is an explanation, not a finding — it was not tested.

**The gate activation — judged a false positive (assistant judgement).** A persona cited passage #0 of
its own source; that turn it had retrieved only #10, but it had retrieved #0 on turn 7. The gate treats
only the current turn's passages as read, so it rejected a legitimate recall, regenerated the turn, and
flagged the second attempt. The rule is deliberate — "a chunk the speaker never saw" catches invented
content — so it is not loosened here; the fix is for first-hand to mean "retrieved by this speaker
earlier in the run", which needs the per-speaker retrieval history rebuilt on resume the way the
citation ledger is. Recorded as open.
