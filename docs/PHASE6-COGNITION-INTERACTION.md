# Structured personas × cognition — pre-registered

**Status:** COMPLETE, on the second attempt. The first attempt found cognition
**completely non-functional** against Haiku 4.5 — there was nothing to interact with —
so two engine defects were fixed and the experiment re-run on Sonnet 5. Both attempts
are recorded below. Nothing above the line was edited.

**Headline: the pre-registered leak does not happen.** Two of three predictions were
wrong, one metric is callable, and `requires-escalation` fired for the first time in
fifteen runs.

## The gap

All nine runs measured so far held **cognition OFF**. That was deliberate — memory and
reflection would have confounded the persona variable, and the original premise
validation set the precedent. But it means the configuration a real user is most
likely to enable, *convictions plus memory plus reflection*, has never been tried.

## Why this is the interesting interaction

With cognition on, three things change that bear directly on Phase 6's design:

1. **Every turn forms 0-2 memories, and 5 are injected into each subsequent prompt.**
   Those memories are written by the persona *about its own reasoning*.
2. **Reflection condenses recent memories into a higher-level "belief"** every 4 turns
   (`agent.reflected`, importance 0.9, tagged `reflection`).
3. So a persona's own prior reasoning starts competing for prompt space with the
   structured block that defines its convictions.

That sets up a specific risk worth naming in advance, and it is a **correctness** risk
rather than a quality one:

> **`underlying_concern` may leak via memory.** The concern is withheld by the code
> path — it reaches only its owner's prompt, never the moderator's, never the event
> log, never the dossier. But nothing stops the persona from *forming a memory* whose
> content encodes the concern, and that memory is then injected into its own later
> prompts alongside the "do not volunteer this" instruction. If the memory wins, the
> persona says out loud the thing the whole design exists to keep unsaid — and it
> would do so by a route none of the leakage tests cover, because all of them check
> prompt construction, not generated memory content.

The second question is whether convictions survive reflection at all: a belief
condensed from a discussion where four people disagreed with you is a plausible
vector for exactly the accommodation Phase 6 was built to prevent.

## Design

| | cognition | turns | n |
|---|---|---|---|
| `arm-e-mandatory` | **off** | 30 | 3 |
| `arm-g-cognition` | **on** (defaults: memory on, `reflection_every: 4`) | 30 | 3 |

Arm G is generated from the same script as every other arm and differs from Arm E in
**`config.cognition` only** — same prose, same structured data, same dismissal-rule
variant. Cognition's own defaults are used rather than tuned, because the question is
what a user gets, not what a best case looks like.

**30 turns rather than 15**, for two reasons:

- Reflection fires on `turn % 4 == 0` for whoever is speaking, so 15 turns yields 3
  reflections across 5 personas — most personas would never reflect at all. 30 turns
  yields 7.
- It **halves the resolution floor on the rate metrics.** At 15 turns one turn is
  0.067 of the rate; at 30 it is 0.033. The documented floor of ~0.2 on rates is the
  main reason so many earlier comparisons were uncallable, so this is worth the spend
  independent of the cognition question.

Arm E is re-run at 30 turns rather than reusing its 15-turn numbers, because turn
count would otherwise be a second variable. Results go in a separate directory so the
30-turn runs never get scored against the 15-turn ones.

Cost estimate: ~$1.00 (cognition adds 20-40% tokens on top of double length).

## What is being measured

**Primary — the leak.** Does any persona's `underlying_concern` reach the transcript?
Checked three ways, because a verbatim search alone would miss a paraphrase:

1. Verbatim / near-verbatim substring match against each authored concern.
2. The formed memories themselves (`agent.response` payloads carry them) searched for
   concern content — this catches the leak *route* even if the utterance never says it.
3. Reading the turns of any persona whose memories match.

**Any leak is a defect, not a metric.** If one occurs, the fix is a code change, not a
prompt tweak — most likely excluding withheld content from memory formation, or
filtering it out of the memory block.

**Secondary — does the behaviour hold?** Against Arm E at 30 turns, cognition off:

- Dismissal rate. Arm E at 15 turns measured 0.333.
- Talking-past on the blind judge. Arm E measured 1.00.
- Accommodation rate — the metric most likely to move, if reflection produces
  agreement.
- Position changes, and whether any is driven by something on the persona's own
  `evidence_that_shifts` list.

**No pass/fail threshold is set for the secondary metrics**, deliberately. This is not
a fix being validated; it is an unmeasured interaction being characterised, and
inventing a threshold would invite reading whatever comes out as a pass. The one
binary claim here is the leak check.

Every between-arm difference will be compared against the within-arm spread of the
same two arms, and any gap below it reported as not a difference — the noise gate
built after the last round.

## Prediction, recorded so it can be wrong

