# How a run works

This page explains what happens between pressing Run and reading the summary, and why the pieces
are shaped the way they are. It covers the lifecycle, who speaks next, cognition, the validation
gate, branching, and the deployed architecture. For step-by-step instructions see
[`../how-to/`](../how-to/); for every setting and event type see [`../reference/`](../reference/).

The examples use an invented brief: whether a small software team should move from monthly to
weekly releases, argued by Noor (engineering lead), Tobias (support), Wren (finance) and Iris
(product).

## The idea everything else rests on: an event log

Every change in a run is an event appended to a log: a speaker chosen, a turn spoken, a memory
formed, a document retrieved, a validation check passed. After each turn the engine also saves a
full snapshot of the state. The log is the source of truth; everything you see is derived from it
([`../project/PROJECT-SPEC.md`](../project/PROJECT-SPEC.md) §4).

This one decision is what makes the rest possible. Branching is "copy the log to turn N and carry
on". Replay is reading the log. The dossier and the why-trace show only what the log recorded, so
a run with cognition off has no why-trace to show rather than a made-up one.

Snapshots are full per turn, not deltas. The original specification proposed deltas; Phase 2a chose
full snapshots because runs are short and reloading one row is simpler than replaying
([`../../matrix_studio/engine/simulator.py`](../../matrix_studio/engine/simulator.py), the
`_run_turns` docstring). That choice later made the move to one-turn-per-Lambda tractable, because
each turn could load its state in one read
([`../project/PHASE5-ORCHESTRATION-DESIGN.md`](../project/PHASE5-ORCHESTRATION-DESIGN.md) §1).

## The lifecycle

### 1. Creation

`POST /api/runs` validates the request before anything is spent:

- **Unknown config keys are refused**, not dropped. This was learned the hard way: a misplaced
  `config.model` used to be silently discarded, so a series of study runs that asked for one model
  ran on another and nothing said so ([`../../matrix_studio/api/app.py`](../../matrix_studio/api/app.py),
  `RunConfigModel`).
- **A binding to a knowledge base you cannot read is refused** (422), and a user over their
  monthly spend cap is refused (402). Both checks live in one function so the run route and the
  ensemble route cannot drift apart (`_preflight` in the same file).
- **The run row is written at once**, with status `pending`, and only then is the state machine
  started. A client that receives a 201 must have something to poll. The first deployment
  returned 201 for runs that never existed
  ([`../project/PHASE5-ORCHESTRATION-DESIGN.md`](../project/PHASE5-ORCHESTRATION-DESIGN.md) §7).

If research is on, its target collections are created here too, before anything is searched
(see [Evidence](evidence-and-retrieval.md)).

### 2. Research

The state machine's first state is always Research. When research is off it returns in
milliseconds. It runs unconditionally so that the run row is the only place that decides
whether research happens; a second copy of that decision in the execution input is the kind of
duplication that has shipped features inert before
([`../../matrix_studio/research_state.py`](../../matrix_studio/research_state.py)).

If research fails, the run continues without it. The state machine's catch sends a failed
Research state to Prepare, not to the failure handler, because "a search outage is not a reason to
lose a conversation the operator asked for"
([`../../infra/matrix_infra/stack.py`](../../infra/matrix_infra/stack.py);
[`../PERSONA-RESEARCH.md`](../PERSONA-RESEARCH.md) §5.2).

### 3. Prepare: turn 0

Before anyone speaks, the engine records the setup as events at turn 0: `sim.started`, one
`persona.structured` per structured persona (with the private fields removed), one
`assumption.made` per working assumption, the avatars, and the ingest and embedding of any
documents. All of this is one function shared by the local and deployed paths, because the two
were about to diverge (`begin_run` in
[`../../matrix_studio/engine/simulator.py`](../../matrix_studio/engine/simulator.py)).

Documents are indexed before turn 1 because a document attached after generation starts is too
late to influence the opening turns ([`../AWS-SERVERLESS-ARCHITECTURE.md`](../AWS-SERVERLESS-ARCHITECTURE.md) §8c).

