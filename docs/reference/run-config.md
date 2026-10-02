# Run configuration reference

The body of `POST /api/runs` (and of `POST /api/runs/forecast`, `POST /api/ensembles` and
`POST /api/ensembles/forecast`, which take the same fields plus `cells`): the topic, the cast, the
`config` block and its nested blocks, and the summary options. Sources: the request models in
`matrix_studio/api/app.py`, the engine models in `matrix_studio/state.py` and
`matrix_studio/personas.py`, the parsers in `matrix_studio/assumptions.py`,
`matrix_studio/experts.py`, `matrix_studio/injections.py`, `matrix_studio/research_state.py`,
`matrix_studio/models.py`, `matrix_studio/service.py` and `matrix_studio/ensemble_spec.py`, and what
the new-run form builds in `frontend/src/views/NewRunForm.tsx`.

See also: [`../explanation/`](../explanation/) for why the defaults are what they are,
[`../how-to/`](../how-to/) for task recipes, and the sibling references [`http-api.md`](http-api.md)
(routes, status codes, branch mutations), [`events.md`](events.md) (what each setting causes the
engine to emit) and [`settings.md`](settings.md) (deployment-wide defaults such as `MAX_MESSAGES`).

**Column key.** *Server default* is what the run gets when the field is omitted. *Form sends* is what
the new-run form sends with nothing changed: `same` when it matches the server default, `not sent`
when the form omits the field. Examples for a single block are fragments of the request body: the
block's key and its value.

---

## Contents

