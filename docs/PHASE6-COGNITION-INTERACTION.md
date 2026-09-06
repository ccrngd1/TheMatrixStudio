# Structured personas × cognition — pre-registered

**Status:** INTERRUPTED. The first three runs found that **cognition was completely
non-functional against this model**, so the interaction could not be measured — there
was nothing to interact with. The defect is fixed (`matrix_studio/jsonio.py`) and the
experiment is still to run. Everything below the line records that; nothing above it
was edited.

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

### The interaction question is still open

Nothing in the pre-registered plan has been answered. The concern-leak check has an
instrument now (`scripts/check_concern_leak.py`) but no valid data: it ran against the
broken transcripts and its candidates were vocabulary overlap with the topic, not
leaks. Re-run required, now that cognition actually does something.
