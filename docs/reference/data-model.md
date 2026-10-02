# Data model reference

What Matrix Studio stores for a run, an ensemble, a knowledge base, a document, a summary and an aside thread, and how the stored fields map to the fields the API returns, including the derived ones (`stance`, `stance_basis`, `hidden`, the cost itemisation, `research_for`). Derived from `matrix_studio/storage/dynamo.py` (the `_*_FIELDS` tuples and the write methods), `matrix_studio/api/app.py` (`_run_summary`, `_ensemble_summary`, the route bodies), `matrix_studio/stance.py`, `matrix_studio/research_state.py`, `matrix_studio/analysis.py`, `matrix_studio/ensemble_reporting.py` and `matrix_studio/state.py`.

See also: [explanation](../explanation/), [how-to guides](../how-to/), [http-api.md](http-api.md) (which route returns what), [events.md](events.md) (the event log), [run-config.md](run-config.md) (the stored `config`), and [PHASE2-STORAGE-KEY-DESIGN.md](../PHASE2-STORAGE-KEY-DESIGN.md) for the DynamoDB key design. This page names storage attributes only where the API exposes them or a reader needs them to interpret a response.

## Where things live

| Entity | Metadata | Body | Partition |
|---|---|---|---|
| Run | `runs` table, one item per run | none | The owner (`USER#<sub>`) |
| Run name marker | `runs` table | none | The owner; makes names unique per owner |
| Monthly spend | `runs` table, one item per owner per UTC month | none | The owner |
| Cast template | `runs` table | none | The owner |
| Ensemble | `runs` table | none | The owner |
| Event | `events` table, one item per event | none | The owner |
| Snapshot (checkpoint) | `snapshots` table, a pointer per turn | S3 object | The owner |
| Summary | `summaries` table, one item per version | none | The run |
| Aside thread | `threads` table | none | The run |
| Aside message | `thread-messages` table | none | The thread |
| Run document | `documents` table | S3 object (text) | The run |
| Knowledge base | `knowledge-bases` table | S3 Vectors index per KB | The KB |
| KB document | `documents` table | S3 object (text), vectors in the KB's index | The KB |
| KB grant | `kb-grants` table | none | The KB |
| Avatar image | none | S3 object (deployed) or local blob directory | The owner |

Table names carry the deployment prefix (`TABLE_PREFIX`, for example `matrix-studio-runs`). S3 object keys for owned data start with the owner's `sub`, which is how a per-request session policy confines reads. The tables partitioned by run or KB are reached only after the route has authorised the run or KB.

Values absent on a stored item read back as `null`: every field listed below is always present in a response, `null` when unset.

---

## Run

### Stored fields