1. [Validation: where each check happens](#validation-where-each-check-happens)
2. [Request body](#request-body)
3. [`cast[]`](#cast)
4. [`cast[].structured`](#caststructured)
5. [`config`](#config)
6. [`config.cognition`](#configcognition)
7. [`config.selection`](#configselection)
8. [`config.retrieval`](#configretrieval)
9. [`config.personas`](#configpersonas)
10. [`config.research`](#configresearch)
11. [`config.experts[]` and `config.consult_limit`](#configexperts-and-configconsult_limit)
12. [`config.assumptions[]`](#configassumptions)
13. [`config.dynamic_assumptions`](#configdynamic_assumptions)
14. [`config.injections[]`](#configinjections)
15. [`config.models` and `config.model`](#configmodels-and-configmodel)
16. [`summary`](#summary)
17. [Ensemble `cells[]`](#ensemble-cells)
18. [What the server writes into the stored config](#what-the-server-writes-into-the-stored-config)
19. [Engine-side models (`state.py`)](#engine-side-models-statepy)

---

## Validation: where each check happens

A request is checked in three places, and a value can pass one and fail a later one.

| Stage | When | Failure |
|---|---|---|
| Request model (Pydantic) | Before the route runs | `422`, `detail` is a list of `{type, loc, msg, input}` |
| `_preflight` in the route (`POST /api/runs` and `POST /api/ensembles`; the forecast routes skip it) | Before anything is stored | `422` (empty cast, unreadable knowledge base) or `402` (monthly cap) |
| Engine parse (`*.from_config`) | When the run starts | The run fails; see below |

### Unknown keys

Only five models refuse unknown keys (`extra="forbid"`). Everywhere else an unknown key is silently
dropped by Pydantic before the server sees it. Verified by validating bodies with a bogus key against
each model (`CreateRunModel.model_validate(...)`) and by `POST /api/runs` through FastAPI's
`TestClient`.

| Where | Model | Unknown key |
|---|---|---|
| Request body top level | `CreateRunModel` | dropped |
| `config` | `RunConfigModel` | **422** `extra_forbidden`, `loc` names the key |
| `config.cognition` | `CognitionConfigModel` | dropped |
| `config.selection` | `SelectionConfigModel` | dropped |
| `config.retrieval` | `RetrievalConfigModel` | dropped |
| `config.research` | `ResearchConfigModel` | dropped |
| `config.personas` | `PersonaConfigModel` | dropped |
| `config.experts[]` | `ExpertModel` | **422** |
| `config.assumptions[]` | `AssumptionModel` | **422** |
| `config.dynamic_assumptions` | `DynamicAssumptionsModel` | **422** |
| `config.injections[]` | `InjectionModel` | **422** |
| `cast[]` | `PersonaModel` | dropped |
| `cast[].structured` and everything under it | `StructuredPersona` and nested | dropped |
| `cast[].document_texts[]`, `config.experts[].document_texts[]` | `InlineDocumentModel` | dropped |
| `summary` | `SummaryConfigModel` | dropped |
| Ensemble body top level | `CreateEnsembleModel` | dropped |
| `cells[]` | `EnsembleCellModel` | dropped |

`POST /api/runs` with `"config": {"max_turns": 10}` answers `422`:

```json
{"detail": [{"type": "extra_forbidden", "loc": ["body", "config", "max_turns"],
             "msg": "Extra inputs are not permitted", "input": 10}]}
```

### Values the request model accepts and the engine refuses

These fields are typed loosely in the request model and validated only by the engine models in
`state.py`. The API answers `201`; the run then fails when the block is parsed.

| Field | Accepted by the API | Engine rule |
|---|---|---|
| `selection.method` | any string | `moderated` \| `rotation` \| `simultaneous` \| `hybrid` |
| `selection.hybrid_opening_rounds` | any integer | `>= 1` |
| `retrieval.mode` | any string | `fts` \| `vector` \| `hybrid` |
| `personas.dismissal_rule` | any JSON value | `mandatory` \| `retuned` \| `blunt` \| `off`, or a boolean |

What "fails" means depends on the deployment:

- **Step Functions (deployed).** The run row is written with status `pending`. `retrieval` and
  `personas` are parsed in the Prepare state and `selection` in the Turn state; the state raises, is
  retried, and the catch marks the run `failed`.
- **Local (no `TURN_LOOP_ARN`).** `run_simulation` parses all four blocks before it writes the run
  row, so the background task fails with no row written. `POST /api/runs` has already answered `201`
  with status `running`, and `GET /api/runs/{run_id}` then answers `404`.

Other values that are not validated at the API: `max_messages` (any integer, see
[`config`](#config)), `research.provider` (an unknown name is recorded on the research record at
research time), `summary.fields` (unknown names are filtered out), `model` and `config.models` values
(not checked against `AVAILABLE_MODELS`), and `config.models` role names (unknown roles are ignored
with a log warning).

### Cross-field checks

| Check | Model | Failure (`422`) |
|---|---|---|
| Top-level `model` and `config.model` both set and different | `CreateRunModel` | `model is set twice and differently: top-level '<a>' and config.model '<b>'. Set one.` |
| `config.experts` non-empty and `config.retrieval.enabled` not true | `CreateRunModel`, `CreateEnsembleModel` | `consultants need retrieval on: set config.retrieval.enabled` |
| A consultant name equals a persona name or another consultant's (trimmed, case-insensitive) | same | `consultant name '<name>' is already used by a persona or another consultant` |
| An injection's `after_turn >= config.max_messages` (only when `max_messages` is set) | `RunConfigModel` | `an injection after turn <n> would never be delivered: ...` |
| An ensemble cell's `injections` override speaks as a cast member | `CreateEnsembleModel` | `cell '<label>' injects as '<name>', a cast member. ...` |
| `cast` empty | route `_preflight` | `At least one persona is required` |
| A bound knowledge base (run level, persona level or consultant level) does not exist or is not readable by the caller | route `_preflight` | `These knowledge bases do not exist or are not shared with you: <ids>` |
| The caller is over their monthly cap, or their spend could not be read | route `_preflight` | `402` with the spent and cap amounts |

---

## Request body

`CreateRunModel`. `CreateEnsembleModel` has the same fields plus `cells` (see
[Ensemble `cells[]`](#ensemble-cells)).

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `topic` | string | required | the topic, trimmed | none (an empty string is accepted) | The question the cast discusses; shown to every persona. |
| `cast` | list of [`PersonaModel`](#cast) | required | only rows with both a name and a persona | at least one entry (`422` from `_preflight`) | The personas who speak. |
| `config` | [`RunConfigModel`](#config) | `{}` | see [`config`](#config) | unknown keys refused | Everything about how the run behaves. |
| `model` | string or null | null | the `/api/models` default, or the selected model | must agree with `config.model` if both are set | The conversation model; copied into `config.model`. See [`config.models`](#configmodels-and-configmodel). |
| `name` | string or null | generated | the name, trimmed, or not sent | none | Run codename. Lowercased and trimmed; a name the caller already uses gets `-2` ... `-99` appended. Omitted: generated by the `naming` role, falling back to a word list. |
| `description` | string or null | generated, or `topic[:80]` when `name` is given | the description, trimmed, or not sent | none | One-line description. |
| `summary` | [`SummaryConfigModel`](#summary) or null | null (auto-summary on, every field) | not sent unless auto-summary is off or a focus is set | unknown keys dropped | The summary generated when the run completes. Stored as `config.summary`. |

```json
{
  "topic": "Should the town library open on Sundays?",
  "cast": [
    {"name": "Ada Byrne", "persona": "Head librarian. Careful with staff hours.", "goals": ["Protect weekday service"]},
    {"name": "Tomas Reyes", "persona": "Parent of two. Works weekdays.", "goals": ["Get Sunday hours"]}
  ],
  "config": {"max_messages": 12},
  "name": "sunday-hours"
}
```

Edge cases:

- Unknown top-level keys (for example a misplaced `cells` on `POST /api/runs`) are dropped, not refused.
- The response is the run's metadata, not the run: see [`http-api.md`](http-api.md).
- A duplicate persona name is not refused. The engine keys agents by name, so the later entry replaces the earlier one.

---

## `cast[]`

`PersonaModel`, one per persona.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `name` | string | required | trimmed | none (blank accepted) | The speaker's name in the transcript, and the key retrieval and bindings resolve by. A real, widely known person's name is replaced before the run is stored (see the edge cases below). |
| `persona` | string | required | trimmed | none | Prose description: the persona's voice and manner. |
| `goals` | list of strings | `[]` | the goals box split on newlines and `;`, blanks dropped | none | Goals shown in the persona's prompt. |
| `documents` | list of strings | `[]` | not sent | none | Server-readable file paths, ingested before turn 1 as this persona's documents. Suffix must be `.txt`, `.text`, `.md`, `.markdown`, `.pdf` or `.docx`. |
| `document_texts` | list of [`InlineDocumentModel`](#castdocument_texts) | `[]` | pasted or uploaded documents with non-blank text, or not sent | none | Inline documents, ingested before turn 1 as this persona's documents. |
| `structured` | [`StructuredPersona`](#caststructured) or null | null | built from the convictions boxes, or not sent | see below | Convictions. Reaches a prompt only when `config.personas.enabled` is true. |
| `knowledge_bases` | list of strings (knowledge-base ids) | `[]` | the persona's picked collections, or not sent | each id must be readable by the caller (`422`) | Collections this persona alone searches, in addition to `config.knowledge_bases`. Searched only when `config.retrieval.enabled` is true. |

### `cast[].document_texts[]`

`InlineDocumentModel`. Also used by `config.experts[].document_texts[]`.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `title` | string or null | `pasted-<n>.txt` | the title, or `pasted-<n>.txt` | none | Document title; appears in citations as `[title #n]`. |
| `text` | string | required | the text | none at run creation | Document body. Blank text is skipped. |

```json
{
  "name": "Ada Byrne",
  "persona": "Head librarian. Careful with staff hours.",
  "goals": ["Protect weekday service"],
  "document_texts": [{"title": "Staffing rota 2026", "text": "Weekday shifts: ..."}],
  "knowledge_bases": ["<kb-id>"]
}
```

Edge cases:

- An ingestion failure does not fail the run: it is recorded as a `document.failed` event (see [`events.md`](events.md)).
- `document_texts` is not checked against `MAX_DOCUMENT_CHARS` at run creation; that limit applies to `POST /api/documents/extract` and knowledge-base uploads.
- `documents` paths are read from the filesystem of whichever process prepares the run (the Prepare worker when deployed). A path that does not exist is a `document.failed` event naming the path.
- A persona whose name also names a consultant is refused (see [Cross-field checks](#cross-field-checks)).
- **Real names.** A persona or consultant named after a real, widely known person is renamed to a fictional sound-alike before the run is stored (`"Jeff Bezos"` becomes `"Geoff Beesoh"`), and the response lists it in `renamed` ([`http-api.md`](http-api.md#conventions)). The same replacement is made for that person's full name in every `persona`, `goals` and `structured` text in the cast, in consultants' `expertise`, in `topic`, in `config.assumptions[]` and in `config.injections[]` (a `speaker` equal to the original name follows the rename). `document_texts` and `documents` are not rewritten. A single first name or surname never matches. Checked against the curated list `matrix_studio/public_figures.json`, then, for a full name not on it, by the `name_check` model role (see [`config.models`](#configmodels-and-configmodel)).

---

## `cast[].structured`

`StructuredPersona` (`matrix_studio/personas.py`). Parsed at run start whether or not
`config.personas.enabled` is true, so a bad value fails early. An empty object, or one with nothing in
it, is treated as absent. The form builds `viewpoints` (one per line of the positions box, with an
optional `[firmness]` prefix and `-> condition; condition`), `underlying_concern` (matched to positions
by line), and `preferences.dismisses`; it sends no other fields.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `role` | string | `""` | not sent | none | "Your role: ..." in the private prompt. |
| `background` | object | empty | not sent | see below | Tenure, prior roles and formative events. |
| `background.tenure_years` | integer or null | null | not sent | none | Years in this kind of work. |
| `background.prior_roles` | list of strings | `[]` | not sent | none | Previous roles. |
| `background.formative_events[]` | list of `{year, event, lesson}` | `[]` | not sent | `event` required; `year` integer or null; `lesson` default `""` | Events and the lesson each taught. |
| `preferences.optimises_for` | list of strings | `[]` | not sent | none | What the persona trades everything for. |
| `preferences.dismisses` | list of strings | `[]` | the dismisses box, or not sent | none | Concerns the persona declines to weigh; rendered with the `dismissal_rule` wording. |
| `preferences.persuaded_by` | list of strings | `[]` | not sent | none | What moves the persona. |
| `viewpoints[]` | list of viewpoint objects | `[]` | parsed positions | see below | Positions the persona holds. |
| `viewpoints[].position` | string | required | the line's position | none | The position as stated aloud. |
| `viewpoints[].firmness` | string | `negotiable` | the `[tag]`, or `negotiable` | `negotiable` \| `firm` \| `non-negotiable` \| `requires-escalation`; anything else is `422` | How firmly it is held. `firm` and above require named evidence before moving. |
| `viewpoints[].evidence_that_shifts` | list of strings | `[]` | the text after `->`, split on `;` | none | What would change the persona's mind. Also the source of research queries and of `retrieval.standing_query`. |
| `viewpoints[].underlying_concern` | string | `""` | the matching concerns line | none | The real worry. Withheld unless asked when `personas.withhold_concerns` is true. Never shown to the moderator, the event log or the dossier. |
| `viewpoints[].formed_by` | string | `""` | not sent | none | The experience that produced the position. |
| `viewpoints[].validity` | string or null | null | not sent | none | Operator calibration note. Never rendered into any prompt. |
| `type`, `schema_version` | string | `StructuredPersona`, `1.0.0` | not sent | none | Discriminator fields; no behavioural effect. |

```json
"structured": {
  "role": "Head librarian",
  "preferences": {"dismisses": ["parking"]},
  "viewpoints": [{
    "position": "No Sunday opening without new staff",
    "firmness": "firm",
    "evidence_that_shifts": ["a funded post in the council budget"],
    "underlying_concern": "I will be the one covering the shifts"
  }]
}
```

Edge cases:

- `firmness` is the one value under `structured` validated at the API: an unknown value is a `422` with `loc` ending in `firmness`.
- A persona with no `viewpoints` gets no per-persona research collection (see [`config.research`](#configresearch)).

---

## `config`

`RunConfigModel`. Unknown keys are refused with a `422` naming the key. The nested blocks have their
own sections below; this table covers the top-level fields.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `max_messages` | integer or null | `MAX_MESSAGES` setting (20) | `10`; `100` when "End when the conversation is finished" is ticked; `12` after "load example" | none at the API (form input 1-100, or 1-300 with the toggle) | Turn budget. A turn is one speaker under `moderated` and one round of everyone under `rotation` and `simultaneous`. |
| `model` | string or null | null | not sent (the form sends top-level `model`) | must match top-level `model` if both set | Conversation model for every role `models` does not name. |
| `generate_avatars` | boolean or null | `ENABLE_AVATARS` setting (true) | `true`; `false` after switching the run type to an ensemble | none | Generate a portrait per persona before turn 1. |
| `cognition` | [object](#configcognition) or null | null (off) | `{enabled: true, ...}` | unknown keys dropped | Memory, reflection, dynamic goals, relationships, pending threads. |
| `retrieval` | [object](#configretrieval) or null | null (off) | sent only when documents, collections, research or consultants exist | unknown keys dropped | Document retrieval per turn. |
| `selection` | [object](#configselection) or null | null (defaults, fairness on) | sent only when a non-default option is chosen | unknown keys dropped | Next-speaker method and stopping rules. |
| `personas` | [object](#configpersonas) or null | null (off) | sent only when some persona has convictions | unknown keys dropped | Whether `structured` reaches prompts. |
| `research` | [object](#configresearch) or null | null (off) | sent only when research is ticked | unknown keys dropped | Pre-conversation web research. |
| `knowledge_bases` | list of strings | `[]` | the run-level picked collections, or not sent | each id readable by the caller (`422`) | Collections every persona searches. |
| `models` | object of string to string, or null | null | not sent | see [`config.models`](#configmodels-and-configmodel) | Per-role model overrides. |
| `experts` | list of [`ExpertModel`](#configexperts-and-configconsult_limit) | `[]` | the consultants, or not sent | at most 5; needs `retrieval.enabled` | Consultants the personas may ask. |
| `consult_limit` | integer or null | 6 (engine) | `6`, only with consultants | 0-20 | Consultations allowed per run. |
| `assumptions` | list of [`AssumptionModel`](#configassumptions) | `[]` | non-blank assumptions, or not sent | at most 8 | Working assumptions every persona reasons from. |
| `dynamic_assumptions` | [object](#configdynamic_assumptions) or null | null (off) | `{enabled: true}` when ticked, else not sent | unknown keys refused | The moderator may add assumptions during the run. |
| `injections` | list of [`InjectionModel`](#configinjections) | `[]` | complete scheduled messages, or not sent | at most 5; each `after_turn < max_messages` | Operator messages entering after a given turn. |

```json
"config": {
  "max_messages": 12,
  "generate_avatars": false,
  "knowledge_bases": ["<kb-id>"],
  "retrieval": {"enabled": true}
}
```

Edge cases:

- `max_messages` of 0 or below is accepted. Under Step Functions the budget then falls back to the `MAX_MESSAGES` setting (`orchestration.budget_of` reads only a positive value); the local path uses the value as given and generates no turns.
- With `max_messages` omitted, the injection check is skipped, so an injection after the default budget is accepted and never delivered.
- A stored run config (from `GET /api/runs/{ref}`) is not always a valid request `config`: it can carry `summary`, `branch_mutation` and `research.targets`, which this model refuses or drops. `GET /api/runs/{ref}/setup` returns a re-submittable body whose `config` carries only `max_messages`, `generate_avatars`, `cognition`, `retrieval`, `personas`, `knowledge_bases`, `selection`, `research` (without `targets`) and `models`; it does not carry `experts`, `consult_limit`, `assumptions`, `dynamic_assumptions`, `injections` or `summary`.
- `knowledge_bases` bindings are checked for readability at creation and re-checked on every turn; a binding whose grant is revoked mid-run returns nothing from then on.
- `knowledge_bases` without `retrieval.enabled` is accepted and never searched.

---

## `config.cognition`

`CognitionConfigModel`. Omitted means cognition is off and the engine path is identical to a run
without the feature. The sub-flags take effect only when `enabled` is true.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `enabled` | boolean | `false` | `true` (the whole block is not sent when unticked) | none | Master switch: structured replies with rationale, memory references and goal served. |
| `memory` | boolean | `true` | same | none | Form and retrieve per-agent memories. |
| `reflection_every` | integer | `4` | `4`, or `0` when unticked | `>= 0` | Reflect every N turns; 0 disables. |
| `goals_dynamic` | boolean | `false` | `true` | none | Agents may update their own goals. |
| `relationships` | boolean | `false` | `true` | none | Track each agent's stance toward the others. |
| `retrieval_k` | integer | `5` | not sent | `>= 0` | Memories injected into each turn's prompt. |
| `threads` | boolean | `false` | not sent | none | Pending-thread ledger (setups and payoffs). |
| `thread_stale_after` | integer | `5` | not sent | `>= 1` | Open threads older than this many turns are flagged `stale`. |

```json
"cognition": {"enabled": true, "memory": true, "reflection_every": 4, "goals_dynamic": true, "relationships": true}
```

Edge cases:

- `selection.stop_when_converged` only works with cognition on: the moderator can decline to pick a speaker only on the cognition-on selection prompt.
- A turn whose structured reply could not be parsed still produces a message; the dossier counts these as `cognition_lost_turns`.

---

## `config.selection`

`SelectionConfigModel`. Omitted means the defaults below, including fairness **on**, which is the one
block whose omission does not mean "off".

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `fairness` | boolean | `true` | not sent | none | Shows the moderator each persona's turn count, time since last spoke, and the fair share. `moderated` and the moderated phase of `hybrid` only. |
| `method` | string | `moderated` | the chosen method when not `moderated` | engine: `moderated` \| `rotation` \| `simultaneous` \| `hybrid` (not checked by the API) | How the next speaker is decided. |
| `hybrid_opening_rounds` | integer | `2` | the opening-rounds value, only with `hybrid` | engine: `>= 1` (not checked by the API) | Simultaneous rounds before `hybrid` switches to moderated. Ignored by other methods. |
| `closing_round` | boolean | `false` | `true` when ticked | none | One extra round at turn `max_messages + 1` asking everyone for final positions and terms. Only when the ceiling is reached, not after convergence. |
| `stop_when_converged` | boolean | `false` | `true` when ticked (checkbox disabled for `rotation` and `simultaneous`) | none | The moderator may end the run when nobody has more to add. Needs every persona to have spoken and two consecutive declines. |

Methods:

| `method` | Per turn | Personas see each other that turn |
|---|---|---|
| `moderated` | one speaker, picked by the `speaker_selection` model | yes |
| `rotation` | everyone once, in cast order | yes, the earlier speakers in the round |
| `simultaneous` | everyone at once | no, each answers the state as the round opened |
| `hybrid` | `hybrid_opening_rounds` simultaneous rounds, then moderated | as above per phase |

```json
"selection": {"method": "hybrid", "hybrid_opening_rounds": 2, "closing_round": true}
```

Edge cases:

- Under `rotation` and `simultaneous`, a round in which every persona passes ends the run as converged, whether or not `stop_when_converged` is set.
- A converged run still ends with status `complete`; `sim.completed` carries `converged` and the turn (see [`events.md`](events.md)).
- Closing-round responses carry `closing: true`; an empty closing reply is a `closing.missing` event.
- An unknown `method` or `hybrid_opening_rounds: 0` is accepted by the API and fails the run (see [Validation](#values-the-request-model-accepts-and-the-engine-refuses)).

---

## `config.retrieval`

`RetrievalConfigModel`. Omitted means retrieval is off: the index is never queried and no prompt block
is added. Required for consultants, and for knowledge-base bindings and research to have any effect.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `enabled` | boolean | `false` | `true` (the block is sent only when documents, collections, research or consultants exist) | none | Master switch. |
| `k` | integer | `3` | not sent | `>= 0` | Most passages injected per turn. |
| `max_chars` | integer | `1200` | not sent | `>= 0` | Hard ceiling on retrieved characters per turn. |
| `recent_turns` | integer | `3` | not sent | `>= 1` | Recent messages that contribute to the query. |
| `mode` | string | `vector` | not sent | engine: `fts` \| `vector` \| `hybrid` (not checked by the API) | `fts` is in-process BM25; `vector` is embeddings; `hybrid` fuses both by Reciprocal Rank Fusion. |
| `embedding_model` | string | `""` | not sent | none | LiteLLM embedding model; `""` means `bedrock/amazon.titan-embed-text-v2:0`. |
| `rrf_k` | integer | `60` | not sent | `>= 1` | Reciprocal Rank Fusion constant (`hybrid` only). |
| `min_similarity` | number | `0.15` | not sent | 0.0-1.0 | Cosine floor for `vector` and `hybrid`; 0 disables. |
| `disclose_unsupported` | boolean | `false` | not sent | none | When retrieval found nothing, the persona says in-voice it is speaking from experience. |
| `cite_inline` | boolean | `true` | the checkbox value (default `true`), always sent | none | Personas cite each passage they rely on as `[title #n]`. |
| `term_limit` | integer | `0` | not sent | `>= 0` | Most discriminative query terms kept; 0 is off. Experimental. |
| `max_df_ratio` | number | `0.5` | not sent | `> 0.0`, `<= 1.0` | Drops query terms in more than this fraction of chunks. Used only when `term_limit > 0`. |
| `score_ratio` | number | `0.0` | not sent | 0.0-1.0 | Drops matches weaker than this fraction of the best score; 0 is off. Experimental. |
| `authority_floor` | integer | `0` | `1` when research is ticked, else not sent | `>= 0` | Retrieval slots reserved for controlling authorities (statute, regulation, decided case); 0 is off. |
| `standing_query` | boolean | `false` | not sent | none | Also retrieves with the speaker's `evidence_that_shifts` each turn, in the same `k`. |

```json
"retrieval": {"enabled": true, "mode": "vector", "k": 3, "max_chars": 1200, "cite_inline": true}
```

Edge cases:

- `cite_inline` defaults to `true` here and `false` in the engine. Sending any `retrieval` object stores `cite_inline: true` unless it is set; a stored config without the key (runs created before 2026-09-28) keeps `false` on resume and branch.
- A run whose chunks were never embedded, or whose embedding call fails, falls back to lexical retrieval for that turn.
- An unknown `mode` is accepted by the API and fails the run.

---

## `config.personas`

`PersonaConfigModel`. Omitted means structured personas are off: every `cast[].structured` block is
ignored for prompts (it is still parsed).

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `enabled` | boolean | `false` | `true` (the block is sent only when some persona has convictions) | none | Render `structured` into the persona's own prompt and a public summary into the moderator's cast list. |
| `withhold_concerns` | boolean | `true` | not sent | none | Keep `underlying_concern` unsaid until asked. |
| `dismissal_rule` | string or boolean | `mandatory` | not sent | engine: `mandatory` \| `retuned` \| `blunt` \| `off`; `true` means `mandatory`, `false` means `off` (not checked by the API) | Which wording is rendered with `preferences.dismisses`. `off` renders neither the rule nor the list. |
| `evidence_lean` | boolean | `true` | the checkbox value (default `true`) | none | A persona asking for evidence must also give its best guess and current lean. |

```json
"personas": {"enabled": true, "dismissal_rule": "mandatory", "evidence_lean": true}
```

Edge cases:

- `withhold_concerns`, `dismissal_rule` and `evidence_lean` take effect only with `enabled: true`.
- The stored config keeps `dismissal_rule` as sent (a boolean stays a boolean); the engine coerces it when the run is parsed.
- An unknown `dismissal_rule` is accepted by the API and fails the run.

---

## `config.research`

`ResearchConfigModel`. Omitted or `enabled: false` means no research. With it on, a pass searches the
open web before turn 1 and stores what it finds in new knowledge bases created for this run.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `enabled` | boolean | `false` | `true` (the block is sent only when ticked) | none | Run the research pass. |
| `shared` | boolean | `true` | same | none | One collection every persona searches, bound at run level. |
| `personas` | boolean | `true` | same | none | One collection per persona with at least one `structured.viewpoints` entry, bound to that persona: their case and the case against it. |
| `consultants` | boolean | `true` | same | none | One collection per consultant, bound to that consultant, searched from their `expertise`. |
| `results_per_query` | integer or null | null (5) | `5` | 1-20 | Search results taken per query. |
| `fetch_per_query` | integer or null | null (3) | not sent | 0-10 | Pages fetched per query. |
| `provider` | string or null | null | not sent | `brave` \| `tavily` \| `exa`, case-insensitive (checked at research time) | Search provider. Null uses `SEARCH_PROVIDER`, else the first configured key in the order Tavily, Exa, Brave. |

`targets` is not a request field. It is dropped by the model, and when research is on the server
writes `research.targets` itself: `{"shared": <kb-id>, "personas": {<name>: <kb-id>}, "consultants": {<name>: <kb-id>}}`.

```json
"research": {"enabled": true, "shared": true, "personas": true, "consultants": false, "results_per_query": 5}
```

Edge cases:

- Each created collection is named `Research — <run name> · <scope>`, owned by the caller, marked with `research_for`, and appended after any bindings the request already had at that scope.
- No configured provider, or a named provider whose key is missing or unknown, records the research as `unavailable`; the run continues without it. A failed pass is recorded as `failed` and the run also continues.
- `research` without `retrieval.enabled` is accepted; the pass runs and is paid for, and no turn queries it. The form always turns retrieval on with research.
- On an ensemble the pass runs once for the parent, and every member binds the same collections.
- `research: true` (a bare boolean) is refused by the API (`422`, the field is an object); the engine parser and the CLI accept it as `{"enabled": true}`.

---

## `config.experts[]` and `config.consult_limit`

`ExpertModel`, one per consultant. A consultant never takes a turn and is never in the speaker pool. A
persona asks by ending a message with `ASK <name>: <question>`; the engine removes the line, answers
from the consultant's own sources only, and appends the answer as `<name> (consultant)`.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `name` | string | required | trimmed | 1-60 characters; unique against personas and other consultants | The consultant's name. |
| `expertise` | string | `""` | trimmed | at most 300 characters | Shown in persona prompts; the research query source for this consultant. |
| `knowledge_bases` | list of strings | `[]` | picked collections, or not sent | each id readable by the caller (`422`) | Collections the consultant answers from. |
| `document_texts` | list of [`InlineDocumentModel`](#castdocument_texts) | `[]` | one document (title defaults to `<name> notes`), or not sent | none | Inline sources the consultant answers from. Titles are listed in persona prompts. |

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `consult_limit` | integer or null | `6` | `6`, only when consultants exist | 0-20 | Consultations allowed per run, counted from the conversation. |

```json
"retrieval": {"enabled": true},
"experts": [{"name": "Priya Nand", "expertise": "Municipal employment law",
             "document_texts": [{"title": "Council staffing policy", "text": "..."}]}],
"consult_limit": 4
```

Edge cases:

- More than 5 consultants is a `422` (`too_long`).
- A consultant without `retrieval.enabled` is a `422`.
- An answer the sources do not contain is the fixed sentence `That isn't in my sources.`

---

## `config.assumptions[]`

`AssumptionModel`. Each is shown to every persona as something to reason from, not as evidence, and is
recorded as an `assumption.made` event at turn 0 with id `A1`, `A2`, ... in order.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `statement` | string | required | trimmed, blank rows omitted | 1-300 characters | The assumption. |
| `basis` | string | `""` | trimmed, or not sent | at most 300 characters | Why it is assumed. |

```json
"assumptions": [{"statement": "Assume Sunday footfall is half of Saturday's", "basis": "Branch counts from a neighbouring town"}]
```

Edge cases:

- More than 8 is a `422`.
- Whitespace is collapsed; a statement that is only whitespace passes the API and is skipped by the engine, which shifts the numbering of later ones.
- A branch can replace or withdraw an assumption by id (`replace_assumption`, `withdraw_assumption`; see [`http-api.md`](http-api.md)).

---

## `config.dynamic_assumptions`

`DynamicAssumptionsModel`. Unknown keys are refused. Omitted means the moderator never adds
assumptions.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `enabled` | boolean | `false` | `true` when ticked (block not sent otherwise) | none | Let the moderator add an assumption when a gap blocks the room. |
| `every` | integer | `4` | not sent | 1-20 | Check after every N completed turns. |
| `limit` | integer | `3` | not sent | 0-8 | Most assumptions the moderator may add in one run. Operator assumptions do not count. |

```json
"dynamic_assumptions": {"enabled": true, "every": 4, "limit": 2}
```

Edge cases:

- Each check is one model call. The moderator adds at most one assumption per turn number, so a retried turn does not add a second one for the same gap.
- `dynamic_assumptions: true` (bare boolean) is refused by the API; the engine parser and the CLI accept it.

---

## `config.injections[]`

`InjectionModel`. Unknown keys are refused. Each message enters the conversation after turn
`after_turn` as an `agent.response` flagged `injected` with source `operator`, sharing that turn's
number, so it does not use up a generated turn.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `after_turn` | integer | required | the turn, floored, at least 0 | `>= 0`; `< max_messages` when `max_messages` is set | When it is delivered; 0 means before the first turn. |
| `speaker` | string | required | trimmed | 1-60 characters | Who says it. A persona's name puts the words in that persona's mouth and history; any other name is a voice in the feed. |
| `content` | string | required | trimmed | 1-2000 characters | The message. |

```json
"injections": [{"after_turn": 6, "speaker": "Council letter", "content": "The council will fund one extra post from April."}]
```

Edge cases:

- More than 5 is a `422`.
- In an ensemble comparing with and without a message, the form sends the injections only in the `with-message` cell override, not in the base `config`.
- An injection already present in the conversation is not delivered again, so a retried turn does not duplicate it.

---

## `config.models` and `config.model`

`config.models` is a plain object of role name to LiteLLM model string. `config.model` (or the
top-level `model`) is the conversation model. Resolution for each role, first match wins:

1. `config.models[role]`
2. `config.model`, which applies to every role
3. the role's deployment default, for `validation`, `speaker_selection`, `naming`, `stance` and `name_check`:
   `bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0`
4. the `LITELLM_MODEL` setting (default `bedrock/global.anthropic.claude-sonnet-5`)

| Role | Call | Temperature | Frequency | Deployment default |
|---|---|---|---|---|
| `voice` | the persona's utterance | 0.7 | once per turn | `LITELLM_MODEL` |
| `speaker_selection` | the moderator's pick | 0.3 | once per moderated turn | Haiku 4.5 |
| `validation` | the pre-emit gate | 0.0 | once per turn, plus retries | Haiku 4.5 |
| `reflection` | a cognition reflection | 0.7 | every `reflection_every` turns | `LITELLM_MODEL` |
| `summary` | the run summary | 0.3 | once per run | `LITELLM_MODEL` |
| `aside` | an aside reply | 0.3-0.6 | on request | `LITELLM_MODEL` |
| `naming` | the run codename | 0.9 | once per run | Haiku 4.5 |
| `wizard` | the persona wizard | 1.0 | once, before the run | `LITELLM_MODEL` |
| `pressure` | adaptive pressure (experimental) | 0.7 | on request | `LITELLM_MODEL` |
| `stance` | the closing-statement classifier | 0.0 | once per summary | Haiku 4.5 |
| `name_check` | is a persona's name a real public figure's (`matrix_studio/real_names.py`) | 0.0 | once per new full name, before the run, cached | Haiku 4.5 |

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `models` | object of string to string | null | not sent | none at the API | Per-role overrides. |
| `model` | string | null | not sent in `config`; the form sends top-level `model` | must match top-level `model` | Conversation model for every role not in `models`. |

```json
"model": "<model-id>",
"config": {"models": {"voice": "<model-id>", "validation": "<model-id>"}}
```

Edge cases:

- An unknown role name is ignored with a log warning; an empty model string is skipped.
- Model strings are not checked against `AVAILABLE_MODELS`.
- Setting `model` (top-level or `config.model`) moves `validation`, `speaker_selection`, `naming` and `stance` off their deployment default as well. It does not move `name_check`: that check also runs before any run exists, from the new-run form, and the two must agree, so only `config.models.name_check` changes it. The new-run form always sends top-level `model` (the `/api/models` default unless another is picked), so form-created runs use that model for every role.
- `GET /api/runs/{ref}` returns `models`, what each role resolved to for that run.

---

## `summary`

`SummaryConfigModel`, at the top level of the request (a `summary` key inside `config` is refused).
Stored as `config.summary`. Omitted means a summary of every field is generated when the run
completes.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `enabled` | boolean | `true` | the checkbox value, only when the block is sent | none | Generate the summary when the run completes. |
| `fields` | list of strings or null | every field | not sent | unknown names are dropped; none left means every field | Which summary fields to produce. |
| `focus` | string or null | null | the focus text, trimmed, or not sent | none | An emphasis passed to the analyst. |
| `instructions` | string or null | null (the default analyst framing) | not sent | none | Replaces the analyst-role framing; the JSON and no-fabrication guardrails remain. |

Summary field names, in canonical order: `overview`, `consensus`, `dissenters`, `key_ideas`,
`open_questions`, `evidence_plan`, `conditional_recommendation`.

```json
"summary": {"enabled": true, "fields": ["overview", "consensus", "dissenters"], "focus": "staffing cost"}
```

Edge cases:

- The form sends the block only when auto-summary is off or a focus is set.
- A summary can be regenerated later with different options through `POST /api/runs/{ref}/summary` (see [`http-api.md`](http-api.md)).

---

## Ensemble `cells[]`

`EnsembleCellModel`, on `POST /api/ensembles` and `POST /api/ensembles/forecast` only. Each cell is a
group of replicate runs; every member of a cell gets an identical config, and cells may differ only in
the keys they declare. Omitted means one cell, `{"label": "base", "n": 5}`, nothing varied.

| Field | Type | Server default | Form sends | Constraints | Effect |
|---|---|---|---|---|---|
| `label` | string | required | `base`, `hybrid`, `with-message` | non-empty, no leading or trailing space, unique | Names the cell in the report and in member run names (`<name>-<label><index>`). |
| `n` | integer | `5` | `5` for `base`, `2` for `hybrid`, `3` for `with-message` | `>= 2`; all cells together at most 12 | Replicates in the cell. |
| `overrides` | object of dotted config path to value | `{}` | see below | allowlisted keys only | How this cell differs from the base `config`. |

Override keys:

| Key | Status | Reason given in the `422` |
|---|---|---|
| `selection.method` | allowed | |
| `selection.hybrid_opening_rounds` | allowed | |
| `injections` | allowed (not as a cast member's words) | |
| `assumptions` | allowed | |
| `max_messages` | refused | turn count is censoring, not variation |
| `selection.fairness` | refused | already measured and settled |
| `selection.stop_when_converged` | refused | changes when a run stops, so it censors; still being validated |
| `models.voice` | refused | changes the instrument, not the question |
| anything under `personas` | refused | persona instructions are the measuring instrument |
| any other key | refused | not a cell override |

```json
"cells": [
  {"label": "base", "n": 3},
  {"label": "hybrid", "n": 3, "overrides": {"selection.method": "hybrid", "selection.hybrid_opening_rounds": 2}}
]
```

Edge cases:

- `n: 0` (or any falsy value) becomes 5: the spec parser reads `n or 5`.
- Unknown keys in a cell are dropped by the request model, so the spec parser's own "Unknown cell field(s)" refusal is never reached through the API.
- Override values are not validated by the request model: a cell's `selection.method` or `injections` value is checked only when the member runs.
- A spec error is a `422` whose `detail` is the reason string.

---

## What the server writes into the stored config

The stored `config` (returned as `config` by `GET /api/runs/{ref}`) is the request's `config` after
`model_dump(exclude_none=True)`, with these changes:

| Key | Written by | Value |
|---|---|---|
| Every field of a block that was sent | request model | The block with its defaults filled in (sending `{"retrieval": {"enabled": true}}` stores every retrieval field) |
| `knowledge_bases`, `experts`, `assumptions`, `injections` | request model | Always present, `[]` when not sent |
| `summary` | `RunManager.create_run` | The top-level `summary` block, when sent |
| `model` | `RunManager.create_run` | The top-level `model`, when `config.model` was not set |
| `research.targets`, `research.enabled` | `research_state.allocate_targets` | The collections allocated for the pass |
| `knowledge_bases` (run, persona and consultant level) | `research_state.allocate_targets` | The research collections, appended |
| `max_messages`, `model`, `branch_mutation` | branch creation | The branch budget, model and mutation (see [`http-api.md`](http-api.md)) |

---

## Engine-side models (`state.py`)

The engine reads each block with `<Model>.from_config(config)`: a missing or non-object block gives the
defaults, keys the model does not define are ignored, and a value that fails the model's validators
raises. The engine models also carry `type` and `schema_version` discriminator fields with no
behavioural effect.

### Configuration models

`CognitionConfig`, `SelectionConfig`, `RetrievalConfig` and `PersonaConfig` have the same fields and
defaults as the request models above, except:

| Field | Request model | Engine model |
|---|---|---|
| `retrieval.cite_inline` | default `true` | default `false` |
| `retrieval.mode` | any string | validated: `fts` \| `vector` \| `hybrid` |
| `selection.method` | any string | validated: `moderated` \| `rotation` \| `simultaneous` \| `hybrid` |
| `selection.hybrid_opening_rounds` | no bound | `>= 1` |
| `personas.dismissal_rule` | any value | coerced (`true` to `mandatory`, `false` to `off`) and validated |

Research, assumptions, dynamic assumptions, injections, consultants and `consult_limit` are parsed by
plain functions that never raise: invalid entries are skipped, strings are trimmed and truncated to the
API limits, and `research_state.settings_from` and `assumptions.dynamic_from_config` also accept a bare
`true`.

### State models

Stored in snapshots and returned by the snapshot and dossier routes.

`MemoryItem`

| Field | Type | Default |
|---|---|---|
| `id` | string | random hex |
| `timestamp` | integer (Unix seconds) | required |
| `content` | string | required |
| `importance` | number or null | null |
| `tags` | list of strings | `[]` (a reflection carries `reflection`) |
| `metadata` | object | `{}` |

`PendingThread`

| Field | Type | Default |
|---|---|---|
| `id` | string | 12 hex characters |
| `description` | string | required |
| `thread_type` | string | `setup` (`setup` \| `promise` \| `faction-action` \| `deferred-consequence`) |
| `origin_turn` | integer | required |
| `origin_agent` | string or null | null |
| `status` | string | `open` (`open` \| `resolved` \| `abandoned`) |
| `resolved_turn` | integer or null | null |

`AgentState`

| Field | Type | Default |
|---|---|---|
| `name` | string | required |
| `persona` | string | required |
| `structured` | `StructuredPersona` or null | null |
| `memory_stream` | list of `MemoryItem` | `[]` |
| `goals` | list of strings | `[]` |
| `relationships` | object of string to string | `{}` |
| `conversation_history` | list of objects | `[]` |
| `total_tokens_in`, `total_tokens_out` | integer | 0 |
| `total_cost_usd` | number | 0.0 |
| `portrait` | string or null | null (legacy base64 avatar, read only) |
| `portrait_key` | string or null | null (blob key of the avatar) |

`SimSnapshot`

| Field | Type | Default |
|---|---|---|
| `run_id` | string | required |
| `turn` | integer | required |
| `topic` | string | required |
| `agents` | object of name to `AgentState` | required |
| `conversation` | list of message objects | `[]` |
| `pending_threads` | list of `PendingThread` | `[]` |
| `firsthand_citations` | list of `[speaker, document_title]` pairs | `[]` |
| `decline_streak` | integer | 0 |
| `status` | string | required |
| `created_at` | integer | required |
| `completed_at` | integer or null | null |
| `total_turns` | integer | required |
| `error_message` | string or null | null |