### 4. The turns

Each turn, in order:

1. Any scheduled message due after the previous turn enters the transcript, marked as injected.
2. If moderator assumptions are on, every few turns the moderator checks whether a gap nobody
   can fill is blocking the room, and may add a working assumption.
3. The next speaker is chosen (next section).
4. The speaker's documents are searched, if retrieval is on.
5. The speaker's turn is generated.
6. The validation gate checks it, and may regenerate it once.
7. Citations in the turn are classified, and a position shift is flagged if the persona says
   its position moved.
8. If the persona asked a consultant a question, the consultant answers into the transcript.
9. Memories and reflections are recorded, if cognition is on.
10. A snapshot is saved.

The order is deliberate in places. Scheduled messages and new assumptions arrive before selection,
so the moderator and the next speaker both read them. A regenerated turn reuses the same retrieved
passages, because "the regeneration is of the utterance, not of the retrieval"
([`../../matrix_studio/engine/simulator.py`](../../matrix_studio/engine/simulator.py), `_run_turns`).

### 5. The closing round (optional)

A run that reaches its turn ceiling usually stops mid-argument. With `selection.closing_round` on,
the engine runs one more round at turn `max_messages + 1`. Everyone is asked at once, blind to each
other's answers, for their final position: what they can accept, what they cannot and why, and what
moved them. Nobody may pass.

Two details carry most of the weight:

- **It asks for positions and terms, not agreement.** The instruction ends: "Do not agree to
  something you do not agree with in order to close." The obvious wording, "work toward a
  consensus", was rejected because the Phase 6 work showed framing alone can move a visible
  behaviour from 0.000 to 0.333. An instruction to agree would reliably produce agreement, and
  nothing would tell a real resolution from a manufactured one (`_CLOSING` in
  [`../../matrix_studio/engine/simulator.py`](../../matrix_studio/engine/simulator.py)).
- **It runs only when the ceiling was reached.** A run that converged has already had everyone say
  they had nothing to add.

The closing statements are also what stance reads, when they exist. See
[Reading the results](reading-the-results.md).

### 6. The end

A run ends in one of these statuses:

| status | meaning |
|---|---|
| `complete` | reached its ceiling, or converged early (the completion event then carries `converged: true`, the turn and the reason) |
| `stopped` | you asked it to stop; the turn in flight finished first |
| `capped` | it hit a spend cap |
| `failed` | a turn exhausted its retries |
| `interrupted` | the process running it died |

`stopped` and `interrupted` are kept distinct so a run list can say which happened (README,
"Stopping a run"). Convergence is a field on `complete` rather than its own status: the list of
terminal statuses is duplicated in three places, and a missing entry in one copy had already caused
two bugs ([`../../matrix_studio/engine/simulator.py`](../../matrix_studio/engine/simulator.py), the
`sim.completed` payload). An earlier design document proposed a `converged` status; the code chose
otherwise.

Only a `complete` run gets an automatic summary. Summarising a stopped or capped run "would describe
a conversation that was cut off as though it had finished"
([`../../matrix_studio/orchestration.py`](../../matrix_studio/orchestration.py), `_summarise_once`).
You can still ask for one by hand.

## Who speaks next

There are four methods (`selection.method`). They differ on two questions: does one persona speak
per turn or everyone, and can they see each other while doing it
([`../../matrix_studio/state.py`](../../matrix_studio/state.py), `SelectionConfig`).

| method | per turn | can they see each other? | turn share |
|---|---|---|---|
| `moderated` (default) | one, chosen by a model | yes | a judgement, corrected by the fairness prompt |
| `rotation` | everyone, in cast order | yes, each sees the earlier speakers that round | equal by construction |
| `simultaneous` | everyone, at once | no, all answer the state as it stood when the round opened | equal by construction |
| `hybrid` | two blind rounds, then moderated | blind while opening, then yes | equal while opening, then judged |

