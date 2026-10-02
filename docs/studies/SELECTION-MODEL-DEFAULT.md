# Should `speaker_selection` default to a bigger model?

Opened 2026-09-15. A plan, not a change. Nothing here is implemented.

The per-role `ModelSet` plumbing already exists (`matrix_studio/models.py`), so this is
entirely about **which model `ROLE_DEFAULTS` names when nobody configured anything**, and
what that costs. Three of the five questions below turned out to have measurable answers,
so they were measured before the plan was written rather than after.

> **Result, added 2026-09-15 after the sweep ran (§6): the answer is no.** Haiku+counts beat
> Sonnet-baseline on Gini and on minimum-turns on 4 of 4 transcripts at 39% of the cost, and
> Sonnet+counts was disqualified on the unresolved-reply guardrail. `ROLE_DEFAULTS` is
> unchanged. The plan below is left as written, pre-registration intact.

**The short version.** Both candidate models silently discard `temperature=0.3` — confirmed
from litellm's model map AND then measured against Bedrock: with `drop_params=False`,
`temperature=0.3` on `global.anthropic.claude-sonnet-5` raises
`UnsupportedParamsError: Only temperature=1 is supported`, while Haiku 4.5 accepts it. Opus 5 would cost **+$0.56 per
40-turn run** (5× the role, +53% on the run's measured spend) for a call whose output is
one name. And the mechanism analysis in `SPEAKER-SELECTION-EVALUATION.md` §3 says the
moderator's problem is that it **cannot see who is overdue**, which is an information
problem a larger model does not fix. So: recommend **Sonnet 5**, not Opus, and **gate it on
the eval harness** against the free intervention that is already winning. Details in §4.

---

## 1. What changes minimally

### The one-line change

```python
# matrix_studio/models.py
SELECTION_MODEL = "bedrock/global.anthropic.claude-sonnet-5"   # new constant

ROLE_DEFAULTS = {
    "validation": LOW_VARIANCE_MODEL,       # unchanged — see below
    "speaker_selection": SELECTION_MODEL,   # was LOW_VARIANCE_MODEL
    "naming": LOW_VARIANCE_MODEL,           # unchanged
}
```

**A new named constant rather than deleting the entry.** Deleting `speaker_selection` from
`ROLE_DEFAULTS` would make selection inherit the *conversation* model, which sounds simpler
and is worse: `model` is the field users actually change, so a run switched to Opus for its
voice would silently start paying Opus for 40 selection calls too. That is exactly what I
had to override by hand on the Opus run (`data/renewalBrief/run.json` pins
`speaker_selection` back to Haiku). Decoupling the roles is the whole point of the table;
an explicit constant keeps the decoupling and makes the cost predictable.

### Do `validation` and `naming` stay on Haiku? Yes, and for different reasons

| role | stays? | why |
|---|---|---|
| `validation` | **yes, non-negotiable** | `temperature=0.0`, and it decides whether to regenerate a turn. Sonnet 5 and Opus 5 both drop the 0.0 (§2), so a bigger gate answers differently on the same turn. Determinism is the feature; there is no version of "better judgement" that compensates for a gate that is not a gate. |
| `naming` | **yes, but it is a free choice** | Two words, once per run, ~60 tokens. Both directions cost approximately nothing; it stays cheap because there is no argument for moving it, not because moving it would hurt. |

So `LOW_VARIANCE_MODEL` keeps its meaning — but only two roles use it, and
`speaker_selection` **leaves the low-variance group**. That is a change of posture, not
just a constant, and the prose has to move with it.

### What else the change drags along (all of it prose or tests, none of it wiring)

1. **`models.py` module docstring.** Three places name selection as a temperature-sensitive
   role: the "Why this is not merely a cost optimisation" section, the role table's
   *wants* column ("consistency and low cost"), and the closing paragraph. All become
   false.
2. **`models.py` line 48** claims selection's *outputs* are small "so a frontier model buys
   nothing measurable". Measured (§3): the **input** is 3,278 tokens per call, 27× the
   output. The cost argument is real but it was stated about the wrong end of the call, and
   correcting it is part of this change whichever model wins.
3. **`simulator.py:220`** — the inline comment "temperature 0.3 is deliberate and Sonnet 5
   would silently drop it" becomes a description of a thing we are now doing on purpose.
4. **`tests/test_model_roles.py::test_the_gate_and_selection_get_a_temperature_honouring_model`**
   asserts both roles resolve to `LOW_VARIANCE_MODEL`. Split it: the gate keeps that
   assertion; selection gets a new one, plus a test that the temperature drop for selection
   is *known* (§2).
5. **`tests/test_model_selection.py:98`** asserts the models used in a default run are
   exactly `{settings default, LOW_VARIANCE_MODEL}`. If `SELECTION_MODEL` equals the
   settings default (`bedrock/global.anthropic.claude-sonnet-5` — it does today), this test
   stops distinguishing anything: it would pass while selection quietly followed the
   conversation model. Re-anchor it on the *role*, asserting selection's model directly.
   Its sibling at line 99 (`assert default != LOW_VARIANCE_MODEL`, "this test proves nothing
   if the two defaults are the same model") is the same trap caught once already.
6. **`docs/studies/SPEAKER-SELECTION-EVALUATION.md` §1** records "Haiku 4.5 by default,
   `temperature=0.3`" as the mechanism under test. Every baseline number in §2 and §8 was
   measured on Haiku at 0.3, so they are not the baseline for a Sonnet selector.

No changes to `resolve`, `model_for`, `/api/models`, the frontend, or the infra template.

---

## 2. The temperature risk — measured, and it is worse than "Sonnet drops it"

`models.py` says Sonnet 5 accepts only `temperature=1`. I checked what litellm believes
about all three candidates, from the model map it ships (no API calls):

```
global.anthropic.claude-haiku-4-5   supports_sampling_params = None  -> honoured (name fallback)
global.anthropic.claude-sonnet-5    supports_sampling_params = False -> dropped
global.anthropic.claude-opus-5      supports_sampling_params = False -> dropped
global.anthropic.claude-fable-5-1   supports_sampling_params = False -> dropped
```

`litellm/llms/anthropic/common_utils.py::_apply_sampling_param` forwards `temperature` only
when `_supports_sampling_params(model)` is true **or the value is exactly 1**; otherwise it
raises `UnsupportedParamsError` — or, with `drop_params=True` (which
`matrix_studio/lazy_litellm.py` sets unconditionally), discards it and proceeds.

**So the answer to "does Opus honour it?" is no.** Opus 5 is not a way to keep the
temperature and gain judgement; it is the same trade Sonnet offers at 2.5× the price. Every
frontier model in the current family has removed sampling params. Haiku 4.5 is the only
temperature-honouring option on the menu, which reframes the whole question: this is not
"small vs large", it is **"keep temperature 0.3, or keep a larger model"**.

### What to do about it

1. **Accept the drop, explicitly, and stop calling it a risk.** Selection at temperature 1
   is a different mechanism, not a broken one — and note the direction is not obviously
   bad: the failure being chased is *dyad lock*, a too-consistent choice. Higher
   temperature may help fairness while hurting responsiveness. That is an empirical
   question, which is what §5 is for. What must not happen is adopting it while the docs
   still claim 0.3 is in force.
2. **Make the drop visible per role, not just per run.** `ModelSet.log_plan` logs
   role→model, which no longer tells an operator what they need. Add the temperature and
   whether it will survive:

   ```
   Models by role: speaker_selection=...sonnet-5 (temperature 0.3 DROPPED by this model),
                   validation=...haiku-4-5 (temperature 0.0 honoured), voice=...
   ```

   Source the flag from litellm's model map (`model_cost[...]["supports_sampling_params"]`),
   not from a hand-maintained list here — a hardcoded list is how this became invisible the
   first time. Treat a missing flag as "honoured", matching litellm's own fallback.
3. **Guard the gate, not selection.** A test should assert that no role with a
   deliberately-low temperature *except* `speaker_selection` resolves by default to a
   model that drops it. That keeps `validation` protected by construction while recording
   selection as a knowing exception rather than an oversight.
4. **If temperature 1 turns out to hurt**, the fallbacks in order of cost: pin selection
   back to Haiku (revert); keep Sonnet and add intervention C's deterministic fairness
   floor so variance cannot starve anyone; or keep Sonnet and reduce variance structurally
   by giving it the participation counts (intervention A), which constrains the choice with
   information instead of with a sampling parameter.

---

## 3. Cost — measured on the real 40-turn Opus run, not estimated from the prompt template

Selection is the highest-frequency call in a run: **once per turn, every turn**. I
reconstructed the actual selection prompt for each of the 40 turns of
`renewal-renewal-opus` (`64cff65d`) from its recorded transcript, using the harness's own
prompt builder:

```
cast                       6 personas, structured personas ON
personas block             4,033 chars  (stable across the run)
utterance length           mean 811 chars, median 817, max 1,270
selection prompt           mean 11,802 chars, max 15,071
                        ≈  3,278 input tokens per call, 131,135 for the run
output                     ≤120 max_tokens; ~40 actual (JSON: speaker + reason)
```

Two things to notice before the money. First, **the input dominates by 27×** — selection is
a cheap *output*, not a cheap call, and `models.py` currently makes the cost argument about
the output. Second, the prompt does **not** grow with run length: the window is the last 10
messages, so per-call cost plateaus and total cost scales linearly with turns. A 100-turn
run costs 2.5× a 40-turn run, not 6×.

Prices from litellm's map, per million tokens: Haiku 4.5 $1/$5, Sonnet 5 $2/$10,
Opus 5 $5/$25.

| selection model | input 131.1k | output ~1.6k | **role total, 40 turns** | per turn | vs Haiku |
|---|---|---|---|---|---|
| Haiku 4.5 (today) | $0.131 | $0.008 | **$0.139** | $0.0035 | — |
| Sonnet 5 | $0.262 | $0.016 | **$0.278** | $0.0070 | +$0.139 (2.0×) |
| Opus 5 | $0.656 | $0.040 | **$0.696** | $0.0174 | +$0.557 (5.0×) |

In run terms: that run's voice calls cost **$0.9061** (305,617 in / 29,488 out, from the
recorded per-turn costs). Selection sits beside it at 15% of the voice bill on Haiku, 31% on
Sonnet, **77% on Opus**:

| selection model | voice + selection, 40 turns | delta on the run |
|---|---|---|
| Haiku 4.5 | $1.045 | — |
| Sonnet 5 | $1.184 | **+13%** |
| Opus 5 | $1.602 | **+53%** |

### Two cost caveats that matter more than the percentages

- **Selection spend is currently not counted at all.** The run's cost total sums the voice
  calls only (already in `private/private/docs/BACKLOG.md` (kept out of git)), so today's $0.139 of selection is invisible to
  the run total *and to the user spend cap*. Moving to Opus would make the unmetered
  fraction ~40% of a run's true cost, so a cap set at $2 would stop a run that had actually
  spent nearly $3. **Fix the metering in the same change, or before it.** This is the one
  hard prerequisite in the plan: it is a correctness bug about money that the model change
  amplifies.
- **Prompt caching is unclaimed.** ~1,300 of the 3,278 input tokens per call (the personas
  block plus boilerplate) are byte-identical on every turn, and nothing marks them
  cacheable. Claiming that would cut the input bill by roughly 40% on *any* model, which is
  a larger saving than the Haiku→Sonnet difference costs. Worth filing separately; it makes
  the bigger selector cheaper without changing the argument for it.

---

## 4. Which model, and how hard a default

### Recommendation: Sonnet 5, and only if the eval earns it

**Sonnet 5 over Opus 5**, for three reasons in descending order of confidence:

1. **Opus buys nothing the task needs.** The output is one name from a list of six. The
   judgement required is "who is being addressed, who is overdue, who has something to
   answer" — a reading-comprehension task over a 10-message window, not a reasoning task.
   The recorded baseline reasons are already specific and well-observed (one cites a
   persona's silence and *what* they went silent about). The failure mode is not bad
   reasoning; it is reasoning over an incomplete view.
2. **Neither honours the temperature**, so Opus's premium buys only capability, at 2.5× Sonnet
   and 5× Haiku for the highest-frequency call in the system.
3. **Cost/benefit is upside-down at 40 turns and worse at 100.** +53% on a run for the
   cheapest-to-get-wrong call is the worst trade in the role table.

**And a prediction I want on the record before any of this runs, because that is this
project's convention:** I expect **Haiku + intervention A (show the participation counts)
to beat Sonnet-with-the-current-prompt on Gini**, at zero extra cost. §3 of the evaluation
doc argues the moderator's failure is that it cannot see who is overdue; §8 shows the counts
moving Gini 0.31→0.17 and 0.38→0.22 on Haiku. A bigger model reading the same blind prompt
still cannot see past the window. If that prediction holds, the correct outcome of this plan
is **"do not change the default"**, and the plan will have earned its cost by saying so on
evidence rather than by argument. The interesting arm is therefore **Sonnet + counts**: if
the two effects compose, that is the adopt candidate; if the counts alone capture most of
it, the default stays on Haiku.

### Config default vs hard default

`ROLE_DEFAULTS` is *already* the soft layer: `config.models.speaker_selection` overrides it
per run, and `config.model` overrides it for every role. So the real question is whether to
add a **settings/env knob** (`SELECTION_MODEL` as an environment variable) so a deployment
can change it without a code change.

**Recommendation: no new knob.** Put it in `ROLE_DEFAULTS` as a code constant.

- The per-run override already covers every case where somebody wants something else,
  including the experiment in §5, and it is recorded in the run's config — so a run's
  transcript says which selector produced it. An env var is *not* recorded in the run, so
  two runs with identical configs could have different selectors and nothing would say so.
  For a change whose entire justification is a measured effect on turn shares, that is
  disqualifying.
- One more environment variable is one more thing that differs between the deployment and
  every test run, and this project has already paid for that kind of drift (`AWS_REGION`
  set but `AWS_DEFAULT_REGION` not, litellm 1.99 locally vs 1.100 in the image).
- `ROLE_DEFAULTS` with a named constant is a one-line diff and reviewable. If per-deployment
  variation is ever genuinely needed, the knob can be added then, with a real use case to
  shape it.

---

## 5. Validation — the harness already takes `--model`, so this is a sweep, not a build

`scripts/eval_speaker_selection.py` accepts `--model`, and the `baseline` arm calls the
engine's own `_select_next_speaker` with `--check-baseline` proving the prompt is
byte-identical. **No harness changes are needed to answer the question**, which is the point
of having built it.

### The grid

| dimension | values |
|---|---|
| model | Haiku 4.5, Sonnet 5, Opus 5 |
| arm | `baseline`, `counts` |
| transcript | `3abd39b3`, `2d2ac45b`, `793b54c4` (24 turns each), `64cff65d` (40 turns, Opus voice) |
| loop | closed (pacing is the question), plus open loop for `baseline` as a comparability check |
| repeats | **3 per cell** |

**The repeats are not optional, and they are new.** Every number in §8 of the evaluation doc
is a single pass at temperature 0.3. Sonnet and Opus run at an effective temperature of 1
(§2), so their variance is strictly higher, and the doc already records run-to-run Gini
variance of ≥0.1 on the *same* definition. A single pass cannot resolve the ~0.05
differences this is looking for. Report **mean and range** per cell; a cell whose range
overlaps the comparison cell's mean has decided nothing.

### Cost of the evaluation

~24–40 calls per replay at ~2.5–3.3k input tokens. Per replay: ~$0.06 Haiku, ~$0.12
Sonnet, ~$0.30 Opus. The full grid (3 models × 2 arms × 4 transcripts × 3 repeats = 72
replays) is roughly **$10–13** and a few hours of wall clock. The decision-critical subset —
Haiku-`counts` vs Sonnet-`baseline` vs Sonnet-`counts`, 4 transcripts, 3 repeats = 36
replays — is about **$4**. Start with the subset.

### Metrics and the decision rule: reuse §6, do not invent new ones

The pre-registered rule stands: **an arm wins if it improves Gini and minimum-turns without
tripping a guardrail, across at least two transcripts** (self-repetition stays 0;
picks-from-the-last-two stays ≥15%; the reasons stay specific, judged by reading 10 sampled
reasons per arm). Reusing it is the whole value of having pre-registered it — a model change
does not get a rule of its own.

Three additions specific to a model change:

1. **Report `unresolved` per cell.** Selection now marks a fallback (`selection_fallback`,
   landed 2026-09-15), so a run of identical picks can finally be read as either a model
   preference or a fallback storm. A bigger model should resolve to a cast member at least
   as often as Haiku; if `unresolved` rises, that alone is disqualifying regardless of Gini.
2. **A cost column beside the Gini column.** The decision is Gini *per dollar*, not Gini.
   An arm that improves Gini by 0.02 for +53% on the run has not won anything, and a table
   without the cost column invites pretending otherwise.
3. **Confirm the temperature drop empirically on the first cell.** litellm's map says
   dropped; a single probe call with `drop_params=False` should raise
   `UnsupportedParamsError` for Sonnet/Opus and succeed for Haiku. Cheap, and it converts
   §2 from "the library says" to "measured", which is the standard the rest of this
   evaluation was held to.

### Stage 2, and the thing replay cannot answer

Replay holds the transcript fixed, so it ranks pick *sequences*, not conversations. If a
model+arm wins Stage 1, it needs one live 40-turn run before adoption, compared against
`renewal-renewal-opus` on the same definition: same distribution metrics, plus whether a
turn the new selector chose reads as responsive. A fairer conversation that no longer
follows itself is not an improvement, and only a human reading the transcript can say.

### The order of work

1. Fix run-cost metering to include selection (§3) — the money bug the change amplifies.
2. Run the decision-critical subset: Haiku-`counts`, Sonnet-`baseline`, Sonnet-`counts`,
   4 transcripts, 3 repeats. Report mean ± range with a cost column.
3. Only if Sonnet wins under the §6 rule *and* the cost column survives inspection: make
   the `ROLE_DEFAULTS` change with the prose and test updates in §1, then one live run.
4. If the prediction in §4 holds instead, close this doc with the numbers and adopt
   intervention A on Haiku. That is a result, not a failure.

---

## 6. Results — the sweep ran 2026-09-15. The answer is no.

The decision-critical subset from §5, exactly as specified: three cells × 4 transcripts × 3
repeats, closed loop, `max_tokens=120` (the engine's own cap), `temperature=0.3` requested.
36 replays, 1,344 selection calls. No harness changes; the CLI was invoked three times per
cell and the rows aggregated.

### Per cell, per transcript (Gini mean of 3 repeats, with the range)

| cell | transcript | gini mean | range | min turns | dyad | coverage | last-two % | **unresolved %** |
|---|---|---|---|---|---|---|---|---|
| haiku-counts | renewal-renewal | 0.233 | 0.22–0.26 | 2.0 | 4.3 | 10.0 | 37.5 | 4.2 |
| haiku-counts | renewal-renewal-2 | **0.197** | 0.19–0.21 | 2.0 | 4.0 | 14.7 | 20.8 | 0.0 |
| haiku-counts | renewal-opus40 | 0.227 | 0.21–0.24 | 3.0 | 6.0 | 27.0 | 49.2 | 0.0 |
| haiku-counts | supply-bridge-2 | 0.263 | 0.26–0.27 | 1.0 | 3.0 | 14.0 | 33.3 | 0.0 |
| sonnet-baseline | renewal-renewal | 0.323 | 0.31–0.33 | 1.0 | 5.0 | 12.0 | 48.6 | 0.0 |
| sonnet-baseline | renewal-renewal-2 | 0.400 | 0.29–0.47 | 0.7 | 5.0 | 16.0 | 48.6 | 1.4 |
| sonnet-baseline | renewal-opus40 | 0.237 | 0.20–0.28 | 2.3 | 4.7 | 26.0 | 54.2 | 3.3 |
| sonnet-baseline | supply-bridge-2 | 0.383 | 0.36–0.41 | **0.0** | 3.0 | never | 41.7 | 0.0 |
| sonnet-counts | renewal-renewal | 0.310 | 0.26–0.35 | 1.3 | 5.3 | 12.3 | 33.3 | **43.1** |
| sonnet-counts | renewal-renewal-2 | 0.277 | 0.12–0.39 | 1.7 | 4.7 | 19.7 | 37.5 | **41.7** |
| sonnet-counts | renewal-opus40 | 0.180 | 0.11–0.23 | 4.3 | 5.0 | 7.7 | 38.3 | **50.8** |
| sonnet-counts | supply-bridge-2 | 0.250 | 0.23–0.26 | 1.0 | 3.3 | 13.7 | 23.6 | **31.9** |

### Pooled, with the cost column

| cell | n | gini | sd | unresolved | **$/40-turn run** | vs Haiku | sweep spend |
|---|---|---|---|---|---|---|---|
| haiku-counts | 12 | **0.230** | 0.027 | **0.89%** | **$0.136** | — | $1.28 |
| sonnet-baseline | 12 | 0.336 | 0.078 | 1.49% | $0.351 | **2.6×** | $3.31 |
| sonnet-counts | 12 | 0.254 | 0.081 | **43.15%** | $0.360 | 2.6× | $3.46 |

Costs are measured, not modelled: token usage was sampled (12 real calls per cell, prompt
and completion tokens from the provider) and the measured tokens-per-char ratio applied to
every prompt in the sweep. Two corrections to §3 fall out of that:

- **Sonnet is 2.6× Haiku on this workload, not 2×.** Identical prompt text reports **0.3202
  tokens/char on Sonnet 5 against 0.2362 on Haiku 4.5** — 36% more input tokens for the same
  bytes, a tokenizer difference I had no reason to expect and would not have caught by
  reading the price list.
- **Completion is ~80 tokens, not the ~40 I assumed.** §3's per-run figures were therefore
  low: measured $0.136 (Haiku) and $0.351 (Sonnet) per 40-turn run against $0.139/$0.278
  predicted.

### The pre-registered §6 rule, applied

- **`sonnet-counts` is disqualified on the guardrail, not on Gini.** The rule says a rise in
  `unresolved` is disqualifying regardless of distribution. It resolved **43% of its picks to
  nobody**, so nearly half its turns were chosen by the random fallback rather than by
  Sonnet — its apparently-decent 0.254 is substantially a measurement of `random.choice`.
- **`sonnet-baseline` loses to `haiku-counts` on both primary metrics, on 4 of 4
  transcripts.** Gini 0.336 vs 0.230 pooled; per transcript 0.323/0.400/0.237/0.383 against
  0.233/0.197/0.227/0.263, with non-overlapping repeat ranges on three of the four. Minimum
  turns: worse or equal on all four, including **one persona who never spoke at all**
  (Dr. Emily Chen, supply-bridge-2) — the exact failure the whole exercise exists to remove.
- **Nothing reaches the adoption target.** No cell approaches Gini ≤ 0.15 or coverage ≤ 8.
  Under the committed rule **no arm is adopted, and the bigger model is rejected.**

### Why `sonnet-counts` failed to name anybody — diagnosed, not guessed

On every failure `finish_reason == "length"`, `completion_tokens == 120`, and
**`message.content` came back empty** — a truncated reply yields no partial JSON at all, so
there is nothing to match a name against. Sonnet's reasons under the counts prompt average
**80–87 output tokens against the engine's 120-token cap**, so the right tail of the
distribution crosses it. Haiku averages 75 and crosses it far less often (0.89%).

Raising the cap to 400 (addendum, 2 repeats × 4 transcripts, $2.58) **reduces but does not
remove it**: 26% of calls still finished on `length`, 21% still unresolved, Gini 0.239–0.298.
And the loop matters again, as it did in `SPEAKER-SELECTION-EVALUATION.md` §8 — the same arm
run **open** loop at the same 120-token cap is far less affected (unresolved 4/24 and 1/40,
vs 41.7% and 50.8% closed). The closed-loop protocol shows the model participation counts
that contradict the transcript it can read, and it argues with them at length. So:

- The **production** risk with a Sonnet selector is real but nearer the open-loop 2.5–17%
  than the 43% in the table, and it is now visible rather than silent, because
  `selection_fallback` (landed earlier today) marks exactly this case.
- The **measurement** consequence is that `sonnet-counts` cannot be scored by this harness
  at the engine's cap. Any future attempt needs the cap raised *and* the loop artefact
  handled, and it would then be measuring a selector that costs 2.6× to get a number that
  Haiku already beats.

### Two things the harness cannot judge, stated so the table is not over-read

- **Self-repetition is not measurable in replay.** The arm is told the *transcript's* last
  speaker, not its own previous pick, so consecutive identical picks in the pick sequence are
  not the engine's self-repetition metric. The 0-of-72 guardrail from §2 was measured on real
  runs and remains untested here.
- **Reason quality did not degrade — if anything Sonnet's are better.** Sampled reasons cite
  specifics ("He hasn't weighed in yet and can speak to how boards would actually treat this
  state-by-state legal-theory exercise"). Sonnet is not bad at the task. It is more expensive,
  worse on fairness with the current prompt, and it overruns an output budget sized for Haiku.

### The prediction held

§4 registered: *Haiku + counts beats Sonnet-with-the-current-prompt on Gini, at zero extra
cost*. It won **4 of 4 transcripts on Gini and 4 of 4 on minimum turns, at 39% of the price.**
The mechanism argument in `SPEAKER-SELECTION-EVALUATION.md` §3 was right: the moderator's
failure is that it cannot see who is overdue, and showing it beats paying for a model that
still cannot.

### One number that got worse under repetition

§8 of the evaluation doc reported `counts` on Haiku at 0.17 and 0.15 on two transcripts —
single pass. With 3 repeats the same cell pools at **0.230 (sd 0.027, range 0.19–0.27)**. The
single-pass table was optimistic, exactly as the §5 warning about repeats predicted. The
missing control is now **haiku-baseline with 3 repeats** — this sweep has no same-protocol
baseline for the counts arm to beat, only §8's single passes (0.26–0.38). That is the next
measurement, and it is cheap ($1.28).

### Spend

Primary sweep $8.05, addendum $2.58, probes and diagnostics ~$1.0 — **~$11.6 total against a
$4 estimate in §5**. The estimate was low for the same two reasons the cost table was:
completion tokens twice what I assumed, and Sonnet's tokenizer counting 36% more input. The
per-call figure in §3 ($0.0035 on Haiku) was accurate; the per-replay figure was not.

---

## 7. The cap is gone (2026-09-15), and that was the whole "refusal"

`max_tokens` has been **removed from the selection call entirely** — it was 120 with
cognition on, 50 without. Verified first that Bedrock accepts an omitted `maxTokens` on both
Haiku 4.5 and Sonnet 5 through litellm (`finish_reason=stop`, 42–55 tokens out), because a
silently-rejected parameter is how this file got written in the first place.

Re-running the cell that §6 had to disqualify, same protocol, no cap:

| transcript | gini | min | dyad | coverage | **unresolved** |
|---|---|---|---|---|---|
| renewal-renewal-2 | 0.18 | 3 | 3 | 11 | **0** (was 41.7%) |
| renewal-opus40 | 0.23 | 3 | 4 | 7 | **0** (was 50.8%) |

**The cap was the entire cause.** Sonnet was never refusing or failing to follow the format;
it was being cut off mid-reply, and a truncated reply comes back as empty content rather than
a partial object. Uncapped, it resolves every single pick, and its Gini (0.18 / 0.23, one
pass each) is in the same region as `haiku-counts` (0.197 / 0.227, three passes) — which
means §6's *cost* argument now carries the whole decision, not the guardrail. Sonnet is still
2.6× the price for no measured fairness gain, so `ROLE_DEFAULTS` stays on Haiku, but the
honest statement is that Sonnet was disqualified on a defect of ours rather than on merit.

Also changed: a truncated reply that costs us the name is now recorded as
`selection_fallback="truncated"`, distinct from `"unresolved"`. They need opposite fixes —
one is an output-budget problem, the other a prompt or resolver problem — and collapsing them
is what let an output-budget problem hide inside what looked like a prompt problem. A
truncated reply whose `speaker` field survived is still counted as a real decision.

Costs nothing in the normal case: output tokens are billed by use and the observed replies
are 40–90 tokens. The residual risk is a runaway reply billed to the model's own ceiling
(128k on Sonnet 5, ~$1.28), which is why `finish_reason` is now read instead of ignored.

**The other caps were not touched**, and one of them is the same shape of hazard: `reflection`
also sits at 120 tokens for a one-sentence belief. `validation` (50) writes pass/fail and
`naming` (60) writes two words, so both have room; `voice` (2048) is comfortable against a
measured mean of ~220 tokens per utterance. Reflection is the one worth checking next.