Cognition raises the accommodation rate and does **not** materially change the
dismissal rate. Reasoning: memories accumulate what *others* said, which is
accommodation pressure, while `dismisses` is re-rendered in full every turn and does
not decay. I expect at least one concern leak through memory across six runs.

---

## Results

### The experiment could not run: cognition was inert

Three runs in (2× Arm E, 1× Arm G, 30 turns each), the Arm G transcript showed
**every one of its 30 turns** carrying a markdown-fenced JSON blob as the utterance:

```
```json
{"utterance": "I want to be direct: I've watched this exact question kill two deals...
```

`json.loads` rejects that, and `_generate_response` degrades gracefully by keeping the
raw text as the utterance — so the run completed, cost **more** than the cognition-off
arm ($0.22 vs $0.21), and produced:

| | Arm G, 30 turns, 5 personas |
|---|---|
| Memories formed | **0** |
| Reflections | **0** |
| Rationales captured | **0** |
| `goal_served` captured | **0** |
| Turns whose utterance was a JSON blob | **30 / 30** |

Cognition has been a shipped feature since v0.2. Against this model it did nothing at
all, while looking like it worked.

The remaining runs were **stopped rather than completed** — three more runs of a
feature known to be inert would have cost ~$0.65 to measure nothing.

### Same root cause, two more defects

`response_format={"type": "json_object"}` is passed on every one of these calls and is
evidently not honoured on this provider path. Three call sites parsed strictly:

1. **`_generate_response`** — the one above. Cognition inert.
2. **The Phase 4a validation gate** (`validation.py`). Its confirmation call catches
   broad exceptions and **fails open**, so a `JSONDecodeError` became
   `violation: False`. The selective LLM confirmation had **never confirmed anything**
   against this model — which is exactly the item `PHASE4-REPORT.md` §4 flagged as
   unmeasured. That bill came due here.
3. **`_select_next_speaker`** — fell back to substring-matching a name out of the raw
   JSON, which usually still worked. The least harmful, and the reason nothing looked
   obviously wrong.

Two *other* modules had already solved this independently — `analysis.py` grew an
`_extract_json` in Phase 1.5, `naming.py` strips fences by hand — while the engine and
the gate never learned it. That duplication is why the lesson did not spread.

### The fix, verified live

`matrix_studio/jsonio.py`: one tolerant `extract_json_object`, used by all five call
sites. Tries the whole string, then a fenced block, then the widest `{...}` span; never
raises; returns `None` rather than a non-dict so callers cannot be handed something
they will `.get()` on.

Verified against the same model on a 6-turn run:

| | before | after |
|---|---|---|
| Fenced-JSON utterances | 30 / 30 | **0 / 6** |
| Memories formed | 0 | **12** |
| Rationale captured | 0 | **6 / 6** |
| `goal_served` captured | 0 | **6 / 6** |

`tests/test_jsonio.py` locks it, starting from the literal payload observed in the
broken run, plus a test asserting a strict `json.loads` **would** have failed on it —
so the tolerance cannot later be removed as unnecessary.

### A second defect, found by switching models

Switching to `bedrock/global.anthropic.claude-sonnet-5` failed differently: that model
accepts **only `temperature=1`**, and the engine passes 0.7 (settings), 0.3 (speaker
selection) and 0.0 (validation gate, reflection). Every path raised
`UnsupportedParamsError`, and the engine wrote the error text into the transcript **as
the character's speech**. The run reported `status: complete` and **$0.0000 cost** —
because no call had succeeded.

Fixed with `litellm.drop_params = True`. The two defects are independent and both
required: the JSON fix alone still breaks on Sonnet, and `drop_params` alone still
breaks on Haiku.

---

# Attempt 2 — Sonnet 5, complete

Six runs at 30 turns, **$2.588 total** ($0.431/run, cognition arms ~15% dearer).
Cognition verified functional and consistent: **33-34 memories and exactly 7
reflections** per Arm G run (matching `turn % 4` over 30 turns), **0** in every Arm E
run. Zero errors, zero fenced utterances. Single clean variable.

## Primary measurement: NO leak, in any run

Every memory and reflection was **read**, not filtered — 100 items across three runs.
The lexical pre-filter also reports clean, but it is a pre-filter and the verdict is
the reading.

**No persona's `underlying_concern` reached a memory, a reflection, or the
transcript.** The pattern is consistent and is the design working as intended: each
persona's memories reference its *condition*, never the private worry behind it.

| persona | withheld concern | what its memories actually say |
|---|---|---|
| Dana | *"I am the one who owns that failure"* | *"matches my stated condition for changing my mind, pending clean-machine verification"* |
| Tomas | *"I will be the one debugging a stale index … where nobody remembers the design"* | *"pushing for the resync command and a named staleness owner"* |
| Marcus | *"an unexplained jump mid-demo is the thing that ends the deal"* | *"the dollar delta against $0.0006/turn"* |

The concern is *operating* — it drives what each persona pushes for — without being
voiced. That is the whole design intent, and memory did not break it.

**The pre-registered prediction ("at least one concern leaks through memory across six
runs") was WRONG**, in the direction that favours the design.

## Secondary metrics, through the noise gate

| metric | cognition OFF | cognition ON | gap | noise | verdict |
|---|---|---|---|---|---|
| Dismissal rate | 0.200, 0.167, 0.167 | 0.067, 0.033, 0.267 | 0.056 | 0.234 | below noise |
| Accommodation rate | 0.000, 0.033, 0.067 | 0.000, 0.033, 0.000 | 0.022 | 0.067 | below noise |
| Citation rate | 0.100, 0.200, 0.200 | 0.133, 0.100, 0.300 | 0.011 | 0.200 | below noise |
| Cross-speaker similarity | 0.1562, 0.1600, 0.1443 | 0.1627, 0.1849, 0.1672 | 0.018 | 0.022 | below noise |
| **Mean turn length (chars)** | 771, 779, 871 | 570, 519, 599 | **244** | **100** | **CALLABLE** |

**The one callable result: cognition makes turns ~30% shorter** (807 → 563 chars).
Plausible mechanisms — the memory block competes for prompt space, and the structured
JSON envelope constrains the utterance field — but the cause is not measured, only the
effect.

**The second pre-registered prediction ("cognition raises the accommodation rate") was
also WRONG.** Accommodation did not rise; it was numerically *lower* and below noise
either way. The prediction on dismissal rate ("not materially changed") is *consistent*
with the data but unproven, since that comparison is also below noise.

## `requires-escalation` fired for the first time in fifteen runs

Priya holds the only `requires-escalation` viewpoint in the cast. Counting only her,
and only explicit refusal-to-concede-plus-escalate language:

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| cognition **ON** | turns 10, 25, 27 | — | turns 12, 14, 16 |
| cognition **OFF** | — | — | — |

**2 of 3 with cognition, 0 of 3 without.** The behaviour itself is exactly what the
firmness level specifies — turn 27, run 1:

> *"the derivation gap doesn't have an owner in this room who can sign off on it, and
> that's not something I can trade away for a converged yes — I'll take it to whoever
> owns cognition-fidelity sign-off myself."*

Refuses to concede, names who to escalate to, does not pretend to agree.

There is a plausible mechanism: escalation requires *sustained* pressure to concede,
and without memory every turn starts fresh. Her memory records
*"Simone proposed deferring the derivation problem to phase two in exchange for a fast
retrieval yes; I flagged this as requires-escalation"* — the pressure persisted.

**Suggestive, not established.** 2/3 versus 0/3 at n = 3 is nowhere near significant
(Fisher's exact ≈ 0.4), and the raw count of escalation language across *all* personas
is below the noise gate (mean 6.0 vs 1.33, but the cognition arm's own spread is 9).
The clean split is only visible when restricted to the persona who actually holds the
field, which is the correct restriction but also a smaller sample. This is the
strongest candidate finding here and the one most worth more runs.

## Reflections reinforce convictions rather than eroding them

The second pre-registered worry — that a belief condensed from a room full of
disagreement would produce accommodation — did not appear. Read against it, reflections
consistently *restate and harden* the persona's position while tracking its exit
condition:

> Tomas: *"I now believe that whether or not retrieval closes deals is beside the point
> for me — the feature only earns its place if it ships as a rebuildable SQLite-table
> lexical index with a live-demonstrated resync command."*

> Dana: *"the design has converged to something that meets my condition on paper, but
> until an actual clean-machine install-and-run with retrieval enabled is verified, no
> one … should call this shippable."*

Dana's is notable: the persona is using its own `evidence_that_shifts` as a memory
anchor and tracking whether it has been satisfied. Qualitative, from reading, and not
something the metrics capture.

## Instrument caveat — do NOT compare these rates to the 15-turn Haiku numbers

Arm E scores dismissal **0.178** here against **0.333** in the 15-turn Haiku runs, and
accommodation **0.033** against **0.400**. Two variables changed at once (model *and*
turn count), and the `ACCOMMODATION` / `DISMISSAL` phrase lists were hand-validated
against **Haiku output at 15 turns**. Neither cross-model nor cross-length rate
comparison is valid without re-validating the lists, and the pre-registered ≥0.30
dismissal criterion was set at 15 turns and should not be applied to 30-turn runs.

The within-experiment comparisons above are unaffected: both arms are the same model,
same length, same lists.

## What this leaves open

- **More runs on `requires-escalation`.** The one finding worth chasing.
- **Cognition's turn-shortening effect** is callable but unexplained.
- **Re-validate the phrase lists against Sonnet output** before any rate here is quoted
  against a differently-configured run.
- **`reflection_every` was left at its default 4**, giving 7 reflections per 30-turn
  run spread over 5 personas. Whether more reflection erodes convictions is still
  untested — this measured the default, not the space.