A "turn" in the round-based methods is a whole round. A 10-turn simultaneous run with four
personas can produce up to 40 messages, which matters for cost
([`../../matrix_studio/forecast.py`](../../matrix_studio/forecast.py), `response_bounds`).

### Moderated

One model call per turn picks the next speaker. The moderator sees each persona's **public**
summary only (role and what it optimises for), never the private block, because the moderator's
prompt is the one place every persona appears at once. It sees the last ten messages.

Left alone, this moderator was measured to be unfair. Over three 24-turn runs one persona spoke
once in 24 turns, two personas alternated for up to seven turns in a row, and the whole cast had not
spoken until turn 10 or 11. The cause was mostly information: the moderator could not see who was
overdue ([`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md) §2–§3). Showing
it the turn counts and the fair share fixed most of it, and that is now on by default. Why that
intervention and not a stronger model or a hard floor is the story of
[Why the defaults are what they are](why-the-defaults.md).

Two more things are worth knowing:

- **When selection fails, a speaker is drawn at random and the event says so** (`selection_fallback`).
  The old fallback always picked the same person, so a failing call looked like the moderator
  favouring someone. The why-trace now says "drawn at random. Nothing chose them."
  ([`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md) §9).
- **The moderator may decline to pick anyone** if `stop_when_converged` is on (and cognition is
  on, because the plain selection prompt asks for a bare name and cannot express "nobody"). Two
  guards stand in front of that: everyone must have spoken at least once, and it takes two declines in a row.
  The asymmetry justifies the caution: "fifteen turns of filler cost about $0.30, fifteen turns of
  argument cut short cost the run" (`SelectionConfig.stop_when_converged`). It is off by default.

### Rotation and simultaneous

Both bypass the moderator. Rotation is what the fairness work approximates: everyone speaks once
per round, and the conversation stays cumulative because each speaker reads the earlier ones.

Simultaneous is genuinely concurrent and measurably more parallel. In the one recorded run, four
personas opened a round by answering the same question with none acknowledging the others
(`SelectionConfig`). Its benefit is that every position is on the table by round 1 with no
anchoring on whoever spoke first.

In both, a persona may pass, and a pass never reaches the transcript. A round in which everyone
passes ends the run as converged. That is "the strongest form this signal takes anywhere in the
engine": every persona was asked and every one declined
([`../../matrix_studio/engine/simulator.py`](../../matrix_studio/engine/simulator.py)).

### Hybrid

Hybrid exists because the simultaneous run's opening was its best part and its later rounds its
worst, while the moderated method's weakness is the opposite: it takes eight to eleven turns to
introduce the cast. So hybrid runs blind opening rounds, then switches to moderated selection. The
default of two opening rounds comes from one live run, where round 2 was the strongest and parallel
restatement began at round 3 (`SelectionConfig.hybrid_opening_rounds`). One run is thin support
for that number, and the code says it is one run.

## Cognition: memory, reflection and the why-trace

Cognition is optional. With it on, each turn is one structured call that returns the utterance plus
a one-sentence first-person rationale, the goal it served, and zero to two memories the persona
formed. Optional sub-features add relationship notes, goal updates and a ledger of pending threads.

- **Memory.** The persona writes its own memories. Before each turn the top five are put back into
  its prompt. They are ranked by importance, with recency breaking ties (`_retrieve_memories`).
- **Reflection.** Every four turns, whoever is speaking condenses its last eight memories into one
  belief, stored as a memory with importance 0.9 (`_reflect`). Because ranking is by importance
  first, reflections tend to stay in the prompt.
- **The why-trace.** The dossier's "why?" panel shows the rationale, the goal served, the memories
  that were in the prompt for that turn, the moderator's reason for choosing the speaker, and the
  validation record. It is built only from recorded events. A turn with no recorded rationale shows
  "not available"; nothing is generated afterwards to fill it
  ([`../../matrix_studio/api/app.py`](../../matrix_studio/api/app.py), `turn_trace`).