| Field | Type | Written | Notes |
|---|---|---|---|
| `id` | string (UUID) | creation | Returned as `run_id`. |
| `owner_sub` | string | creation | Never returned by the run routes. A branch inherits its parent's. |
| `topic` | string | creation | |
| `cast_json` | JSON string | creation | Returned decoded as `cast` on the detail route, and as `cast_names` everywhere. |
| `config_json` | JSON string or null | creation | Returned decoded as `config`. Includes server-written keys; see [run-config.md](run-config.md#what-the-server-writes-into-the-stored-config). |
| `status` | string | creation, then the engine | See [Run status](#run-status). Starts at `pending`. |
| `created_at`, `completed_at` | integer | creation; terminal status | `completed_at` is set with a terminal status, and stays when a run is resumed. |
| `name`, `description`, `slug` | string | creation | `name` is lowercased and unique per owner (a name marker item enforces it). `slug` is the supplied name, or a slugified form of a generated one; in practice it equals `name`. |
| `parent_run_id`, `branch_turn` | string / integer or null | branch creation | Lineage. Not set on ensemble members. |
| `ensemble_id`, `ensemble_cell` | string or null | ensemble fan-out | Membership; not lineage. |
| `groups_json` | JSON string or null | creation | The creator's verified groups, so the turn loop can resolve group-granted KBs. Not returned. Fixed for the run's lifetime. |
| `stop_requested` | boolean or absent | `POST .../stop`; cleared by resume | Not returned. |
| `budget` | integer or absent | branch, resume, injection | The effective turn budget when it differs from `config.max_messages`. Not returned. |
| `research_json` | JSON string or absent | the research pass | Returned decoded as `research`. See [Research record](#research-record). |
| `stance_json` | JSON string or null | each summary generation | Returned decoded as `stance`. |
| `stance_basis_json` | JSON string or null | each summary generation | Returned decoded as `stance_basis` (detail route only). |
| `hidden` | `true` or absent | `POST .../hidden` | Returned as `hidden`, always a boolean. Showing a run removes the attribute. |

### Derived fields

Computed per request; none of these is stored on the run.

| API field | Routes | Derivation |
|---|---|---|
| `turn_count` | list, detail, tree, ensemble members | Number of `agent.response` events. Counts injected messages, closing statements and every response in a simultaneous or rotation round, so it can exceed the number of turns. |
| `total_cost_usd` | list, detail, tree, ensemble members | Sum of `payload.cost_usd` over every event in the log. |
| `last_event_at` | list, detail, ensemble members | Write time of the newest event. A `running` run whose `last_event_at` is old has stalled. |
| `max_messages` | list, detail | `config.max_messages` when it is an integer, else `null`. The effective budget of a branch or resumed run can be higher (`budget`). |
| `cast_names` | list, detail | Names from `cast_json`. |
| `stance` | list, detail | `stance_json` decoded, or `null`. |
| `hidden` | list, detail | `bool(hidden)`. |
| `cost` | detail | See [Cost itemisation](#cost-itemisation). |
| `models` | detail | Each role resolved from the stored `config` (`config.models[role]`, then `config.model`, then the role default, then `LITELLM_MODEL`). |
| `result` | detail | From the latest snapshot: `conversation`, `agents`, `total_turns`, and `total_cost_usd` (the agents' own accumulated voice cost). |
| `lineage` | detail | The parent (`{run_id, name, branch_turn}`) and direct branches, from `parent_run_id` across the owner's runs. |

### Run status

| Status | Terminal | Set when | Resumable |
|---|---|---|---|
| `pending` | no | Created on the deployed path; the first turn state flips it to `running`. | no |
| `running` | no | Generating (locally from creation). | no |
| `complete` | yes | The budget was reached, or the moderator ended a converged run (`sim.completed` carries `converged: true`). | no (branch instead) |
| `stopped` | yes | A stop request took effect, including one that arrived on the last turn. | yes |
| `capped` | yes | The per-run cost cap (`MAX_RUN_COST_USD`) or the owner's monthly cap was reached mid-run. `sim.capped.payload.scope` is `user-monthly` for the monthly cap. | no |
| `failed` | yes | The engine raised, or (deployed) a state exhausted its retries. | yes |
| `interrupted` | yes | The startup sweep found the run `running` in a process that had restarted (`STARTUP_SWEEP`, local only). | yes |

Only `complete` runs get an automatic summary, and only `complete` runs accept `POST .../summary`.

### Cost itemisation

`GET /api/runs/{ref}` returns `cost`:

| Field | Source |
|---|---|
| `in_run` | Sum of `cost_usd` over the event log (the same number as `total_cost_usd`). |
| `by_kind` | `in_run` split by `event_type`, for event types with a non-zero cost. |
| `summary` | `cost_usd` of the latest generated summary. Earlier versions are not added. |
| `research` | `research.cost_usd` from the research record. |
| `stance` | `stance_basis.classifier.cost_usd`, the closing-statement classifier that ran with the latest summary. |
| `total` | `in_run + summary + research + stance`. |

All values are rounded to 6 decimal places. Not included anywhere in `cost`: aside replies (counted per thread as `total_cost_usd`), regenerated avatars, embeddings made through `POST .../documents/embed` and `POST /api/knowledge-bases/{kb_id}/documents`, earlier summary versions, and an ensemble's report (on the ensemble as `report_cost_usd`). An ensemble's single research pass is recorded on the ensemble, not on its members.

The same response carries two other totals. `total_cost_usd` equals `cost.in_run`. `result.total_cost_usd` is the sum of each agent's `total_cost_usd` in the latest snapshot, which covers the personas' own calls and leaves out speaker selection, validation, consultants and avatars.

### What counts towards the monthly spend

The owner's monthly total (one item per UTC month, `SPEND#YYYY-MM`) is increased by:

| Spend | When |
|---|---|
| Conversation | After each turn on the deployed path: the change in the run's event-log total. The local path does not record conversation spend. |
| Summary and stance classifier | Every summary generation, automatic or requested. |
| Research pass | When the pass finishes. |
| Ensemble report | When the report is stored. |

Not recorded: aside replies, persona drafting, name suggestion, avatar regeneration, and embeddings made through the document routes. The cap is checked before a run starts (`402`) and after every deployed turn (the run ends `capped`). The total can over-count by one turn when a turn state is retried.

### Stance and stance basis

`stance` maps each persona name to one of:

| Value | Meaning |
|---|---|
| `support` | Accepts the outcome the room reached. |
| `conditional` | Accepts it with conditions, or signs while keeping an objection standing. |
| `holding` | Does not accept it. |
| `unstated` | Nothing recorded says which way they went. |

`stance` and `stance_basis` are written together each time a summary is generated, replacing the previous pair. Both are `null` until then, while a run is live, and when nothing decided any persona (no closing statements and no usable dissenter list).

`stance_basis`:

| Field | Type | Notes |
|---|---|---|
| `personas` | object | Persona name to a basis entry (below). |
| `classifier` | object or null | The closing-statement classifier call: `{model, tokens_in, tokens_out, cost_usd, error}`. `null` when the run had no closing statements. |

Basis entry:

| Field | Type | Notes |
|---|---|---|
| `stance` | string | The persona's stance. |
| `source` | string | `closing` (their closing statement decided it) or `summary`. |
| `class` | string or null | The classifier's class: `accepts`, `accepts_with_conditions`, `rejects` or `unclear`. With `source: summary` it is what the classifier said, if anything. |
| `quote` | string or null | `closing`: the deciding sentence, verbatim from the statement. `summary`: the summary's account of the objection (`holding`), the persona's flagged shift sentence (`support`), or `null`. |
| `fallback` | string | Only with `source: summary`. Why the closing statement did not decide: `no_closing_round`, `no_statement`, `classifier_failed`, `no_verdict`, `unclear`, `unverified_quote`. |
| `claimed` | string | Only when the classifier's quote was not found in the statement: the class it claimed. |

Rules: a closing-round verdict counts only when its quote (at least 12 characters) appears in that persona's closing statement. Otherwise the summary decides: a named dissenter is `holding`; a flagged `position.shift` that is not a dissenter is `support`; anyone else is `unstated`.

### Research record

`research` on a run (and on an ensemble) is the account of the pre-conversation research pass, in counts. The documents themselves are in the knowledge bases it wrote into.

| Field | Type | Notes |
|---|---|---|
| `status` | string | `researched`, `found-nothing` (a successful search that found no source), `unavailable` (no search provider configured), `failed`, `skipped`. |
| `finished_at` | integer | |
| `cost_usd` | number | Model, search-tiering and embedding cost of the pass. |
| `batch` | string | The pass's batch id; a later pass over the same scope replaces this batch's documents. |
| `provider` | string | `brave`, `tavily` or `exa`. |
| `scopes` | array | One per corpus, below. |
| `error` | string | Present on `unavailable`, `failed` and `skipped`. |

Scope entry: `scope` (`"shared"` or a persona or consultant name), `consultant` (`true` for a consultant's corpus), `queries`, `documents`, `controlling`, `unreadable`, `query_negatives` (integers), `negative` (boolean, a documented negative was written), `kb_id`, `written`, `replaced`, `embedded` (integers), `embed_cost_usd`, and `refused` or `embed_error` (strings) when that scope could not be written or embedded.

---

## Event

One item per event: `run_id`, `turn`, `seq`, `event_type`, `agent_name`, `payload` (JSON) and `created_at`. The API returns all but `created_at`. `seq` is unique per run and writes are conditional on it being free. Resume and branch delete events (the tail after the last checkpoint, and inherited terminal events). Every event type and payload: [events.md](events.md).

---

## Snapshot

A checkpoint written after each turn: a pointer item (`run_id`, `turn`, `status`, `created_at`) and the full `SimSnapshot` as an S3 object. `GET .../snapshots` returns the pointers; `GET .../snapshots/{turn}` and the run detail's `result` read the body.

| Body field | Type | Notes |
|---|---|---|
| `run_id`, `turn`, `topic`, `status` | | `status` is the run's status at that checkpoint. |
| `agents` | object | Persona name to agent state (below). |
| `conversation` | array | The transcript so far: `{speaker, content, turn}`, with extra keys on scheduled injections (`injection`) and consultant answers (`consultant`, `expert`, `asked_by`, `question`). |
| `pending_threads` | array | The pending-thread ledger. |
| `firsthand_citations` | array | `[speaker, document title]` pairs a participant read first-hand. |
| `decline_streak` | integer | Consecutive moderator declines (convergence guard). |
| `created_at`, `completed_at` | integer | |
| `total_turns` | integer | |
| `error_message` | string or null | |

Agent state: `name`, `persona`, `structured` (convictions, or `null`), `memory_stream` (`{id, timestamp, content, importance, tags, metadata}`), `goals`, `relationships` (name to stance text), `conversation_history` (the agent's view of recent messages, at most 50), `total_tokens_in`, `total_tokens_out`, `total_cost_usd`, `portrait_key` (avatar key), `portrait` (legacy inline image, old runs only), and the discriminators `type`, `schema_version`. The snapshot keeps the private conviction fields; the dossier and exports remove `validity` and `underlying_concern`.

---

## Summary

One item per version; a regeneration adds a version and the API returns the latest of each kind.

| Field | Type | Notes |
|---|---|---|
| `id` | integer | Increments per run across both kinds. |
| `run_id` | string | |
| `kind` | string | `generated` (by this app) or `imported` (by the import script). A generated summary never replaces an imported one. |
| `payload` | object | Below. |
| `tokens_in`, `tokens_out` | integer | |
| `cost_usd` | number | Includes the retry when the first reply was not valid JSON. |
| `instructions` | string or null | The custom analyst framing used, or `null` for the default. |
| `created_at` | integer | |

`payload` has the requested fields, each always present (empty when the reply did not supply it):

| Field | Type | Notes |
|---|---|---|
| `overview` | string | |
| `consensus` | array of strings | |
| `dissenters` | array of `{speaker, position}` | `speaker` is the analyst's text; stance matching tolerates partial names. |
| `key_ideas` | array of strings | |
| `open_questions` | array of strings | |
| `evidence_plan` | array of objects | Each `{data, asked_by, decision, moves_them, best_guess, cheapest_way}`; a value the conversation did not supply is `"not stated"`. Rows without `data` are dropped. |
| `conditional_recommendation` | string | `""` when nobody asked for evidence. |
| `omitted` | array of strings | Present only when the reply left requested fields out (or gave a blank overview). |

When generation fails, the summary is still stored, with an `overview` stating that generation was unavailable and the other fields empty.

---

## Aside thread and message

| Thread field | Type | Notes |
|---|---|---|
| `id` (also `thread_id`) | string (UUID) | |
| `run_id` | string | |
| `target` | string | `analyst`, `persona`, `room` or `consultant`. |
| `persona_name` | string or null | The persona or consultant; `null` for `analyst` and `room`. |
| `mode` | string | `aside`. |
| `created_at` | integer | |
| `message_count`, `total_cost_usd` | integer / number | Derived, on the list route. |

| Message field | Type | Notes |
|---|---|---|
| `id` | integer | Increments per thread; orders the messages. |
| `thread_id`, `content`, `created_at` | | |
| `role` | string | `user`, `target` (a reply) or `error` (a reply that could not be generated). |
| `speaker` | string | `user`, the replying persona or consultant, `system` for errors. |
| `tokens_in`, `tokens_out`, `cost_usd` | | Zero on user and error messages. |

The thread routes currently also return the storage keys `pk` and `sk`.

---

## Document

Run documents and KB documents share one shape; `run_id` or `kb_id` says which, never both.

| Field | Type | Notes |
|---|---|---|
| `id` | string | 12 hex characters. Returned as `id` on run-document lists, `document_id` elsewhere. |
| `run_id` | string or null | Set on a run document. |
| `kb_id` | string or null | Set on a KB document. |
| `persona_name` | string or null | Run documents only: the persona it belongs to; `null` is cast-wide. |
| `title` | string | |
| `source_path` | string or null | A file name for uploads, the page URL for researched documents. |
| `media_type` | string or null | `txt`, `md`, `pdf`, `docx`. |
| `char_count`, `chunk_count` | integer | |
| `text_is_original` | boolean | The stored text is the original extraction, so re-chunking reproduces the embedded chunks exactly. |
| `origin` | string or null | `uploaded` or `researched`; `null` means uploaded. |
| `authority` | string or null | Researched documents: `controlling`, `persuasive`, `commentary` or `unknown`, as judged by a model. |
| `research_batch` | string or null | The research pass that wrote it. |
| `created_at` | integer | |
| `s3_key`, `owner_sub` | string | Storage fields. Removed from run-document responses; present on documents in `GET /api/knowledge-bases/{kb_id}`. |

The text is chunked for retrieval; a chunk is addressed by `ordinal` (0-based) within its document, and `chunk_id` is a stable integer derived from document id and ordinal.

---

## Knowledge base

| Field | Type | Notes |
|---|---|---|
| `id` | string | 12 hex characters. |
| `name`, `description` | string | Names are not unique. |
| `owner_sub` | string | The creator. Write access is ownership alone. Returned on KB responses, including to grantees. |
| `embedding_model` | string or null | Set on the first embedding; `null` until then. Every vector in the KB's index uses it. |
| `created_at` | integer | |
| `research_for` | string or null | Set only on a collection a research pass created: `run:<run-id>` or `ensemble:<ensemble-id>`. `null` on every collection a person made. Written once at creation. |

Derived on the KB routes: `shared` (`true` when the caller is not the owner) and `document_count` (counted per request). The stored `document_count` attribute is not maintained and is overwritten in responses.

A research pass writes only into collections whose `research_for` names that pass, so starting a run from another run's setup cannot overwrite the earlier run's research. Research collections are named `Research — <label> · <scope>`.

### Grant

| Field | Type | Notes |
|---|---|---|
| `kb_id` | string | |
| `principal` | string | A user `sub` or a group name. |
| `kind` | string | `user` or `group`. |
| `granted_by` | string | The owner's `sub`. |
| `created_at` | integer | |

A grant gives read access: listing, detail, and retrieval in runs that bind the KB. Retrieval re-checks the grant on every query, so a revocation applies to runs already bound to the KB. Group membership comes from the token at request time, and from the run row (fixed at creation) inside the turn loop. A grantee can read retrieved passages and their neighbours but not a document's full stored text.

### Binding

A binding is a KB id in `config.knowledge_bases` (every persona), `cast[].knowledge_bases` (one persona) or `config.experts[].knowledge_bases` (one consultant). A speaker searches the union of the run-level and its own bindings, intersected with what the owner may read at query time. A binding is not a permission.

---

## Ensemble

### Stored fields

| Field | Type | Notes |
|---|---|---|
| `id` | string (UUID) | Returned as `ensemble_id`. |
| `owner_sub`, `topic`, `name`, `description`, `slug` | string | The name is not in the run-name space. |
| `status` | string | `pending`, `researching`, `running`, `failed`, `complete`. `complete` is written when a report is stored. |
| `created_at`, `completed_at` | integer | |
| `spec_json` | JSON | Returned as `spec`: the cells, `{label, n, overrides}`. |
| `base_config_json` | JSON | Returned as `base_config`. |
| `members_json` | JSON | `[{run_id, cell, index}]`, written before any member exists. Returned through `members`. |
| `cast_json`, `groups_json` | JSON | The base cast and the creator's groups, for the worker that fans out after research. Not returned. |
| `research_json` | JSON | Returned as `research`: one record for the whole ensemble. |
| `report_json` | JSON | Returned as `report`. At most 300 KB. |
| `report_generated_at`, `report_cost_usd` | integer / number | |
| `report_claimed_at` | integer | When a builder last took the 20-minute lease. Not cleared when a build finishes. |
| `report_error` | string | Why the last build produced no report; cleared by a successful one. |

Derived on the detail route: `members[].run` (a run summary, or `null` for a member that was never created), `cells` (`{cell, declared, complete, settled}`), `report_ready` and `has_report`.

### Report

| Field | Type | Notes |
|---|---|---|
| `generated_at`, `ensemble_id` | | |
| `cells` | array | Per declared cell: `{cell, runs: [{run_id, name, metrics, usable}], usable, declared}`. Every declared cell appears, even one whose runs all failed. |
| `missing_members` | array | Members never created: `{run_id, cell, index}`. |
| `claims` | array | Claims per cell with their tier: `unanimous` (every usable run), `split` (more than one, not all), `rare` (exactly one), `absent` (none). Never pooled across cells. |
| `conclusions` | array | What the runs concluded, counted the same way. |
| `clustered` | boolean | `true` when claims were grouped by the clustering model; `false` means crude text matching, whose counts under-report agreement. |
| `per_persona`, `agreements` | object | Per-persona demands and agreements, on the same canonical labels as `claims`. |
| `synthesis` | string | Model-written synthesis of the usable runs. |
| `cost_usd` | number | Extraction, clustering and synthesis. |
| `caveats` | array of strings | How far the counts can be trusted. |

A report needs every member settled and at least 2 usable extractions; otherwise the build records `report_error`.

---

## Cast template

| Field | Type | Notes |
|---|---|---|
| `name` | string | Unique per owner; the template's key. |
| `description` | string or null | |
| `cast` | array | Persona entries without `documents` or `document_texts`. Knowledge-base bindings are kept. |
| `created_at`, `updated_at` | integer | `created_at` survives an overwrite. |
