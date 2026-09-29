# How much does the judge vary on the same transcript? — pre-registration

**Status:** pre-registered 2026-09-29, before any repeat judgement. Criteria not to be edited afterwards.

## Why

`distinct_positions` — the blind judge's count of genuinely distinct substantive positions — is the one signal
pointing at a real cost to rendering convictions from structured data rather than prose. Across the Phase 6
arms it reads: prose `5, 5, 5`; the shipped rendering `5, 3, 3`; two rule variants `5, 5, 3` and `2, 2, 5`
(`docs/labels/judge-rules.json`). Every one of those numbers is **one judge call on one run**, so a between-run
gap of 2 and a judge that disagrees with itself by 2 are indistinguishable in the data that exists. The
backlog calls this the highest-value remaining Phase 6 question; this is the free half of answering it, and it
gates the other half — if the instrument is that noisy, no amount of re-running the arms can settle anything.

## Design

The four stored 15-turn Sonnet transcripts (`docs/labels/sonnet-transcripts.json`). Each is judged **five
times** by `scripts/judge_variance.py` using `score_validation.judge_arm` **unmodified** — same prompt, same
schema, same model as the default (Sonnet 5) — under the same anonymous label each time.

**Found while trying to run this, and recorded rather than quietly fixed:** `judge_arm` asks for
`temperature=0`, and **Sonnet 5 refuses any temperature but 1**; litellm raised `UnsupportedParamsError`, so
`score_validation.py --judge` has been broken against the default model since that model became the default.
Fixed with `drop_params=True` (the convention everywhere else in this codebase), which means the judge runs
at **temperature 1** on Sonnet 5. That makes this measurement more necessary, not less: the numbers in
`docs/labels/judge-rules.json` were produced by a model that honoured 0, and any future scoring run on the
default model will not be. The criteria below were fixed before the fix and are unchanged.
No new conversations; about $0.10 of judge calls.

## Criteria — decided now

For each transcript, `spread = max − min` of `distinct_positions` over its five judgements.

- **The metric is unusable at n = 1 per run if** any transcript's spread is **≥ 2** — the size of the
  between-arm gaps the Phase 6 question rests on. Then `docs/labels/judge-rules.json`'s numbers do not
  support a claim about rendering, and any future arm comparison must judge each run **≥ 3 times and use
  the median**, which this pre-registration then recommends.
- **The metric is stable enough if** every transcript's spread is **≤ 1**. Then the observed instability is in
  the runs rather than the judge, the Phase 6 question stands as stated, and the next step is more runs.
- **Reported alongside:** the spread of every other integer field in the schema, so a reader knows whether
  the noise is specific to `distinct_positions` or general to the judge.

**What four transcripts × five repeats can say.** Whether the judge disagrees with itself by as much as the
effect being claimed. Not the exact variance of any field.

## Result — 2026-09-29

Four transcripts × five judgements, twice: once before the output-budget fix below and once after
($0.16 + $0.31). `private/docs/judge-variance.json`, `-fixed.json`.

| | spread of `distinct_positions` over 5 judgements |
|---|---|
| all four transcripts, before and after the fix | **0** (every judgement 5) |

- **Criterion: the metric is stable enough at one judgement per run.** Every spread is 0, not merely ≤ 1, at
  the temperature the default model forces. So the judge is not the source of the Phase 6 instability
  (`5,5,5` vs `5,3,3` vs `5,5,3` vs `2,2,5`): those numbers differ because the *runs* differ, the question
  stands as stated, and the next step on it is more runs rather than more judgements.
- **Other fields are nearly as stable**, and the exceptions are worth knowing: `positions_citing_a_named_source`
  moved by up to 2 and `participants_who_changed_position`, `position_changes_justified_by_new_evidence`,
  `talking_past_each_other` and `specificity` by 1. A claim resting on a gap of 1 in any of those is inside
  the judge's own noise; `distinct_positions` is not.
- **Caveat.** Four transcripts of one 15-turn brief, all judged 5 as it happens — a corpus where the count is
  easy. It does not show the judge is stable where the answer is genuinely ambiguous.

**Two defects found by trying to run this, both fixed, neither a result:**

1. **`--judge` could not run at all against the default model.** It asks for `temperature=0`; Sonnet 5 permits
   only 1, and litellm raised `UnsupportedParamsError`. Fixed with `drop_params=True` (`65978de`).
2. **Then 5 of 20 judgements came back empty** — `finish_reason="length"` against a 900-token cap, because
   Sonnet 5 spends most of an output budget reasoning. Each landed as `{"error": "unparseable"}`, which the
   scoring report renders as an arm with no numbers rather than as a failure. The cap is now 8,000 (the size
   `research.ASK_MAX_TOKENS` settled on for the same reason) and the error carries `finish_reason`, so "wrote
   prose" and "ran out of room" are distinguishable. After the fix: 20 of 20 parsed.
