# Event log reference

Every run has an event log: an ordered list of events, one per thing that happened, written while the
conversation is generated. This page lists every `event_type` the product writes, with its payload, when
it is written, and how the log is read. It is derived from `matrix_studio/engine/simulator.py` (the turn
loop and branch mutations), `matrix_studio/orchestration.py` (the deployed turn loop),
`matrix_studio/api/app.py` and `matrix_studio/api/manager.py` (the API and the live stream),
`matrix_studio/storage/dynamo.py` (storage and ordering), and the payload builders in
`matrix_studio/shifts.py`, `assumptions.py`, `injections.py`, `experts.py`, `citations.py`,
`validation.py`, `pressure.py` and `retrieval.py`.

See also: [explanation](../explanation/), [how-to guides](../how-to/), [`http-api.md`](http-api.md),
[`run-config.md`](run-config.md), [`data-model.md`](data-model.md).

- [Event envelope](#event-envelope)
- [Ordering, `seq` and `turn`](#ordering-seq-and-turn)
- [Terminal events](#terminal-events)
- [Cost on events](#cost-on-events)
- [Reading the log](#reading-the-log)
- [All event types](#all-event-types)
- Event types by family: [run lifecycle](#run-lifecycle) · [turn-0 setup](#turn-0-setup) ·
  [speaker selection](#speaker-selection) · [persona turns](#persona-turns) ·
  [documents and retrieval](#documents-and-retrieval) · [validation](#validation) ·
  [consultants](#consultants) · [working assumptions](#working-assumptions) ·
  [cognition](#cognition) · [pending threads](#pending-threads) · [checkpoints](#checkpoints) ·
  [branch interventions](#branch-interventions)

---

## Event envelope

Every event, whether read from `GET /api/runs/{ref}/events` or received on the WebSocket stream, has
this shape (`event_row_to_wire` in `matrix_studio/api/manager.py`).

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | The run the event belongs to. On a branch, events copied from the parent carry the branch's id. |
| `turn` | integer | The turn the event belongs to. See [turn values](#turn-values). |
| `seq` | integer | Position in the run's log. Unique within a run. See [ordering](#ordering-seq-and-turn). |
| `event_type` | string | One of the [event types](#all-event-types) below. |
| `agent_name` | string or null | The persona, consultant or voice the event is about; null for run-level events. Listed per event below. |
| `payload` | object | Event-specific fields. Always an object on the wire: a stored payload that fails to parse is served as `{}`. |

Storage also records `created_at` (epoch seconds) on each event. It is not part of the wire shape; it
surfaces only as `last_event_at` on the run (see [`data-model.md`](data-model.md)).

Payload fields marked "optional" below are omitted from the payload when they do not apply, rather than
sent as null, unless the table says otherwise.

```json
{
  "run_id": "<run-id>",
  "turn": 3,
  "seq": 14,
  "event_type": "speaker.selected",
  "agent_name": "Ada Byrne",
  "payload": {"speaker": "Ada Byrne", "candidates": ["Ada Byrne", "Tomas Reyes", "Iris Okafor"]}
}
```

---

## Ordering, `seq` and `turn`

### `seq`

| Property | Value |
|---|---|
| Assignment | A per-run counter, incremented once per event in the order the engine emits them. A fresh run's `sim.started` is `seq` 0. |
| Uniqueness | Enforced by a conditional write. Writing a `seq` that already exists for the run raises `StorageError` ("event (run=..., seq=...) already exists"). |
| Continuity | Not guaranteed. Gaps appear where events are deleted (see below), and in a branch whose parent's log had them. |
| Reuse | Possible. After events are deleted (below), numbering continues at the highest remaining `seq` + 1, so deleted `seq` values are issued again. |

Events are deleted from a log in exactly three places. "Checkpoint turn" is the highest turn with a saved
snapshot.

| Operation | Deletes |
|---|---|
| Resume in place (`POST /api/runs/{ref}/resume`), and every deployed turn before it generates | Every event with `turn` greater than the checkpoint turn. Normally none on a deployed turn; a retried turn's partial events otherwise. |
| Resume in place | Every terminal event (`sim.completed`, `sim.failed`, `sim.interrupted`, `sim.stopped`, `sim.capped`). |
| Branch (`POST /api/runs/{ref}/branch`) | The terminal events inherited from the parent when the fork is at the parent's last turn. |

### Read order

Both read paths return events sorted by `(turn, seq)`, not by `seq` alone (`get_events_after` in
`storage/dynamo.py`). The two orders differ when an event is emitted with a lower `turn` than an event
emitted before it:

| Case | Effect in `(turn, seq)` order |
|---|---|
| A converged run (an honoured `speaker.declined`, or a round in which everyone passed) | `sim.completed` (turn N-1) sorts before the `speaker.declined`, `speaker.selected` and `agent.passed` events of turn N that preceded it. |
| `POST .../agents/{name}/regenerate-avatar` | The new `avatar.ready` (turn 0, highest `seq`) sorts among the turn-0 events. |

A client that keeps its own cursor should track the highest `seq` it has seen, not the `seq` of the last
item in a response. The SPA does this (`frontend/src/hooks/useRunStream.ts`) and re-sorts by `seq`.

### Turn values

| Event | `turn` |
|---|---|
| `sim.started`, `persona.structured`, `avatar.ready`, operator `assumption.made`, `document.ingested`, `document.failed`, `document.embedded` | 0 |
| Scheduled injection (`agent.response` with `injection`) | The `after_turn` it was scheduled for. It shares that turn number with the turn it follows. |
| `assumption.checked`, moderator `assumption.made` | The number of turns completed when the check ran. |
| Everything a turn produces (`speaker.selected` through `checkpoint.saved`) | The turn being generated, 1-based. |
| Round modes (`rotation`, `simultaneous`, the opening rounds of `hybrid`, the closing round) | The round number. Every persona's events in that round share it. |
| Closing round | The run's turn budget + 1. |
| Branch `inject_message`, `promote_aside`, `adaptive_pressure` | The fork turn + 1. |
| Branch `replace_assumption`, `withdraw_assumption` | The fork turn. |
| `sim.completed`, `sim.stopped`, `sim.capped` | The last turn generated. On a converged run, the turn before the decline. |
| `sim.failed` | The turn being generated when the exception was raised. |
| `sim.interrupted` | The highest `turn` in the log. |

### Order of events within one turn

A moderated turn emits, in this order, whichever of the following apply:

1. Scheduled injections due after the previous turn (`agent.response`, `injected: true`).
2. `assumption.checked`, then `assumption.made` if the check produced one.
3. `speaker.declined` (moderator nominated nobody).
4. `speaker.selected`.
5. `document.retrieved` or `document.unsupported`.
6. `validation.checked` (one per attempt), then `validation.flagged` if the last attempt still failed.
7. `agent.response`, or in a round `agent.passed` or `closing.missing` (which end that persona's slot).
8. `position.shift`.
9. A consultation: `document.retrieved` (with `consultant: true`) then `expert.answered`.
10. `memory.formed` (0 to 2), `goal.updated`, `relationship.updated` (one per persona named).
11. `thread.opened` (0 to 2), `thread.resolved`, `thread.abandoned`.
12. `agent.reflected`.
13. `checkpoint.saved`.
14. `sim.stopped` or `sim.capped`, if the run ends here.

In a round, items 4 to 13 repeat once per persona, and items 1 and 2 run once at the start of the round.
`sim.completed` follows the last turn.

---

## Terminal events

Five event types mean the engine will write nothing more for the run. The same set is defined in
`api/manager.py` (`TERMINAL_EVENTS`), `storage/dynamo.py` (`TERMINAL_EVENT_TYPES`) and the SPA
(`frontend/src/lib/runStatus.ts`).

| Event | Run status written with it | Written by |
|---|---|---|
| `sim.completed` | `complete` | The engine, when the turn budget (and any closing round) is used, or the run converges. |
| `sim.stopped` | `stopped` | The engine after the turn in flight, or the deployed loop when it sees a stop flag set during a turn. |
| `sim.capped` | `capped` | The engine (per-run cap) or the deployed loop (monthly per-user cap). |
| `sim.failed` | `failed` | The engine, when an exception escapes the turn loop. |
| `sim.interrupted` | `interrupted` | The startup sweep, for a run left `running` by a previous process. |

A terminal status can be written without a terminal event:

| Path | Status | Event |
|---|---|---|
| Deployed: a turn or prepare Lambda exhausts its retries (Step Functions `MarkFailed`) | `failed` | None |
| Deployed: a slice finds the budget already met and calls `finalise` | `complete` (or `stopped` if a stop was requested) | None |
| Local: the background task crashes before the turn loop starts (for example during turn-0 work) | stays `running` | None |
| Local: a resume task crashes | `failed` | None |

---

## Cost on events

Each model call records its cost in USD as `cost_usd` on the event it produced. A run's in-run cost is
the sum of `payload.cost_usd` over every event in its log (`get_run_stats`), returned as
`total_cost_usd` and itemised by event type as `cost.by_kind` on `GET /api/runs/{ref}`.

| Event | `cost_usd` covers | Present |
|---|---|---|
| `agent.response` | The persona's reply that was kept. 0 for injected messages. | Always |
| `agent.passed`, `closing.missing` | The reply that produced no message. | Always |
| `speaker.selected` | The moderator's selection call. | Only when non-zero |
| `validation.checked` | The confirmation call, plus the discarded attempt's generation when a regeneration follows. | Only when non-zero |
| `agent.reflected` | The reflection call. | Always |
| `expert.answered` | The consultant's answer. | Always |
| `assumption.checked` | The moderator's gap check. | Always |
| `avatar.ready` | The `AVATAR_COST_USD` list price (default 0.08). | Only when an image was produced at run start |
| `document.embedded` | Embedding the run's attached chunks. | Always |
| `pressure.applied` | Generating the pressure event. | Always |

Not on any event: the honoured `speaker.declined` selection call, a portrait made by
`regenerate-avatar`, summaries, asides, research, and knowledge-base embeddings at upload.

The terminal events' `total_cost_usd` is a different number: the sum of the per-persona totals in the
run's snapshot. Those totals include rejected attempts, validation, passes and reflections, and exclude
speaker selection, consultations, assumption checks, avatars, embeddings and pressure. It is not summed
into the run's cost (the key is `total_cost_usd`, not `cost_usd`), and the per-run cap
(`MAX_RUN_COST_USD`) is checked against it.

---

## Reading the log

### `GET /api/runs/{ref}/events`

Returns events strictly after a `seq`. The polling path, and on the deployed stack the only live
delivery. Full route reference: [`http-api.md`](http-api.md).

| Query parameter | Type | Default | Notes |
|---|---|---|---|
| `after_seq` | integer | `-1` | Events with `seq` greater than this. Any negative value reads from the start. |
| `limit` | integer | none | At most this many events, taken in `seq` order before sorting. Not range-checked: `0` returns none, and a negative value drops events from the end. |

`{ref}` is a run id or a run name. Another owner's run, or no such run, is `404 {"detail": "Run not found"}`.

```json
{
  "run_id": "<run-id>",
  "events": [
    {"run_id": "<run-id>", "turn": 0, "seq": 0, "event_type": "sim.started", "agent_name": null,
     "payload": {"topic": "Should the town library open on Sundays?", "agent_count": 3}}
  ]
}
```

### WebSocket `/api/runs/{ref}/stream`

Replays the stored log, then forwards live events. Available when the API process is also the process
generating the run: the local `serve` command. The deployed stack has no WebSocket route (the API is an
HTTP API, and turns run in worker Lambdas with no broker), so clients there poll `GET .../events`; the
SPA polls every 3 seconds alongside the socket.

| Step | Behaviour |
|---|---|
| Authentication | The same identity as HTTP routes (`current_user_ws`). Without one, the connection is refused before it is accepted. |
| Unknown run | One frame `{"event_type": "error", "payload": {"detail": "Run not found"}}`, then close. This frame has no `run_id`, `turn` or `seq`. |
| Replay | Every stored event, in `(turn, seq)` order. |
| Live tail | Only when this process holds a live broker for the run. Events with `seq` at or below the highest replayed `seq` are skipped. Closes after the first terminal event. |
| No live broker | Closes after the replay. If the replay held no terminal event, first sends one synthesized frame: `event_type` `sim.completed` when the run's status is `complete`, otherwise `sim.failed`, with `payload: {"status": "<run status>"}`, `agent_name` null, `turn` = the run's `branch_turn` or 0, and `seq` = highest replayed `seq` + 1. |

### Frames that are not stored

The WebSocket's `error` frame and its synthesized terminal frame exist only on the socket. Every other
event is stored before it is forwarded. With the CLI's `--no-db` flag nothing is stored at all.

---

## All event types

34 event types. "Emitted by" names the module that writes the event.

| Event type | Emitted by | `agent_name` | When |
|---|---|---|---|
| [`sim.started`](#simstarted) | engine | null | Turn 0, first event of a fresh run. |
| [`sim.completed`](#simcompleted) | engine | null | The run finished its budget, closing round, or converged. |
| [`sim.stopped`](#simstopped) | engine, orchestration | null | An operator stop took effect. |
| [`sim.capped`](#simcapped) | engine, orchestration | null | The per-run or monthly cost cap was reached. |
| [`sim.failed`](#simfailed) | engine | null | An exception escaped the turn loop. |
| [`sim.interrupted`](#siminterrupted) | API startup sweep | null | A run was left `running` by a previous process. |
| [`persona.structured`](#personastructured) | engine | persona | Turn 0, structured personas on. |
| [`avatar.ready`](#avatarready) | engine, API | persona | A portrait was generated, or regenerated. |
| [`document.ingested`](#documentingested) | engine | persona or consultant | Turn 0, a cast or consultant document was indexed. |
| [`document.failed`](#documentfailed) | engine | persona or consultant | Turn 0, a cast or consultant document could not be indexed. |
| [`document.embedded`](#documentembedded) | engine | null | Turn 0, vector or hybrid retrieval on. |
| [`speaker.selected`](#speakerselected) | engine | the speaker | Each persona's turn or round slot. |
| [`speaker.declined`](#speakerdeclined) | engine | null | The moderator nominated nobody. |
| [`agent.response`](#agentresponse) | engine | the speaker or voice | A message entered the transcript. |
| [`agent.passed`](#agentpassed) | engine | the persona | A persona passed in a round. |
| [`closing.missing`](#closingmissing) | engine | the persona | A closing-round reply had no words. |
| [`position.shift`](#positionshift) | engine | the speaker | A reply announced that the speaker's position moved. |
| [`document.retrieved`](#documentretrieved) | engine | the speaker or consultant | Retrieval returned passages. |
| [`document.unsupported`](#documentunsupported) | engine | the speaker | Retrieval returned nothing and disclosure is on. |
| [`validation.checked`](#validationchecked) | engine | the speaker | Each validation attempt. |
| [`validation.flagged`](#validationflagged) | engine | the speaker | The last allowed attempt still failed validation. |
| [`expert.answered`](#expertanswered) | engine | the consultant | A consultant answered a persona's question. |
| [`assumption.made`](#assumptionmade) | engine | null | A working assumption came into force. |
| [`assumption.checked`](#assumptionchecked) | engine | null | The moderator checked for a blocking gap. |
| [`assumption.withdrawn`](#assumptionwithdrawn) | engine | null | A branch withdrew an assumption. |
| [`memory.formed`](#memoryformed) | engine | the speaker | Cognition memory on, a reply formed a memory. |
| [`goal.updated`](#goalupdated) | engine | the speaker | Dynamic goals on, a reply changed the goal list. |
| [`relationship.updated`](#relationshipupdated) | engine | the speaker | Relationships on, a reply updated a stance toward someone. |
| [`agent.reflected`](#agentreflected) | engine | the speaker | Every `reflection_every` turns, cognition on. |
| [`thread.opened`](#threadopened) | engine | the speaker | Threads on, a reply planted a thread. |
| [`thread.resolved`](#threadresolved) | engine | the speaker | Threads on, a reply paid off an open thread. |
| [`thread.abandoned`](#threadabandoned) | engine | the speaker | Threads on, a reply marked an open thread moot. |
| [`checkpoint.saved`](#checkpointsaved) | engine | null | A snapshot was saved after a message. |
| [`pressure.applied`](#pressureapplied) | engine (branch) | `"Narrator"` | An `adaptive_pressure` branch mutation. |

---

## Run lifecycle

### `sim.started`

**When:** Turn 0 of a fresh run, before any other event. Not emitted by a branch or a resume (a branch
inherits its parent's copy). On the deployed stack the prepare state skips turn-0 work if the run already
has one.

| Field | Type | Notes |
|---|---|---|
| `topic` | string | The run's topic. |
| `agent_count` | integer | Number of cast members. |

Runs imported with `scripts/import_thematrix_run.py` instead carry `topic`, `cast` (string array) and
`config` (`{"imported": true}`).

```json
{"run_id": "<run-id>", "turn": 0, "seq": 0, "event_type": "sim.started", "agent_name": null,
 "payload": {"topic": "Should the town library open on Sundays?", "agent_count": 3}}
```

### `sim.completed`

**When:** The turn loop ends because the budget is used (including any closing round) or the run
converged. Terminal; the run's status becomes `complete`.

| Field | Type | Notes |
|---|---|---|
| `total_turns` | integer | The last turn number. Includes the closing round when there was one. |
| `message_count` | integer | Messages in the conversation, including injected messages and consultant answers. |
| `total_cost_usd` | number | Sum of per-persona totals. See [cost on events](#cost-on-events). |
| `closing_round_empty` | `true` | Optional. Every closing reply had no words (each recorded as `closing.missing`). |
| `converged` | `true` | Optional. The run ended before its budget. |
| `converged_at_turn` | integer | Optional, with `converged`. The last turn that produced a message. |
| `converged_reason` | string | Optional, with `converged`. The moderator's reason, or `"every participant passed this round"`. |
| `turns_unused` | integer | Optional, with `converged`. Budget minus `converged_at_turn`. |

Imported runs carry `total_turns`, `total_cost_usd` (0.0) and `imported: true`.

```json
{"run_id": "<run-id>", "turn": 9, "seq": 61, "event_type": "sim.completed", "agent_name": null,
 "payload": {"total_turns": 9, "message_count": 9, "total_cost_usd": 0.4182, "converged": true,
             "converged_at_turn": 9, "converged_reason": "Every position is stated and the hours question is parked",
             "turns_unused": 11}}
```

### `sim.stopped`

**When:** An operator stop (`POST /api/runs/{ref}/stop`) takes effect after the turn in flight is saved.
Emitted by the engine when it sees the stop before the next turn, or by `orchestration.stop_now` when the
deployed loop sees the stop flag after a turn. In round modes the check runs after each persona's
message, so a run can stop mid-round. Terminal; status `stopped`.

| Field | Type | Notes |
|---|---|---|
| `total_turns` | integer | The last turn generated. |
| `message_count` | integer | Messages in the conversation. |
| `total_cost_usd` | number | Sum of per-persona totals. |

```json
{"run_id": "<run-id>", "turn": 4, "seq": 30, "event_type": "sim.stopped", "agent_name": null,
 "payload": {"total_turns": 4, "message_count": 4, "total_cost_usd": 0.1903}}
```

### `sim.capped`

**When:** A cost cap is reached after a turn. Two variants: the per-run cap (`MAX_RUN_COST_USD`, engine,
checked after each message) and the monthly per-user cap (`MAX_USER_MONTHLY_COST_USD` or
`USER_SPEND_CAPS_JSON`, deployed loop only, checked after each turn). Terminal; status `capped`.

| Field | Type | Notes |
|---|---|---|
| `total_turns` | integer | The last turn generated. |
| `message_count` | integer | Messages in the conversation. |
| `total_cost_usd` | number | Sum of per-persona totals. |
| `cap_usd` | number | The cap that applied. |
| `scope` | `"user-monthly"` | Monthly variant only. Absent for the per-run cap. |
| `user_spend_usd` | number or null | Monthly variant only. The month's spend; null when it could not be read. |
| `error` | string | Monthly variant only, optional. Why the spend could not be read (the cap fails closed). |

```json
{"run_id": "<run-id>", "turn": 6, "seq": 44, "event_type": "sim.capped", "agent_name": null,
 "payload": {"total_turns": 6, "message_count": 6, "total_cost_usd": 0.31, "scope": "user-monthly",
             "cap_usd": 25.0, "user_spend_usd": 25.07}}
```

### `sim.failed`

**When:** An exception escapes the engine's turn loop. Terminal; status `failed`. Not emitted when a
deployed turn exhausts its retries, nor for a failure before the turn loop (see
[terminal events](#terminal-events)).

| Field | Type | Notes |
|---|---|---|
| `error` | string | The exception message. |

```json
{"run_id": "<run-id>", "turn": 5, "seq": 37, "event_type": "sim.failed", "agent_name": null,
 "payload": {"error": "<exception message>"}}
```

### `sim.interrupted`

**When:** At API startup with `STARTUP_SWEEP` on (the default; off on the deployed stack), once for each
run still marked `running`. Terminal; status `interrupted`.

| Field | Type | Notes |
|---|---|---|
| `reason` | string | `"server restarted while the run was still generating"`. |
| `at_turn` | integer | The highest `turn` in the log; also the event's `turn`. |

```json
{"run_id": "<run-id>", "turn": 7, "seq": 52, "event_type": "sim.interrupted", "agent_name": null,
 "payload": {"reason": "server restarted while the run was still generating", "at_turn": 7}}
```

---

## Turn-0 setup

### `persona.structured`

**When:** Turn 0, once per cast member with a `structured` block, only when `config.personas.enabled` is
true. Ordered after `sim.started`.

| Field | Type | Notes |
|---|---|---|
| `agent_name` | string | The persona. |
| `structured` | object | The persona's structured block (see [`run-config.md`](run-config.md)), with `validity` and `underlying_concern` removed from every viewpoint. |
| `withhold_concerns` | boolean | From `config.personas`. |
| `dismissal_rule` | string | From `config.personas`, normalised to a variant name. |
| `evidence_lean` | boolean | From `config.personas`. |

```json
{"run_id": "<run-id>", "turn": 0, "seq": 1, "event_type": "persona.structured", "agent_name": "Ada Byrne",
 "payload": {"agent_name": "Ada Byrne", "structured": {"role": "Branch librarian", "viewpoints": [
   {"position": "Sunday hours need two staff on shift", "firmness": "firm",
    "evidence_that_shifts": ["a volunteer rota that covers the desk"]}]},
   "withhold_concerns": true, "dismissal_rule": "mandatory", "evidence_lean": true}}
```

### `avatar.ready`

**When:** Turn 0, once per persona when avatars are on (`config.generate_avatars`, default
`ENABLE_AVATARS`), in the order the images finish; emitted whether or not an image was produced. Also
appended by `POST /api/runs/{ref}/agents/{name}/regenerate-avatar`, at turn 0 with the next `seq`.

| Field | Type | Notes |
|---|---|---|
| `agent_name` | string | The persona. |
| `portrait_key` | string or null | Content-addressed image key, served by `GET .../agents/{name}/avatar?v=`. Null when generation was disabled, filtered or failed. |
| `cost_usd` | number | Optional. Run-start events only, and only when an image was produced. |
| `portrait_b64` | string | Legacy. Present only on runs recorded before images moved out of the log. |

The last `avatar.ready` for a persona is the current portrait.

```json
{"run_id": "<run-id>", "turn": 0, "seq": 2, "event_type": "avatar.ready", "agent_name": "Tomas Reyes",
 "payload": {"agent_name": "Tomas Reyes", "portrait_key": "<portrait-key>", "cost_usd": 0.08}}
```

### `document.ingested`

**When:** Turn 0, when `config.retrieval.enabled` is true, once per cast member or consultant document
(`document_texts` entries first, then `documents` paths) that was indexed. Knowledge-base documents are
not ingested per run and produce no event.

| Field | Type | Notes |
|---|---|---|
| `document_id` | string | The stored document's id. |
| `persona_name` | string | The cast member or consultant it is scoped to. |
| `title` | string | The given title; for an untitled pasted document, `pasted-<n>.txt`. |
| `media_type` | string | Detected media type. |
| `char_count` | integer | Characters of extracted text. |
| `chunk_count` | integer | Passages it was split into. |
| `source` | `"inline"` | Optional. Present for `document_texts` entries; absent for server paths. |

```json
{"run_id": "<run-id>", "turn": 0, "seq": 5, "event_type": "document.ingested", "agent_name": "Iris Okafor",
 "payload": {"document_id": "<document-id>", "persona_name": "Iris Okafor", "title": "Footfall survey 2025",
             "media_type": "text/plain", "char_count": 8400, "chunk_count": 9, "source": "inline"}}
```

### `document.failed`

**When:** Turn 0, retrieval on, a cast or consultant document could not be read or indexed. The run
continues without it. The two variants use different field names.

| Field | Type | Notes |
|---|---|---|
| `persona_name` | string | Both variants. |
| `title` | string | Inline variant. |
| `source` | `"inline"` | Inline variant. |
| `reason` | string | Inline variant. The error. |
| `path` | string | Path variant. The server path given. |
| `error` | string | Path variant. The error. |

```json
{"run_id": "<run-id>", "turn": 0, "seq": 6, "event_type": "document.failed", "agent_name": "Iris Okafor",
 "payload": {"persona_name": "Iris Okafor", "path": "./background/missing.pdf", "error": "<error message>"}}
```

### `document.embedded`

**When:** Turn 0, after ingestion, when retrieval is on and `config.retrieval.mode` is `vector` or
`hybrid`. Emitted once, including when there was nothing to embed.

| Field | Type | Notes |
|---|---|---|
| `embedded` | integer | Chunks given a vector. |
| `skipped` | integer | Chunks returned without a vector. |
| `tokens` | integer | Embedding tokens. |
| `cost_usd` | number | Embedding cost. |
| `model` | string | Embedding model used. |
| `error` | string | Optional. Why embedding failed; retrieval then falls back to lexical search. |

```json
{"run_id": "<run-id>", "turn": 0, "seq": 7, "event_type": "document.embedded", "agent_name": null,
 "payload": {"embedded": 9, "skipped": 0, "tokens": 2100, "cost_usd": 0.00004, "model": "<embedding-model>"}}
```

---

## Speaker selection

### `speaker.selected`

**When:** Before every persona's reply: once per moderated turn, and once per persona in a round. In a
round no model is called; the queue (cast order) decides.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | Who speaks. |
| `candidates` | string array | Every persona in the run at that point, including the last speaker. |
| `method` | string | Optional, rounds only. The run's `selection.method` (`rotation`, `simultaneous`, `hybrid`; `moderated` for a moderated run's closing round). |
| `round` | integer | Optional, rounds only. Equals `turn`. |
| `closing` | `true` | Optional. Closing round only. |
| `reason` | string | Optional. The moderator's one-line reason, when cognition is on and it gave one. On a `declined_override`, the reason it gave for declining. |
| `selection_fallback` | string | Optional, present only when no model chose: `call_failed` (the call raised), `unresolved` (the reply named nobody in the cast), `truncated` (the reply hit the length limit), `declined_override` (the moderator declined but the guards did not allow it; the least-heard persona other than the last speaker was called). The first three pick at random among personas other than the last speaker. |
| `cost_usd` | number | Optional, when the selection call cost more than 0. |

```json
{"run_id": "<run-id>", "turn": 2, "seq": 15, "event_type": "speaker.selected", "agent_name": "Iris Okafor",
 "payload": {"speaker": "Iris Okafor", "candidates": ["Ada Byrne", "Tomas Reyes", "Iris Okafor"],
             "reason": "She has the footfall numbers the others keep asking for", "cost_usd": 0.0021}}
```

### `speaker.declined`

**When:** In a moderated turn, the moderator replied `{"speaker": null}`. Possible only when
`config.cognition.enabled` and `config.selection.stop_when_converged` are both true. Honoured (the run
converges) only when every persona has spoken and this is the second consecutive decline; otherwise a
`speaker.selected` with `selection_fallback: "declined_override"` follows on the same turn.

| Field | Type | Notes |
|---|---|---|
| `reason` | string or null | What the moderator said is finished. |
| `consecutive` | integer | Consecutive declines, including this one. Carried across turns and slices. |
| `everyone_spoke` | boolean | Whether every persona had spoken. |
| `honoured` | boolean | Whether this decline ended the run. When true, the event's turn produced no message and `sim.completed` follows at the previous turn. |

```json
{"run_id": "<run-id>", "turn": 10, "seq": 60, "event_type": "speaker.declined", "agent_name": null,
 "payload": {"reason": "Every position is stated and the hours question is parked", "consecutive": 2,
             "everyone_spoke": true, "honoured": true}}
```

---

## Persona turns

### `agent.response`

**When:** A message enters the transcript. Three sources: a persona's generated reply (the common case),
a scheduled injection from `config.injections`, and a branch injection (`inject_message`,
`promote_aside`, `adaptive_pressure`). The run's `turn_count` counts every `agent.response`, injected or
not.

Generated reply:

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona. |
| `message` | string | What was said. A leading `Name:` with the speaker's own name, quotes enclosing the whole reply, and any valid `ASK <consultant>:` line are removed. If the model call raised, the text is `[Error generating response: <error>]` with zero tokens and cost. |
| `tokens_in` | integer | Prompt tokens of the kept attempt. |
| `tokens_out` | integer | Completion tokens of the kept attempt. |
| `cost_usd` | number | Cost of the kept attempt. |
| `closing` | `true` | Optional. The reply was a closing statement (closing round). This flag is what identifies a closing statement in the log (`stance.closing_statements`). |
| `rationale` | string | Optional. Cognition on, and the reply gave one. |
| `goal_served` | string | Optional. Cognition on, and the reply gave one. |
| `cognition_parsed` | `false` | Optional. Cognition on and the structured reply did not parse; its rationale, memories and updates were lost. Absent when it parsed. |
| `memory_refs` | string array | Optional. Cognition memory on. Ids of the memories that were in the prompt (may be empty). |
| `thread_refs` | string array | Optional. Cognition threads on. Ids of the open threads that were in the prompt (may be empty). |
| `document_refs` | integer array | Optional. Chunk ids of the passages in the prompt, when retrieval returned any. |
| `citation_provenance` | object array | Optional. Retrieval on and the reply cites sources. Each: `label` (string, `"title #n"`), `title` (string), `kind` (`firsthand`, `secondhand`, `mention`, `unverified`), `attributive` (boolean), `via` (string, optional: who was credited), `reason` (string, optional: why unverified). |

Injected message (generated by no model):

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The voice. A cast member's name puts the words in that persona's mouth; any other name is a voice outside the cast. |
| `message` | string | The injected text. |
| `tokens_in`, `tokens_out`, `cost_usd` | 0, 0, 0.0 | Always zero. |
| `injected` | `true` | Always. |
| `source` | string | `operator` (scheduled), `user` (branch default, or the `source` given), `aside` (`promote_aside`), `pressure` (`adaptive_pressure`, speaker `"Narrator"`). |
| `injection` | string | Scheduled only. The injection's key, `I1` to `I5` in config order. |
| `scheduled_after_turn` | integer | Scheduled only. The configured `after_turn`. |

Runs imported with `scripts/import_thematrix_run.py` carry `content` instead of `message`, no `speaker`
(use `agent_name`), and zero tokens and cost.

```json
{"run_id": "<run-id>", "turn": 21, "seq": 140, "event_type": "agent.response", "agent_name": "Ada Byrne",
 "payload": {"speaker": "Ada Byrne", "message": "I can accept Sunday afternoons with a volunteer rota. I cannot accept a single member of staff alone on the desk.",
             "tokens_in": 3120, "tokens_out": 88, "cost_usd": 0.0106, "closing": true,
             "rationale": "The rota answers my staffing condition", "goal_served": "keep the desk safely staffed"}}
```

```json
{"run_id": "<run-id>", "turn": 3, "seq": 22, "event_type": "agent.response", "agent_name": "A resident's letter",
 "payload": {"speaker": "A resident's letter", "message": "Many of us work weekdays and cannot reach the branch before it closes.",
             "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "injected": true, "source": "operator",
             "injection": "I1", "scheduled_after_turn": 3}}
```

### `agent.passed`

**When:** In a round other than the closing round (`rotation`, `simultaneous`, the opening rounds of
`hybrid`), a persona passed: the reply set `"pass": true`, or (backstop) its text opens with a pass
phrase such as "pass" or "nothing to add". Nothing enters the transcript, and no checkpoint follows. A
round in which every persona passed ends the run as converged.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona. |
| `round` | integer | The round number (equals `turn`). |
| `tokens_in` | integer | Tokens of the reply. |
| `tokens_out` | integer | Tokens of the reply. |
| `cost_usd` | number | Cost of the reply. |

```json
{"run_id": "<run-id>", "turn": 4, "seq": 31, "event_type": "agent.passed", "agent_name": "Tomas Reyes",
 "payload": {"speaker": "Tomas Reyes", "round": 4, "tokens_in": 2400, "tokens_out": 30, "cost_usd": 0.0077}}
```

### `closing.missing`

**When:** In the closing round (`config.selection.closing_round`), a persona's reply had no words. Passing
is not offered in the closing round, so this records a failed answer, not a decision. Nothing enters the
transcript, and no checkpoint follows.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona. |
| `round` | integer | The closing round's number (budget + 1; equals `turn`). |
| `tokens_in` | integer | Tokens of the reply. |
| `tokens_out` | integer | Tokens of the reply. |
| `cost_usd` | number | Cost of the reply. |
| `declared_pass` | boolean | Whether the reply set `"pass": true` anyway. |
| `reply` | string | The raw model reply, first 1500 characters. |

```json
{"run_id": "<run-id>", "turn": 21, "seq": 143, "event_type": "closing.missing", "agent_name": "Iris Okafor",
 "payload": {"speaker": "Iris Okafor", "round": 21, "tokens_in": 3300, "tokens_out": 12, "cost_usd": 0.0101,
             "declared_pass": false, "reply": "{\"utterance\": \"\"}"}}
```

### `position.shift`

**When:** Directly after a generated `agent.response` whose text announces a realised change of the
speaker's own position ("you've persuaded me", "I concede", "I was wrong"), excluding conditional and
negated forms. Checked on every generated reply, including closing statements; not on injected messages
or consultant answers. Flag only: the message is not changed.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona. |
| `sentences` | string array | The sentences that announce the shift, each capped at 400 characters. |
| `credits` | object array | What the shift names, from the shift sentences or else the whole message. Each: `kind` (`assumption`, `scheduled message`, `consultant`, `persona`) and `name` (an assumption id such as `A1`, or a name). Empty when it names nothing. |
| `conditions` | object array | The persona's stated change-conditions for viewpoints whose `firmness` is `firm`, `non-negotiable` or `requires-escalation`. Each: `position`, `firmness`, `condition` (strings). Read from the cast member's `structured` block whether or not `config.personas` is enabled. |
| `matched_conditions` | string array | Conditions sharing at least two content words with the message. |
| `no_listed_condition` | boolean | True when `conditions` is non-empty and none matched. |

```json
{"run_id": "<run-id>", "turn": 8, "seq": 55, "event_type": "position.shift", "agent_name": "Ada Byrne",
 "payload": {"speaker": "Ada Byrne", "sentences": ["Iris, you've persuaded me that a volunteer rota can cover the desk."],
             "credits": [{"kind": "persona", "name": "Iris Okafor"}],
             "conditions": [{"position": "Sunday hours need two staff on shift", "firmness": "firm",
                             "condition": "a volunteer rota that covers the desk"}],
             "matched_conditions": ["a volunteer rota that covers the desk"], "no_listed_condition": false}}
```

---

## Documents and retrieval

### `document.retrieved`

**When:** Retrieval is on (`config.retrieval.enabled`) and returned at least one passage: before the
speaker's reply, or before a consultant's answer (with `consultant: true`). Records what was placed in the
prompt; passage text is not included.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona, or the consultant's plain name. |
| `query` | string | The query actually run. |
| `passages` | object array | Each: `chunk_id` (integer), `document_id` (string), `title` (string), `ordinal` (integer, the passage's position in its document), `score` (number, 4 decimals; its scale depends on `retrieval.mode`), `chars` (integer), `authority` (string, optional: `controlling`, `persuasive`, `commentary`, `unknown`), `origin` (string, optional: `researched` or `uploaded`). |
| `total_chars` | integer | Sum of passage lengths. |
| `consultant` | `true` | Optional. A consultant's lookup. |
| `researched_passages` | integer | Optional. How many passages came from research; present only when at least one did. Persona lookups only. |
| `floor_rejected` | integer | Optional. Matches dropped by `min_similarity`; present only when more than 0. Persona lookups only. |
| `kb_failures` | string array | Optional. Knowledge-base ids whose index could not be queried. Persona lookups only. |

```json
{"run_id": "<run-id>", "turn": 2, "seq": 16, "event_type": "document.retrieved", "agent_name": "Iris Okafor",
 "payload": {"speaker": "Iris Okafor", "query": "sunday footfall weekday closing",
             "passages": [{"chunk_id": 412, "document_id": "<document-id>", "title": "Footfall survey 2025",
                           "ordinal": 3, "score": 0.6121, "chars": 1180}],
             "total_chars": 1180}}
```

### `document.unsupported`

**When:** Retrieval is on, returned nothing for the speaker, and `config.retrieval.disclose_unsupported`
is true. The persona is asked to say in its own voice that it has nothing in front of it.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona. |
| `query` | string | The query that found nothing. |
| `floor_rejected` | integer | Optional. Matches dropped by the similarity floor; present only when more than 0. |

```json
{"run_id": "<run-id>", "turn": 5, "seq": 34, "event_type": "document.unsupported", "agent_name": "Tomas Reyes",
 "payload": {"speaker": "Tomas Reyes", "query": "overtime budget sunday", "floor_rejected": 2}}
```

---

## Validation

Both events require `VALIDATION_ENABLED` (default true). Validation runs on every generated persona reply,
including replies then recorded as a pass, and not on injected messages, consultant answers, or the
engine's `[Error generating response` marker.

### `validation.checked`

**When:** Once per attempt at a reply. A failed attempt is regenerated up to `VALIDATION_RETRY_BUDGET`
times (default 1), each with the same retrieved passages.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona. |
| `attempt` | integer | 0 for the first attempt. |
| `passed` | boolean | Whether this attempt passed. |
| `method` | string | Optional. `heuristic` (a deterministic rule decided) or `llm` (a suspicion was sent to a confirmation call). Absent when the attempt passed with no suspicion. |
| `principle` | string | Optional, failed attempts. `citation_integrity`, `coherence`, `continuity`, `agency`, `character_consistency` or `causality`. |
| `reason` | string | Optional, failed attempts. |
| `rejected_text` | string | Optional, failed attempts. The rejected reply, first 1500 characters. |
| `cost_usd` | number | Optional, when non-zero. |

```json
{"run_id": "<run-id>", "turn": 6, "seq": 41, "event_type": "validation.checked", "agent_name": "Tomas Reyes",
 "payload": {"speaker": "Tomas Reyes", "attempt": 0, "passed": false, "method": "heuristic",
             "principle": "coherence", "reason": "speaker 'Tomas Reyes' emitted dialogue attributed to 'Ada Byrne'",
             "rejected_text": "Ada Byrne: I think...", "cost_usd": 0.0094}}
```

### `validation.flagged`

**When:** The last allowed attempt still failed. That attempt is then recorded unchanged as the
`agent.response`.

| Field | Type | Notes |
|---|---|---|
| `speaker` | string | The persona. |
| `principle` | string | As in `validation.checked`. |
| `reason` | string | As in `validation.checked`. |
| `attempts` | integer | Attempts made. |

```json
{"run_id": "<run-id>", "turn": 6, "seq": 43, "event_type": "validation.flagged", "agent_name": "Tomas Reyes",
 "payload": {"speaker": "Tomas Reyes", "principle": "continuity",
             "reason": "utterance verbatim-repeats turn 4 by 'Tomas Reyes'", "attempts": 2}}
```

---

## Consultants

### `expert.answered`

**When:** A persona's reply ends with a line `ASK <consultant name>: <question>` naming a consultant in
`config.experts`, retrieval is on, and the run's `consult_limit` (default 6, counted from the
conversation) is not used up. Follows the persona's `agent.response` and `position.shift` on the same
turn. Only the last valid ask in a reply is answered. The answer joins the conversation that later
speakers read.

| Field | Type | Notes |
|---|---|---|
| `expert` | string | The consultant's name. |
| `speaker` | string | How the answer appears in the transcript: `"<name> (consultant)"`. |
| `asked_by` | string | The persona who asked. |
| `question` | string | The question. |
| `answer` | string | The answer. `"That isn't in my sources."` when the sources do not cover it; with a failure note when the call failed. |
| `document_refs` | integer array | Chunk ids of the consultant's passages. May be empty. |
| `citation_provenance` | object array | Optional. As on `agent.response`. |
| `tokens_in` | integer | |
| `tokens_out` | integer | |
| `cost_usd` | number | |
| `error` | string | Optional. Why the call failed, first 300 characters. |

```json
{"run_id": "<run-id>", "turn": 3, "seq": 23, "event_type": "expert.answered", "agent_name": "Mara Lind",
 "payload": {"expert": "Mara Lind", "speaker": "Mara Lind (consultant)", "asked_by": "Tomas Reyes",
             "question": "What does the staffing agreement say about Sunday premiums?",
             "answer": "Sunday shifts are paid at time and a half [Staffing agreement #2].",
             "document_refs": [88], "tokens_in": 900, "tokens_out": 40, "cost_usd": 0.0033}}
```

---

## Working assumptions

The assumptions in force at any turn are read from these events: a later `assumption.made` with the same
`id` replaces the earlier one, and `assumption.withdrawn` removes it (`assumptions.from_events`).

### `assumption.made`

**When:** Three cases. Turn 0, once per entry in `config.assumptions` (ids `A1`..`A8` in order). After a
gap check that produced one (moderator). At a branch fork, for a `replace_assumption` mutation.

| Field | Type | Notes |
|---|---|---|
| `id` | string | `A<n>`. A moderator assumption takes the next free number. A replacement keeps the replaced id. |
| `statement` | string | Up to 300 characters. |
| `basis` | string | Up to 300 characters; may be empty. |
| `source` | string | `operator` (config or replacement) or `moderator`. |
| `turn` | integer | 0, the completed turn of the check, or the fork turn. |
| `gap` | string | Moderator only. The unknown it fills. |
| `asks` | string array | Moderator only. The quoted asks found in the conversation (at least two messages). |
| `replaces` | string | Replacement only. The statement it replaces. |
| `replaced_source` | string | Replacement only. The replaced assumption's `source`. |

```json
{"run_id": "<run-id>", "turn": 4, "seq": 29, "event_type": "assumption.made", "agent_name": null,
 "payload": {"id": "A2", "statement": "About 300 visitors would use the branch on a Sunday afternoon.",
             "basis": "Iris's estimate from the survey", "source": "moderator", "turn": 4,
             "gap": "expected Sunday visitors", "asks": ["how many people would actually come", "we need a visitor estimate"]}}
```

### `assumption.checked`

**When:** `config.dynamic_assumptions.enabled` is true, at the start of a moderated turn or a round (never
in the closing round), when the completed turn count is a positive multiple of `every`, the moderator has
added fewer than `limit` assumptions, and none was added after this turn already. Emitted for every
check, whatever it decided.

| Field | Type | Notes |
|---|---|---|
| `after_turn` | integer | Completed turns; also the event's `turn`. |
| `proposed` | boolean | Whether an assumption was made. |
| `gap` | string | The gap, or `""`. |
| `cost_usd` | number | Cost of the check. |
| `rejected` | string | Optional. Why the engine discarded a proposal (for example a repeat, or asks not found in the conversation). Present only when the model proposed something. |
| `proposal` | object | Optional, with `rejected`. The discarded proposal as the model returned it. |
| `error` | string | Optional. Why the check call failed, first 300 characters. |

```json
{"run_id": "<run-id>", "turn": 8, "seq": 54, "event_type": "assumption.checked", "agent_name": null,
 "payload": {"after_turn": 8, "proposed": false, "gap": "", "cost_usd": 0.0015,
             "rejected": "repeats A2, already in force", "proposal": {"statement": "Roughly 300 people would come on Sundays."}}}
```

### `assumption.withdrawn`

**When:** At a branch fork, for a `withdraw_assumption` mutation. Turns after the fork no longer reason
from it.

| Field | Type | Notes |
|---|---|---|
| `id` | string | The withdrawn assumption. |
| `statement` | string | Its statement. |
| `turn` | integer | The fork turn. |

```json
{"run_id": "<run-id>", "turn": 6, "seq": 45, "event_type": "assumption.withdrawn", "agent_name": null,
 "payload": {"id": "A1", "statement": "The council will fund one extra shift a week.", "turn": 6}}
```

---

## Cognition

All four require `config.cognition.enabled`. They follow the speaker's `agent.response` on the same turn.

### `memory.formed`

**When:** `cognition.memory` is on (default when cognition is enabled) and the reply listed memories; up
to two per reply.

| Field | Type | Notes |
|---|---|---|
| `agent` | string | The persona. |
| `id` | string | Memory id (32 hex characters), as used in `memory_refs`. |
| `content` | string | The memory. |
| `importance` | number or null | 0.0 to 1.0 as the model gave it; null when absent or not a number. |
| `tags` | string array | |

```json
{"run_id": "<run-id>", "turn": 3, "seq": 24, "event_type": "memory.formed", "agent_name": "Ada Byrne",
 "payload": {"agent": "Ada Byrne", "id": "<memory-id>", "content": "Mara confirmed Sunday premiums are time and a half",
             "importance": 0.7, "tags": ["cost"]}}
```

### `goal.updated`

**When:** `cognition.goals_dynamic` is on and the reply's `goal_update` differs from the current goals.

| Field | Type | Notes |
|---|---|---|
| `agent` | string | The persona. |
| `before` | string array | Goals before. |
| `after` | string array | Goals after. |

```json
{"run_id": "<run-id>", "turn": 5, "seq": 36, "event_type": "goal.updated", "agent_name": "Tomas Reyes",
 "payload": {"agent": "Tomas Reyes", "before": ["keep the budget flat"], "after": ["keep the budget flat", "trial Sundays for one quarter"]}}
```

### `relationship.updated`

**When:** `cognition.relationships` is on; one event per other cast member named in the reply's
`relationship_updates` (the speaker itself and names outside the cast are ignored).

| Field | Type | Notes |
|---|---|---|
| `agent` | string | The persona. |
| `other` | string | The cast member the stance is toward. |
| `stance` | string | One line. |

```json
{"run_id": "<run-id>", "turn": 5, "seq": 37, "event_type": "relationship.updated", "agent_name": "Tomas Reyes",
 "payload": {"agent": "Tomas Reyes", "other": "Iris Okafor", "stance": "Her numbers are solid; I trust them"}}
```

### `agent.reflected`

**When:** `cognition.reflection_every` is above 0 (default 4) and the turn number is a multiple of it: the
speaker of that turn (each persona who spoke, in a round) condenses its last eight memories into one
belief. Skipped when the speaker has no memories or the call fails.

| Field | Type | Notes |
|---|---|---|
| `agent` | string | The persona. |
| `id` | string | The belief's memory id. Stored as a memory with importance 0.9 and tags `["reflection", "belief"]`. |
| `belief` | string | One first-person sentence. |
| `tokens_in` | integer | |
| `tokens_out` | integer | |
| `cost_usd` | number | |

```json
{"run_id": "<run-id>", "turn": 8, "seq": 57, "event_type": "agent.reflected", "agent_name": "Ada Byrne",
 "payload": {"agent": "Ada Byrne", "id": "<memory-id>", "belief": "I now think Sundays can work if the rota is real.",
             "tokens_in": 210, "tokens_out": 18, "cost_usd": 0.0004}}
```

---

## Pending threads

All three require `config.cognition.enabled` and `config.cognition.threads`. Distinct from aside threads
(`/api/threads/...`).

### `thread.opened`

**When:** The reply's `thread_updates.open` planted a thread; up to two per reply. An unknown
`thread_type` is recorded as `setup`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | Thread id (12 hex characters), as used in `thread_refs`. |
| `description` | string | |
| `thread_type` | string | `setup`, `promise`, `faction-action` or `deferred-consequence`. |
| `origin_turn` | integer | This turn. |
| `origin_agent` | string | The persona. |

```json
{"run_id": "<run-id>", "turn": 4, "seq": 32, "event_type": "thread.opened", "agent_name": "Iris Okafor",
 "payload": {"id": "<thread-id>", "description": "Iris promised to bring the volunteer sign-up numbers",
             "thread_type": "promise", "origin_turn": 4, "origin_agent": "Iris Okafor"}}
```

### `thread.resolved`

**When:** The reply's `thread_updates.resolved` named a thread that was open and was in this turn's
prompt. Other ids are ignored.

| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `description` | string | |
| `thread_type` | string | |
| `origin_turn` | integer | |
| `resolved_turn` | integer | This turn. |

```json
{"run_id": "<run-id>", "turn": 9, "seq": 63, "event_type": "thread.resolved", "agent_name": "Iris Okafor",
 "payload": {"id": "<thread-id>", "description": "Iris promised to bring the volunteer sign-up numbers",
             "thread_type": "promise", "origin_turn": 4, "resolved_turn": 9}}
```

### `thread.abandoned`

**When:** The reply's `thread_updates.abandoned` named a thread that was open and was in this turn's
prompt. Same payload as `thread.resolved`; `resolved_turn` is the turn it was abandoned.

| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `description` | string | |
| `thread_type` | string | |
| `origin_turn` | integer | |
| `resolved_turn` | integer | This turn. |

```json
{"run_id": "<run-id>", "turn": 9, "seq": 64, "event_type": "thread.abandoned", "agent_name": "Tomas Reyes",
 "payload": {"id": "<thread-id>", "description": "A council vote on the overtime line", "thread_type": "deferred-consequence",
             "origin_turn": 2, "resolved_turn": 9}}
```

---

## Checkpoints

### `checkpoint.saved`

**When:** After each generated `agent.response` and its follow-on events, once the turn's snapshot is
saved; and after a branch injection. In a round, once per persona who spoke (each overwrites the round's
snapshot). Not after `agent.passed` or `closing.missing`. Resume and retry restart from the highest turn
with a saved snapshot, which is read from the snapshot store rather than from these events.

| Field | Type | Notes |
|---|---|---|
| `turn` | integer | The snapshot's turn; equals the event's `turn`. |

```json
{"run_id": "<run-id>", "turn": 3, "seq": 27, "event_type": "checkpoint.saved", "agent_name": null,
 "payload": {"turn": 3}}
```

---

## Branch interventions

A branch's log starts with a copy of its parent's events up to and including the fork turn (same `turn`,
`seq` and payload). A mutation then writes at most the events below; `edit_goal`, `add_persona`,
`remove_persona` and `continue` write none and change only the fork snapshot or budget. Mutation kinds are
listed in [`http-api.md`](http-api.md).

| Mutation | Events at the fork |
|---|---|
| `inject_message`, `promote_aside` | `agent.response` (`injected: true`), `checkpoint.saved` |
| `adaptive_pressure` | `pressure.applied`, `agent.response` (speaker `"Narrator"`, `source: "pressure"`), `checkpoint.saved` |
| `replace_assumption` | `assumption.made` with `replaces` |
| `withdraw_assumption` | `assumption.withdrawn` |

### `pressure.applied`

**When:** A branch with an `adaptive_pressure` mutation, which requires `ADAPTIVE_PRESSURE_ENABLED`
(default false). Written before the narrator's injected message. A pressure text that fails the agency
guard after its retry rejects the branch, and nothing is written.

| Field | Type | Notes |
|---|---|---|
| `signals` | object | What was observed at the fork: `repetition` (number, 3 decimals), `stale_threads` (array of `{id, description, age}`), `open_threads` (integer), `budget_remaining` (integer), `as_of_turn` (integer). |
| `focus` | string or null | The operator's direction, if given. |
| `attempts` | integer | Generation attempts used. |
| `tokens_in` | integer | |
| `tokens_out` | integer | |
| `cost_usd` | number | |

```json
{"run_id": "<run-id>", "turn": 7, "seq": 48, "event_type": "pressure.applied", "agent_name": "Narrator",
 "payload": {"signals": {"repetition": 0.41, "stale_threads": [], "open_threads": 1, "budget_remaining": 13, "as_of_turn": 6},
             "focus": "the council meets tomorrow", "attempts": 1, "tokens_in": 1500, "tokens_out": 60, "cost_usd": 0.0052}}
```