The reason for producing the rationale in the same call as the turn, rather than asking afterwards,
is the project's honesty rule: "a separate pass that 'explains' a finished turn is a
rationalization, not a cause" ([`../project/PHASE2C-REQUIREMENTS.md`](../project/PHASE2C-REQUIREMENTS.md)). The
memory the dossier shows is the memory the next turn actually read.

That does not make the rationale true. It is still the model describing itself.

Two measured facts about cognition, from
[`../project/PHASE6-COGNITION-INTERACTION.md`](../project/PHASE6-COGNITION-INTERACTION.md):

- It was **completely inert** against one model for a long time. The model wrapped its JSON in a
  code fence, a strict parser rejected it, and the run quietly kept the raw text: 30 of 30 turns,
  zero memories, at slightly higher cost than cognition off. A tolerant parser fixed it. The run
  now records `cognition_parsed: false` on any turn where this happens, so the two cases can be
  told apart.
- With it working, the one measurable effect at n = 3 per arm was **shorter turns**: about 30%
  (807 to 563 characters). Accommodation and dismissal rates did not move beyond noise.

The engine default is off; the launch form turns it on. See [Why the defaults are what they are](why-the-defaults.md).

## The validation gate

After a turn is generated and before it is committed, a gate checks it against the project's
priority order: world coherence, causality, continuity, agency, character consistency, then lower
goals ([`../project/PROJECT-SPEC.md`](../project/PROJECT-SPEC.md) §4a). Document citations are checked too
(`citation_integrity`).

The checks are mostly cheap and narrow:

