# Asking personas to cite inline — pre-registration and result

**Status:** pre-registered 2026-09-27, before any run. The result section is filled in afterwards and
the criteria above it are not edited once a run has started.

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

## Result

*(Not yet run.)*
