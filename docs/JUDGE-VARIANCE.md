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