- a speaker writing dialogue for another cast member ("Wren: …" in Noor's turn);
- a long verbatim repeat of a recent message;
- a phrase that flatly denies another participant a choice ("you have no choice");
- a speaker claiming to be someone else;
- a citation of a passage the speaker never had (see [Evidence](evidence-and-retrieval.md)).

A near-duplicate only raises a suspicion, and only then is a small model asked to confirm. Clean
turns never pay for an extra call
([`../../matrix_studio/validation.py`](../../matrix_studio/validation.py)).

On a violation the turn is regenerated once. If the second attempt also fails, it is emitted as-is
with a `validation.flagged` event. **The gate never edits model output.** Rewriting a turn in place
would fabricate what the persona said.

The gate's limits are stated in the Phase 4 report: it has no model of the world, so subtle
coherence and causality problems pass, and its real-model behaviour was not benchmarked when it
shipped ([`../../PHASE4-REPORT.md`](../../PHASE4-REPORT.md) §4). One later measurement exists: in a
40-turn comparison the citation check rejected one turn (2.5 per 100 turns), and that rejection was
judged a false positive. The cause, recalling a passage read on an earlier turn, has since been
fixed ([`../studies/CITE-INLINE.md`](../studies/CITE-INLINE.md), comparison 2).

## Branching and forking

A branch is a new run. It copies the parent's log up to turn N, applies at most one change, and
generates forward. The parent is never modified or re-run. The changes on offer include injecting
a message, editing a goal, adding or removing a persona, promoting an aside into the room, and
replacing or withdrawing a working assumption
([`../../matrix_studio/api/app.py`](../../matrix_studio/api/app.py), `BranchMutationModel`).

Forward of the fork the run is non-deterministic, and that is expected. The specification puts it
plainly: "we never re-run the original. The original branch is already recorded; we only ever
generate forward" ([`../project/PROJECT-SPEC.md`](../project/PROJECT-SPEC.md) §4a, determinism note). That is also
why intervention is branching rather than live editing.

What a branch carries across, and what it does not, is worth knowing before you read one:

| carried across | not carried across |
|---|---|
| the transcript up to turn N | memories and reflections |
| the cast, including convictions and underlying concerns | relationship notes |
| the pending-thread ledger | goal updates made during the run (the cast's original goals are used) |
| the first-hand citation ledger | |
| the working assumptions in force at turn N | |
| the run's config (method, cognition, retrieval, and so on), with hidden agendas written out — `true` for a parent from before 2026-10-02 that recorded no value | |

The branch state is rebuilt by replaying the parent's log, and that replay reads responses, thread
events and consultant answers but not memory, reflection, relationship or goal events
([`../../matrix_studio/branching.py`](../../matrix_studio/branching.py), `reconstruct_at_turn`).
The Phase 4 report records this as a known gap ([`../../PHASE4-REPORT.md`](../../PHASE4-REPORT.md)
§4). The Phase 2c requirements say a branch reconstructs cognitive state from the fork snapshot; the
code does not do that. A *resume*, by contrast, loads the run's own snapshot and keeps its memories.

Three related operations are easy to confuse:

- **Resume** continues the same run from its last checkpoint.
- **Branch** forks a new run from turn N of an existing one.
- **Start fresh from this setup** copies the definition into the new-run form. Nothing is replayed.
  A copied setup deliberately leaves the earlier run's research behind
  ([`../PERSONA-RESEARCH.md`](../PERSONA-RESEARCH.md) §5.1).

A fork with a different value is one draw on each side. It shows you a difference; it cannot tell
you the change caused it. For that, the same change has to be an ensemble variable
([`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md) §3.3;
[`../../matrix_studio/injections.py`](../../matrix_studio/injections.py)).

## The deployed architecture, in brief

Matrix Studio runs only on AWS. There is no laptop mode, by decision
([`../project/PROJECT-SPEC.md`](../project/PROJECT-SPEC.md) §8.2 and §8.5).

```
browser ──▶ CloudFront (the app) ──▶ API Gateway (JWT check) ──▶ API Lambda
                                                                   │ StartExecution
                                                                   ▼
                          Step Functions:  Research ─▶ Prepare ─▶ Turn ⇄ CheckContinue ─▶ Finalise
                                                                   │
                       DynamoDB (runs, events, snapshot pointers) · S3 (snapshot and document bodies)
                       S3 Vectors (one index per knowledge base) · Bedrock (models)
```

### Why a state machine, and one turn per Lambda

The first plan ran the whole conversation inside the API Lambda as a background task. It could not
work. Lambda freezes the execution environment when the handler returns, so the background task
simply stopped; the measured invocation billed 8 ms. Waiting for the run inside the request is no
better, because API Gateway's limit is 30 seconds and a 20-turn run takes minutes. So the phase was
cancelled and Step Functions became the only way a run executes at all
([`../project/AWS-IMPLEMENTATION-PLAN.md`](../project/AWS-IMPLEMENTATION-PLAN.md), Phase 4 "CANCELLED").

Each Turn invocation generates one turn (or one round) and returns. That shape buys several things
([`../project/PHASE5-ORCHESTRATION-DESIGN.md`](../project/PHASE5-ORCHESTRATION-DESIGN.md)):

- **Retries per turn.** Bedrock throttling is retried with backoff on that turn alone.
- **Safe retries.** Each turn first trims anything written past the last checkpoint, so a turn that
  died half-written is not appended twice (§5).
- **State is reloaded, never passed along.** A state's input and output are capped at 256 KB, and
  snapshots reach 2.2 MB. The execution payload carries only ids and counters (§3).
- **A one-turn stop.** A stop is a flag on the run row, checked after the turn in flight is saved.
  So the turn being generated finishes, its tokens are not wasted, and no further turn starts (§6).

`CheckContinue` is a plain Choice over the turn's own output rather than a native DynamoDB read.
A native read would use the state machine's own role, which is not scoped to one user, on the
hottest path in the system ([`../../infra/matrix_infra/stack.py`](../../infra/matrix_infra/stack.py)).
Every storage access otherwise uses credentials scoped to the run's owner, so a missing filter in
code cannot read another user's data ([`../AWS-SERVERLESS-ARCHITECTURE.md`](../AWS-SERVERLESS-ARCHITECTURE.md) §3).

One recurring lesson from this design is recorded several times in the code: anything a turn needs
across turns has to live in durable state, because every deployed turn is a fresh invocation. The
moderator's decline counter and the closing round both worked in tests and did nothing in production
at first, for exactly this reason. The citation ledger had the same flaw and was caught before the
move to Step Functions ([`../../matrix_studio/state.py`](../../matrix_studio/state.py),
`SimSnapshot.decline_streak` and `firsthand_citations`;
[`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md) §15;
[`../project/PHASE5-ORCHESTRATION-DESIGN.md`](../project/PHASE5-ORCHESTRATION-DESIGN.md) §2).

### Why polling, not WebSockets

The browser asks for new events every three seconds. On the deployed system nothing could push
them: each turn runs in a worker Lambda that exits, and the in-memory broker that fans events out
to WebSocket clients lives in the API process, which never sees them. Polling an endpoint that
already existed needed almost no new code. Three seconds against a measured 6–13 second turn means
a turn appears within about half its own duration
([`../../frontend/src/hooks/useRunStream.ts`](../../frontend/src/hooks/useRunStream.ts);
[`../project/PHASE5-ORCHESTRATION-DESIGN.md`](../project/PHASE5-ORCHESTRATION-DESIGN.md) §8).

The specification also records a cost argument: about 1.4 KB per poll on a 30-turn run, cheaper than
holding a connection open per viewer through API Gateway ([`../project/PROJECT-SPEC.md`](../project/PROJECT-SPEC.md)
§8.3). The WebSocket path is kept for the single-process server, where it is sub-second, and the
design for adding it on AWS (DynamoDB Streams into a fan-out Lambda) is deferred
([`../AWS-SERVERLESS-ARCHITECTURE.md`](../AWS-SERVERLESS-ARCHITECTURE.md) §9).

### Cost accounting

Every model call records its cost on the event it produced: the speaker selection, the turn, each
validation check, a rejected attempt, a reflection, a consultant's answer, an avatar. **A run's cost
is the sum of its events**, and the monthly per-user cap is charged from the same number, so the
page and the cap cannot disagree ([`../../matrix_studio/orchestration.py`](../../matrix_studio/orchestration.py),
`execute_slice`). This was not always so. Until 26 September 2026 a run recorded only its voice and
reflection calls; on one run, speaker selection alone came to $0.20 charged to nobody
([`../../matrix_studio/forecast.py`](../../matrix_studio/forecast.py);
[`../../matrix_studio/engine/simulator.py`](../../matrix_studio/engine/simulator.py), `SpeakerChoice`).
Some older documents still describe selection and avatars as unmetered.

The monthly cap **fails closed**: if the spend cannot be read, the run is refused. The argument is
that the read uses the same credentials as every other read in the run, so a failure there means the
run would be failing anyway, while failing open would let a cap silently stop applying
(`over_monthly_cap`).

The cost forecast shown before launch is priced from this account's own past runs, not from a
price list. Two price-list estimates in one week were three to four times low, because they missed
whole kinds of spend. Where no comparable history exists, the forecast says "not yet measured" and
reports the rest as a minimum ([`../../matrix_studio/forecast.py`](../../matrix_studio/forecast.py)).

One limit worth knowing: the optional per-run cap (`MAX_RUN_COST_USD`, off by default) is checked
inside the engine against the personas' running totals. Those totals include turns, validation
checks and reflections, but not speaker selection, avatars, consultant answers or moderator
assumption checks, which are costed on their events only. So a run's displayed cost can exceed its
per-run cap.

## Related

- [What Matrix Studio is for](what-matrix-studio-is-for.md)
- [Why the defaults are what they are](why-the-defaults.md)
- The record: [`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md),
  [`../project/PHASE5-ORCHESTRATION-DESIGN.md`](../project/PHASE5-ORCHESTRATION-DESIGN.md),
  [`../AWS-SERVERLESS-ARCHITECTURE.md`](../AWS-SERVERLESS-ARCHITECTURE.md),
  [`../project/PHASE2C-REQUIREMENTS.md`](../project/PHASE2C-REQUIREMENTS.md)
