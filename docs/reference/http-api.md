# HTTP API reference

Every HTTP route the Matrix Studio server answers, and its WebSocket stream, grouped by resource. Derived from `matrix_studio/api/app.py` (routes and request models), `matrix_studio/api/manager.py`, `matrix_studio/service.py`, `matrix_studio/branching.py`, `matrix_studio/orchestration.py`, `matrix_studio/ensemble_reporting.py`, `matrix_studio/forecast.py` and `matrix_studio/storage/dynamo.py` (response shapes).

See also: [explanation](../explanation/), [how-to guides](../how-to/), [run-config.md](run-config.md) (the create-run body), [events.md](events.md) (event payloads), [data-model.md](data-model.md) (what the response fields mean), [settings.md](settings.md) (the environment that changes route behaviour).

## Conventions

These apply to every route unless the entry says otherwise.

| Topic | Rule |
|---|---|
| Base path | Every route is under `/api`, except `/config.json`, `/` and the static SPA routes. No version prefix. |
| Methods | `GET`, `POST` and `DELETE` only. There is no `PUT` or `PATCH`. The deployed CORS rule allows `GET`, `POST`, `DELETE` and `OPTIONS`, with headers `authorization` and `content-type`. |
| Identity | The caller is the `sub` claim of a JWT verified by API Gateway (`AUTH_MODE=jwt`, the deployed stack). In `AUTH_MODE=single-user` (the default, local) every request is the fixed identity `local-single-user`. A request that reached the app through an authorizer but carries no `sub` is `401 Authenticated request carried no subject claim`; in `jwt` mode with no authorizer claims it is `401 Authentication required`. |
| Groups | The caller's Cognito groups come from the verified `cognito:groups` claim, as a list or a bracketed string. Missing groups are an empty list, never an error. Groups decide knowledge-base grants and the monthly cost cap. |
| Ownership | Runs, ensembles, cast templates, threads and run documents are read through the caller's own partition. Another owner's resource is `404`, with the same body as one that does not exist. Knowledge bases use a read check (owner or grantee) and a write check (owner only); both answer `404` when they fail. |
| Deployed gateway | On the deployed stack every route, including `/api/health`, sits behind the JWT authorizer (one `ANY /{proxy+}` route), so an unauthenticated request is refused by API Gateway with `401` before the app runs. The integration timeout is about 30 seconds, which is why some routes answer `202` and hand the work to a worker. |
| Run reference `{ref}` | A run id, or a run name (exact match on the stored name; names are stored lowercased). Resolution tries the id first. |
| Timestamps | Unix seconds, integer. |
| Money | US dollars, float. |
| Error body | Errors raised by a route are `{"detail": "<message>"}`. Request-validation errors (Pydantic) are `422` with `{"detail": [{"type", "loc", "msg", "input", ...}]}`. |
| Unknown body keys | Refused (`422 extra_forbidden`) only where the model forbids extras: `config` itself, `config.injections[]`, `config.assumptions[]`, `config.dynamic_assumptions` and `config.experts[]`. Elsewhere unknown keys are dropped silently. See [run-config.md](run-config.md). |
| Real names | A persona or consultant never carries the name of a real, widely known person (`matrix_studio/real_names.py`). Every route that takes a cast — `POST /api/runs`, `POST /api/ensembles`, `POST /api/personas/suggest`, `POST` and `GET /api/cast-templates`, `add_persona` at `POST /api/runs/{ref}/branch` — replaces such a name with a fictional sound-alike before anything is stored, replaces the real name with the same parody in the request's persona and consultant text, topic, assumptions and scheduled messages (not in documents), and lists each replacement in `renamed`: `{from, to, reason: "real public figure", source: "list" | "model", role: "persona" | "consultant"}`. A single first name or surname never matches. [`POST /api/personas/check-names`](#post-apipersonascheck-names) asks the same question without creating anything. |
| Local vs deployed | Where behaviour differs, the entry says which. Each deployed behaviour is selected by its own variable, all set by the CDK stack: `TURN_LOOP_ARN` (runs, branches, resumes), `ASIDE_FUNCTION` (aside replies and requested summaries), `RESEARCH_FUNCTION` (ensemble research), `ENSEMBLE_REPORT_FUNCTION` (ensemble reports). See [settings.md](settings.md). |

### Status codes used

| Status | Meaning in this API |
|---|---|
| `200` | Read, or a synchronous write that finished. |
| `201` | Created: run, branch, ensemble, cast template, knowledge base, KB document, grant, run document, thread, thread message (local). |
| `202` | Accepted, finishing later: stop requested; summary or aside reply dispatched to the aside worker; ensemble report dispatched to the report worker. |
| `402` | The caller is over their monthly cost cap (or their spend could not be read). Run-starting routes only. |
| `403` | The structured turn view is switched off. |
| `404` | Not found, or not the caller's. |
| `409` | The resource is in a state that refuses the request: stop a finished run, resume a run that is not resumable, summarise a run that is not complete, a cast-template name clash, an aside question still awaiting its reply, an ensemble report refused. |
| `413` | An uploaded file, or the text extracted from it, is over the size limit. |
| `422` | The body or query is invalid, including semantic refusals (bad mutation, unreadable knowledge base, unsupported file type). |
| `502` | An upstream model failed: persona drafting, avatar generation, or embedding a knowledge-base document. |
| `504` | A local aside reply passed its 26-second deadline. |

## Route index

| Method | Path | Group |
|---|---|---|
| `GET` | `/api/health` | [Service](#service) |
| `GET` | `/config.json` | [Service](#service) |
| `GET` | `/` | [Service](#service) |
| `GET` | `/{full_path:path}` | [Service](#service) |
| `GET` | `/api/models` | [Models, names and forecasts](#models-names-and-forecasts) |
| `GET` | `/api/name/suggest` | [Models, names and forecasts](#models-names-and-forecasts) |
| `POST` | `/api/runs/forecast` | [Models, names and forecasts](#models-names-and-forecasts) |
| `POST` | `/api/ensembles/forecast` | [Models, names and forecasts](#models-names-and-forecasts) |
| `POST` | `/api/personas/suggest` | [Cast templates and persona packs](#cast-templates-and-persona-packs) |
| `POST` | `/api/personas/check-names` | [Cast templates and persona packs](#cast-templates-and-persona-packs) |
| `GET` | `/api/persona-packs` | [Cast templates and persona packs](#cast-templates-and-persona-packs) |
| `GET` | `/api/cast-templates` | [Cast templates and persona packs](#cast-templates-and-persona-packs) |
| `POST` | `/api/cast-templates` | [Cast templates and persona packs](#cast-templates-and-persona-packs) |
| `GET` | `/api/cast-templates/{name}` | [Cast templates and persona packs](#cast-templates-and-persona-packs) |
| `DELETE` | `/api/cast-templates/{name}` | [Cast templates and persona packs](#cast-templates-and-persona-packs) |
| `POST` | `/api/runs` | [Runs](#runs) |
| `GET` | `/api/runs` | [Runs](#runs) |
| `GET` | `/api/runs/{ref}` | [Runs](#runs) |
| `GET` | `/api/runs/{ref}/setup` | [Runs](#runs) |
| `GET` | `/api/runs/{ref}/tree` | [Runs](#runs) |
| `POST` | `/api/runs/{ref}/stop` | [Run lifecycle](#run-lifecycle) |
| `POST` | `/api/runs/{ref}/resume` | [Run lifecycle](#run-lifecycle) |
| `POST` | `/api/runs/{ref}/branch` | [Run lifecycle](#run-lifecycle) |
| `POST` | `/api/runs/{ref}/hidden` | [Run lifecycle](#run-lifecycle) |
| `GET` | `/api/runs/{ref}/events` | [Event log and checkpoints](#event-log-and-checkpoints) |
| `WS` | `/api/runs/{ref}/stream` | [Event log and checkpoints](#event-log-and-checkpoints) |
| `GET` | `/api/runs/{ref}/snapshots` | [Event log and checkpoints](#event-log-and-checkpoints) |
| `GET` | `/api/runs/{ref}/snapshots/{turn}` | [Event log and checkpoints](#event-log-and-checkpoints) |
| `GET` | `/api/runs/{ref}/agents/{name}/dossier` | [Introspection](#introspection) |
| `GET` | `/api/runs/{ref}/turns/{turn}/trace` | [Introspection](#introspection) |
| `GET` | `/api/runs/{ref}/turns/{turn}/structured` | [Introspection](#introspection) |
| `GET` | `/api/runs/{ref}/pending-threads` | [Introspection](#introspection) |
| `GET` | `/api/runs/{ref}/quotes` | [Introspection](#introspection) |
| `GET` | `/api/runs/{ref}/sources/{document_id}` | [Introspection](#introspection) |
| `GET` | `/api/runs/{ref}/summary` | [Summaries](#summaries) |
| `POST` | `/api/runs/{ref}/summary` | [Summaries](#summaries) |
| `GET` | `/api/runs/{ref}/threads` | [Asides and threads](#asides-and-threads) |
| `POST` | `/api/runs/{ref}/threads` | [Asides and threads](#asides-and-threads) |
| `GET` | `/api/threads/{thread_id}` | [Asides and threads](#asides-and-threads) |
| `POST` | `/api/threads/{thread_id}/messages` | [Asides and threads](#asides-and-threads) |
| `GET` | `/api/runs/{ref}/documents` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `POST` | `/api/runs/{ref}/documents` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `DELETE` | `/api/runs/{ref}/documents/{document_id}` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `POST` | `/api/runs/{ref}/documents/reindex` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `POST` | `/api/runs/{ref}/documents/embed` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `GET` | `/api/runs/{ref}/documents/search` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `GET` | `/api/documents/formats` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `POST` | `/api/documents/extract` | [Run documents and file extraction](#run-documents-and-file-extraction) |
| `GET` | `/api/knowledge-bases` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `POST` | `/api/knowledge-bases` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `GET` | `/api/knowledge-bases/{kb_id}` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `POST` | `/api/knowledge-bases/{kb_id}/documents` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `DELETE` | `/api/knowledge-bases/{kb_id}/documents/{document_id}` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `GET` | `/api/knowledge-bases/{kb_id}/grants` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `POST` | `/api/knowledge-bases/{kb_id}/grants` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `DELETE` | `/api/knowledge-bases/{kb_id}/grants` | [Knowledge bases, documents and grants](#knowledge-bases-documents-and-grants) |
| `POST` | `/api/ensembles` | [Ensembles](#ensembles) |
| `GET` | `/api/ensembles` | [Ensembles](#ensembles) |
| `GET` | `/api/ensembles/{ensemble_id}` | [Ensembles](#ensembles) |
| `POST` | `/api/ensembles/{ensemble_id}/report` | [Ensembles](#ensembles) |
| `POST` | `/api/runs/{ref}/agents/{name}/regenerate-avatar` | [Avatars](#avatars) |
| `GET` | `/api/runs/{ref}/agents/{name}/avatar` | [Avatars](#avatars) |
| `GET` | `/api/runs/{ref}/export` | [Exports and briefs](#exports-and-briefs) |
| `GET` | `/api/ensembles/{ensemble_id}/export` | [Exports and briefs](#exports-and-briefs) |
| `GET` | `/api/runs/{ref}/brief` | [Exports and briefs](#exports-and-briefs) |
| `GET` | `/api/ensembles/{ensemble_id}/brief` | [Exports and briefs](#exports-and-briefs) |

FastAPI's own `GET /docs`, `GET /redoc` and `GET /openapi.json` are also served. The OpenAPI schema omits the WebSocket route and the behaviour described here.

`scripts/list_routes.py` prints the app's routing table (`--check docs/reference/http-api.md` compares it with this file), and `tests/test_reference_routes.py` fails when a route the app serves has no entry or index row here, or when this file documents an `/api` route the app no longer serves.

---

## Service

### `GET /api/health`

Liveness, plus which model providers have credentials configured. Never returns key values.

- **Auth:** none at the app level (the deployed authorizer still applies).
- **Parameters:** none.
- **Success:** `200`.

| Response field | Type | Notes |
|---|---|---|
| `status` | string | Always `"ok"`. |
| `readiness.openai` | boolean | `OPENAI_API_KEY` is set. |
| `readiness.anthropic` | boolean | `ANTHROPIC_API_KEY` is set. |
| `readiness.bedrock` | boolean | An AWS access key, secret key or `AWS_BEARER_TOKEN_BEDROCK` is in settings or the environment. An instance or role credential is not detected, so this can be `false` on a host that can call Bedrock. |

```json
{"status": "ok", "readiness": {"openai": false, "anthropic": false, "bedrock": true}}
```

### `GET /config.json`

The SPA's runtime configuration. Served by the app only locally; on the deployed stack CloudFront serves `/config.json` from the SPA bucket and this route is not reached.

- **Auth:** none.
- **Success:** `200`, header `Cache-Control: no-store`.

| Response field | Type | Source |
|---|---|---|
| `hostedUiUrl` | string | `COGNITO_HOSTED_UI_URL`, default `""`. |
| `clientId` | string | `COGNITO_CLIENT_ID`, default `""`. |
| `userPoolId` | string | `COGNITO_USER_POOL_ID`, default `""`. |
| `authRequired` | boolean | `false` when `AUTH_MODE=single-user`, else `true`. |

### `GET /`

- With a built frontend (`matrix_studio/static/index.html` exists): the SPA shell, `Cache-Control: no-cache, must-revalidate`.
- Without one: `200` JSON `{"message": "TheMatrix Simulation Studio API is running. ...", "docs": "/docs"}`.

### `GET /{full_path:path}`

Registered only when the frontend is built. Serves a file under `matrix_studio/static/` when one exists at that path, otherwise the SPA shell (client-side routing). Content-hashed assets (under `/assets/`, or names matching `-<8+ chars>.js|css|woff|woff2`) get `Cache-Control: public, max-age=31536000, immutable`; everything else gets `no-cache, must-revalidate`. `/assets/*` is also served by a static mount with the immutable header. Registered after every API route, so it never shadows one.

---

## Models, names and forecasts

### `GET /api/models`

The model strings the deployment allows, and what each model role resolves to with nothing overridden.

- **Auth:** none at the app level.
- **Success:** `200`.

| Response field | Type | Notes |
|---|---|---|
| `default` | string | `LITELLM_MODEL`. |
| `models` | array of `{id, label}` | `AVAILABLE_MODELS`, in order. `label` is a friendly name for known ids, else the id's last path segment. |
| `roles` | object, role to model | What each role uses for a run that sets neither `model` nor `models` ([settings.md](settings.md) has the role table). |
| `overridable_roles` | array of strings | Keys accepted in `config.models`. |
| `roles_pinned_by_default` | array of strings | Roles whose default is not the conversation model: `name_check`, `naming`, `speaker_selection`, `stance`, `validation`. |

```json
{
  "default": "bedrock/global.anthropic.claude-sonnet-5",
  "models": [{"id": "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0", "label": "Haiku 4.5"}],
  "roles": {"voice": "bedrock/global.anthropic.claude-sonnet-5", "validation": "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"},
  "overridable_roles": ["voice", "speaker_selection", "validation", "reflection", "summary", "aside", "naming", "wizard", "pressure", "stance", "name_check"],
  "roles_pinned_by_default": ["name_check", "naming", "speaker_selection", "stance", "validation"]
}
```

Edge cases:
- `roles.naming` and `roles.wizard` are reported, but name suggestion and persona drafting call `model or LITELLM_MODEL` directly and do not consult the role table.
- `name_check` is the one role a run's `model` does not move: only `config.models.name_check` does ([run-config.md](run-config.md#configmodels-and-configmodel)).

### `GET /api/name/suggest`

A memorable codename and one-line description for a topic. Makes one model call (falls back to a word list on failure).

- **Auth:** caller (uniqueness is checked against the caller's run names only).
- **Success:** `200`.

| Query | Type | Required | Notes |
|---|---|---|---|
| `topic` | string | yes | Missing is `422`. |

| Response field | Type | Notes |
|---|---|---|
| `name` | string | Lowercase, hyphenated, unique among the caller's runs at the time of the call. |
| `description` | string | |
| `slug` | string | |
| `source` | string | `"llm"` or `"fallback"`. |

```json
{"name": "quiet-lantern", "description": "Weighing weekend opening hours", "slug": "quiet-lantern", "source": "llm"}
```

### `POST /api/runs/forecast`

What `POST /api/runs` with the same body would cost, priced from the caller's own finished runs. No model call; nothing is created.

- **Auth:** caller and groups (for the budget block).
- **Body:** a create-run body, validated exactly as `POST /api/runs` ([run-config.md](run-config.md)). The KB-binding and cost-cap preflight is not run.
- **Success:** `200`.

| Response field | Type | Notes |
|---|---|---|
| `runs` | integer | Always `1`. |
| `parts` | array | One entry per cost component (below). |
| `low`, `typical`, `high` | number | Sums over the measured parts only. |
| `complete` | boolean | `false` when any part is unmeasured, so the total is a lower bound. |
| `unmeasured` | array of strings | Part names with no comparable history. |
| `thin` | array of strings | Parts priced from fewer than 3 past observations. |
| `responses` | `{low, typical, high}` | Persona responses expected. |
| `budget` | object or null | `{cap, spent, remaining}` when a monthly cap applies to the caller; `null` otherwise, or when spend could not be read. |

Each `parts[]` entry: `part` (string: `conversation`, `speaker selection and checks`, `consultations`, `assumption checks`, `avatars`, `summary`, `research`), `low`, `typical`, `high` (number or null), `measured` (boolean), `basis` (string, how it was priced), `based_on` (integer or null), `runs` (integer).

```json
{
  "runs": 1,
  "parts": [{"part": "conversation", "low": 0.21, "typical": 0.3, "high": 0.42, "measured": true,
             "basis": "12 responses × $0.0175–0.0350 each, from 4 past claude-sonnet-5 run(s) with 3 personas, similar length",
             "based_on": 4, "runs": 1}],
  "low": 0.21, "typical": 0.3, "high": 0.42,
  "complete": true, "unmeasured": [], "thin": [],
  "responses": {"low": 12, "typical": 12, "high": 12},
  "budget": null
}
```

Edge cases:
- The history is cached per owner in the serving process for 300 seconds, so a run that finished in that window is not yet counted.
- A part with no history has `low`, `typical` and `high` set to `null` and is excluded from the totals.

### `POST /api/ensembles/forecast`

What `POST /api/ensembles` with the same body would cost: every planned member priced with its own cell's config, one research pass, and the report.

- **Auth:** caller and groups.
- **Body:** a create-ensemble body ([Ensembles](#post-apiensembles)).
- **Success:** `200`. Same shape as the run forecast without `responses`; `runs` is the member count, per-run parts are summed (`basis` is prefixed `×N runs; each:`), and an `ensemble report` part is always present.

| Status | When |
|---|---|
| `422` | The cell spec is refused (same messages as `POST /api/ensembles`). |

---

## Cast templates and persona packs

### `POST /api/personas/suggest`

Draft a cast of structured personas from a short brief. Returns a draft; starts nothing.

- **Auth:** caller. The drafting call is not charged to the caller's monthly spend; the real-name check on the draft is.
- **Success:** `200`.

| Body field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `brief` | string | yes | | Blank is a `502` (see edge cases). |
| `count` | integer | no | `5` | `2` to `7`. |
| `model` | string | no | `LITELLM_MODEL` | Any model string; not checked against `AVAILABLE_MODELS`. |

| Response field | Type | Notes |
|---|---|---|
| `cast` | array | Each `{name, persona, goals, structured}`, where `structured` is `{role, background.formative_events[], preferences{optimises_for, dismisses, persuaded_by}, viewpoints[]}`. Entries that would fail `StructuredPersona` validation are dropped. Names are de-duplicated case-insensitively. |
| `count` | integer | Length of `cast`; can be below the requested count. |
| `renamed` | array | Real public figures' names this request carried, and what each became: `{from, to, reason, source, role}` (see [Conventions](#conventions)). Empty when none. The draft asks for first names only, so this is usually empty. |

| Status | When |
|---|---|
| `502` | The model call failed, returned nothing usable, or the brief was blank. `detail` names which. |

### `POST /api/personas/check-names`

Which of these names are real, widely known people, and what each would become. Creates nothing. The new-run form calls it when a name field loses focus and when a cast is filled in one go, so it can show the switch on the persona; the create routes apply the same check regardless.

- **Auth:** caller and groups. Model calls are charged to the caller's monthly spend.
- **Success:** `200`.

| Body field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `names` | array of strings | no | `[]` | At most 40. Persona names. |
| `consultants` | array of strings | no | `[]` | At most 10. Consultant names (only `role` differs). |

| Response field | Type | Notes |
|---|---|---|
| `renamed` | array | One `{from, to, reason, source, role}` per name that must change, in request order. A name that may stay is not listed. |

How a name is checked, first match wins:

1. **The curated list** (`matrix_studio/public_figures.json`, 243 people): full names and common variants, case- and accent-insensitive, ignoring a leading title (`Dr.`, `Sir`, `President`), a trailing `Jr.` or `III`, middle initials and anything after a comma or in brackets. Free. `source: "list"`; `to` is the hand-written parody.
2. **The model check**, for two to six real words not on the list: one `name_check` call (Haiku 4.5, temperature 0, a JSON schema), cached in the process. Only `famous: true` at `high` confidence counts; the model is told ordinary names shared by many people are not famous. Its suggested parody is used only if it is spelled differently in every word, not too close to the real spelling, not unkind, not on the list and — asked again — not famous itself; otherwise a deterministic respelling is used. `source: "model"`.

```json
{"names": ["Jeff Bezos", "Ruth", "Priya Okonjo"]}
```

```json
{"renamed": [{"from": "Jeff Bezos", "to": "Geoff Beesoh", "reason": "real public figure",
              "source": "list", "role": "persona"}]}
```

Edge cases:
- A single word (`Ruth`, `Jeff`, `Bezos`) never matches and never costs a model call.
- A failed or unreadable model reply counts as not famous and is logged. A failed call is not cached, so the next request asks again.
- Over the monthly cap (or with spend unreadable), only the curated list is applied: the model check is skipped rather than charged.
- More than 40 names is a `422`. A name longer than 200 characters is clipped, which cannot change its verdict.

### `GET /api/persona-packs`

Ready-made persona archetypes. Static: the same for every caller.

- **Auth:** caller (required, not used).
- **Success:** `200`.

| Response field | Type | Notes |
|---|---|---|
| `packs` | array | Each `{id, label, summary, persona, qualification}`, where `persona` is a cast entry (`name`, `persona`, `goals`, `structured`). |
| `qualification` | string | `"not yet qualified"`. |

### `GET /api/cast-templates`

The caller's saved casts, most recently saved first.

- **Auth:** caller.
- **Success:** `200`.

| Response field | Type | Notes |
|---|---|---|
| `templates[]` | array | `{name, description, personas, updated_at}`; `personas` is the list of persona names, with a curated real name shown as its parody (the list only; loading the template runs the full check). |

### `POST /api/cast-templates`

Save a cast under a name.

- **Auth:** caller.
- **Success:** `201`.

| Body field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `name` | string | yes | | 1 to 80 characters; trimmed; may not be blank or contain `/`. |
| `description` | string | no | `null` | Up to 500 characters. |
| `cast` | array of persona objects | yes | | 1 to 30 entries; same persona model as a create-run cast. |
| `overwrite` | boolean | no | `false` | Replace a template of the same name. |

| Response field | Type | Notes |
|---|---|---|
| `name`, `description`, `cast`, `created_at`, `updated_at` | | The stored template. `created_at` is kept from the template it replaced. |
| `dropped_documents` | integer | How many `document_texts` and `documents` entries were removed. Templates never store document text. |
| `renamed` | array | Real names replaced before saving (see [Conventions](#conventions)); the template is stored with the parodies. |

| Status | When |
|---|---|
| `409` | A template with that name exists and `overwrite` is false. |
| `422` | Name rules, cast length, or a persona field is invalid. |

```json
{"name": "library board", "cast": [{"name": "Ada", "persona": "A librarian", "goals": ["Keep staff rested"]}]}
```

### `GET /api/cast-templates/{name}`

- **Auth:** caller.
- **Success:** `200` with `{name, description, cast, created_at, updated_at, renamed}`. The cast is checked on the way out, so a template saved before the check existed loads with its real names replaced and listed in `renamed`; the stored template is not changed.
- **Errors:** `404 No template named '<name>'`.

### `DELETE /api/cast-templates/{name}`

- **Auth:** caller.
- **Success:** `200` with `{"deleted": "<name>"}`.
- **Errors:** `404 No template named '<name>'`.

---

## Runs

### `POST /api/runs`

Start a conversation. Returns at once; turns are generated in the background.

- **Auth:** caller and groups. The groups are stored on the run so the worker can resolve group-granted knowledge bases.
- **Body:** `CreateRunModel`: `topic` (string, required), `cast` (array, required, at least one), `config` (object, default `{}`), `model` (string), `name` (string), `description` (string), `summary` (object). Every field is in [run-config.md](run-config.md).
- **Success:** `201`.

Checks, in order: request validation (`422`); at least one persona (`422 At least one persona is required`); every knowledge base bound at run, persona or consultant level is readable by the caller (`422 These knowledge bases do not exist or are not shared with you: <ids>`); the monthly cost cap (`402`). Then real names are replaced (see [Conventions](#conventions)), and the run is stored with the replacements; the model calls that takes are charged to the caller's monthly spend.

| Response field | Type | Notes |
|---|---|---|
| `run_id` | string (UUID) | |
| `name` | string | The supplied name lowercased, or a generated codename. A clash with one of the caller's run names is resolved by appending `-2` ... `-99`. |
| `description` | string | Supplied, else the generated description; with a supplied name and no description, the first 80 characters of the topic. |
| `slug` | string | Same as `name`. |
| `name_source` | string | `"user"`, `"llm"` or `"fallback"`. |
| `topic` | string | |
| `status` | string | `"pending"` deployed (the state machine flips it to `running`), `"running"` locally. |
| `renamed` | array | Real public figures' names this request carried, and what each became: `{from, to, reason, source, role}` (see [Conventions](#conventions)). Empty when none. |

| Status | When |
|---|---|
| `402` | Over the monthly cap: `You have spent $X of your $Y monthly budget, so this run was not started...`; or spend unreadable: `Could not read your monthly spend, so this run was refused...`. |
| `422` | Validation, empty cast, unreadable knowledge base, `model` and `config.model` both set and different, consultants without `config.retrieval.enabled`, a consultant name equal to a persona or another consultant, an injection at or after `max_messages`. |

```http
POST /api/runs
Content-Type: application/json

{"topic": "Should the town library open on Sundays?",
 "cast": [{"name": "Ada", "persona": "A librarian", "goals": ["Keep staff rested"]},
          {"name": "Ben", "persona": "A parent", "goals": ["Weekend access"]}],
 "config": {"max_messages": 12}}
```

```json
{"run_id": "<run-id>", "name": "quiet-lantern", "description": "Weighing weekend opening hours",
 "slug": "quiet-lantern", "name_source": "llm", "topic": "Should the town library open on Sundays?",
 "status": "pending", "renamed": []}
```

Edge cases:
- With `config.research.enabled`, new knowledge bases are created and bound before the run row is written; a failure there fails the request.
- A top-level `summary` block is stored as `config.summary`; a top-level `model` is stored as `config.model`.
- Deployed, a run whose execution did not start stays at `pending`.

### `GET /api/runs`

The caller's runs, newest first.

- **Auth:** caller.
- **Success:** `200` with `{"runs": [<run summary>, ...]}`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `q` | string | none | Case-insensitive substring match on name, description or topic. Empty means no filter. |

At most 200 runs (newest first, after filtering). Hidden runs are included; the client filters them. Ensemble members are included.

Run summary fields (also the first fields of `GET /api/runs/{ref}`):

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | |
| `name`, `description`, `slug`, `topic` | string | |
| `status` | string | `pending`, `running`, `complete`, `failed`, `stopped`, `capped`, `interrupted`. |
| `turn_count` | integer | Count of `agent.response` events, so it includes injected messages and closing statements, and counts every response in a simultaneous round. |
| `total_cost_usd` | number | Sum of `cost_usd` over the event log only. Excludes the summary, research and stance classifier; see `cost.total` on the detail route. |
| `created_at`, `completed_at`, `last_event_at` | integer or null | `last_event_at` is the newest event's write time. |
| `parent_run_id`, `branch_turn` | string / integer or null | Set on branches. |
| `ensemble_id`, `ensemble_cell` | string or null | Set on ensemble members. |
| `hidden` | boolean | Always present. |
| `max_messages` | integer or null | `config.max_messages`, when an integer. |
| `cast_names` | array of strings | |
| `stance` | object or null | Persona name to `support`, `conditional`, `holding` or `unstated`; `null` until the run is summarised. |

### `GET /api/runs/{ref}`

One run, with everything the run page needs.

- **Auth:** caller.
- **Success:** `200`.
- **Errors:** `404 Run not found`.

Response: the run summary fields above, plus:

| Field | Type | Notes |
|---|---|---|
| `cast` | array | The stored cast (as created, with research allocations applied). |
| `config` | object | The stored config, `{}` if unreadable. Includes server-written keys such as `summary`, `model`, `branch_mutation` and `research.targets`. |
| `cost` | object | `in_run` (event log), `by_kind` (event type to cost), `summary` (latest generated summary), `research` (research record), `stance` (closing-statement classifier), `total` (sum of the four). Rounded to 6 places. |
| `models` | object | Every role and the model it resolves to from this run's stored config. |
| `result` | object or null | From the latest checkpoint: `conversation`, `agents` (name to agent state), `total_turns`, `total_cost_usd` (sum of agent voice costs only). `null` until the first checkpoint exists; present while a run is still going. |
| `summary` | object | `{generated, imported}`, each the latest summary of that kind or `null`. |
| `lineage` | object | `parent` (`{run_id, name, branch_turn}` or null) and `branches` (direct children: `{run_id, name, branch_turn, status, created_at}`, newest first). |
| `research` | object or null | The research record; `null` when no research ran. |
| `stance_basis` | object or null | `{personas: {name: {stance, source, class, quote, fallback?, claimed?}}, classifier}`; `null` for runs summarised before the field existed. |

Edge cases:
- Three different totals appear: `total_cost_usd` (event log), `result.total_cost_usd` (agents' own voice cost) and `cost.total` (whole run). Only `cost.total` includes the summary, research and stance classifier. See [data-model.md](data-model.md).
- A hidden run opens normally.

### `GET /api/runs/{ref}/setup`

This run's definition, shaped as a create-run body, for starting a fresh run from it.

- **Auth:** caller.
- **Success:** `200` with `{run_id, setup, warnings}`.
- **Errors:** `404 Run not found`.

`setup` has `topic`, `cast`, `config`, and `model`, `name`, `description` when set. Each cast member keeps its stored keys except `documents` and `document_texts`, which are rebuilt from the run's stored documents (persona-scoped ones only). `config` keeps only `max_messages`, `generate_avatars`, `cognition`, `retrieval`, `personas`, `knowledge_bases`, `selection`, `research` (without `targets`) and `models`. `personas.withhold_concerns` is written out with the source run's value, `true` when it recorded none, so a copy of a run that withheld its concerns withholds them too.

`warnings[]` (strings) reports: documents with no recoverable text (skipped); cast-wide documents (cannot be carried into a create-run body); knowledge bases this run's research wrote into (removed from the bindings).

Edge cases:
- Not carried: `experts`, `consult_limit`, `assumptions`, `dynamic_assumptions`, `injections` and `summary`. A run created from the setup has none of them unless they are added back.
- `name` is the old run's name; posting it unchanged creates `<name>-2`.

### `GET /api/runs/{ref}/tree`

The whole branch lineage that contains this run, rooted at its earliest ancestor.

- **Auth:** caller.
- **Success:** `200` with `{root_id, nodes}`.
- **Errors:** `404 Run not found`.

`nodes` maps run id to `{id, name, slug, status, branch_turn, parent_run_id, created_at, turn_count, total_cost_usd, mutation_kind}`. `mutation_kind` is the branch's `config.branch_mutation.kind`, or `null`. Ensemble membership is not lineage and does not appear here.

---

## Run lifecycle

### `POST /api/runs/{ref}/stop`

Ask a live run to stop after the turn it is generating. The turn in flight finishes and is saved; the run then ends as `stopped`.

- **Auth:** caller.
- **Body:** none.
- **Success:** `202` with `{"run_id": "<run-id>", "status": "stopping", "stop_requested": true}`.

| Status | When |
|---|---|
| `404` | Run not found. |
| `409` | The run is already `complete`, `failed`, `stopped`, `capped` or `interrupted` (`Run is '<status>'; there is nothing to stop.`), or the row disappeared before the flag was written. |

Edge cases:
- Idempotent: repeating it on a live run is another `202`.
- Accepted for a `pending` run.
- The returned `status` is not stored; the run's own status changes when the engine sees the flag. A stopped run does not get an automatic summary.

### `POST /api/runs/{ref}/resume`

Continue a `stopped`, `interrupted` or `failed` run in place (same id and name) from its last checkpoint.

- **Auth:** caller.
- **Body:** none.
- **Success:** `200` with `{"run_id": "<run-id>", "name": "<name>", "status": "running"}`. The status is written before the response.

| Status | When |
|---|---|
| `404` | Run not found. |
| `409` | The status is not resumable (`complete`, `capped`, `running`, `pending`): `Run is '<status>'; only failed/interrupted/stopped runs can be resumed (use a branch to continue a completed run).`; or, locally, the run already has a live task. |

Edge cases:
- The dangling tail after the last checkpoint and any terminal event are removed before generating.
- If the checkpoint is at or past the budget, the budget is extended by the run's original budget, so a resume always generates.
- The stop flag is cleared.
- Locally, a crash during the resume sets the run back to `failed`.

### `POST /api/runs/{ref}/branch`

Fork a run at a turn into a new run that continues from there. The parent is never modified.

- **Auth:** caller. The branch belongs to the parent's owner.
- **Success:** `201`.

| Body field | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `from_turn` | integer | yes | | `>= 0`, and at most the parent's `turn_count` (its count of `agent.response` events). |
| `name` | string | no | generated | Lowercased; de-duplicated with `-2` ... `-99`. |
| `description` | string | no | `Branch of <parent> @ turn <n>` | |
| `model` | string | no | parent's `config.model` | Not inherited from an imported parent. |
| `mutation` | object | no | `null` (a plain fork) | One mutation, below. |

Mutation kinds (`mutation.kind`) and their fields. Fields not listed for a kind are ignored.

| `kind` | Required fields | Optional fields | Effect |
|---|---|---|---|
| `continue` | `add_budget` (int, `>= 1`) | | Generate exactly `add_budget` more turns from the fork. |
| `inject_message` | `speaker`, `content` (non-blank strings) | `source` (string, default `"user"`), `add_budget` (int, `>= 1`) | Adds a message as turn `from_turn + 1` (an `agent.response` with `injected: true`, no cost), then continues. Without `add_budget` the budget is the inherited one plus 1. |
| `edit_goal` | `persona_name`, `goals` (array of strings) | | Replaces that persona's goals. |
| `add_persona` | `name`, `persona` | `goals` | Adds a persona at the fork. A real public figure's name is replaced first, as at run creation, and reported in `renamed`. |
| `remove_persona` | `persona_name` (or `name`) | | Removes a persona; the last persona cannot be removed. |
| `promote_aside` | `thread_id`, `message_id` (int) | | Injects an aside thread message as an `inject_message` with `source: "aside"`, spoken by the message's speaker. |
| `replace_assumption` | `assumption_id` (e.g. `"A3"`), `statement` (max 300 chars) | `basis` (max 300 chars) | Emits `assumption.made` for that id at the fork. |
| `withdraw_assumption` | `assumption_id` | | Emits `assumption.withdrawn` at the fork. |
| `adaptive_pressure` | | `focus` (string), `add_budget` | Experimental. Refused unless `ADAPTIVE_PRESSURE_ENABLED=true`. Generates one narrator event (`pressure.applied`, then an injected `Narrator` turn). |

| Response field | Type | Notes |
|---|---|---|
| `run_id`, `name`, `slug`, `name_source`, `description`, `topic` | | As for a new run. |
| `parent_run_id`, `parent_name`, `branch_turn` | | |
| `status` | string | `"running"`. |
| `max_messages` | integer | The branch's stored budget: the parent's, or `from_turn + parent budget` when the fork is at or past it. |
| `model` | string or null | The branch's generation model; `null` means the settings default. |
| `mutation` | object or null | The validated mutation as stored in `config.branch_mutation`. |
| `renamed` | array | For `add_persona`, the added persona's real name and its replacement; otherwise empty. |

| Status | When |
|---|---|
| `404` | Parent run not found. |
| `422` | `from_turn` out of range (`from_turn must be between 0 and <n> for this run`); unknown `kind` (the message lists the supported kinds); a required mutation field missing; `adaptive_pressure` while disabled. |

```json
{"from_turn": 6, "mutation": {"kind": "inject_message", "speaker": "Town clerk",
                              "content": "The library budget for next year is fixed."}}
```

Edge cases:
- Checks that need the fork state (the persona exists for `edit_goal` and `remove_persona`, the new name is free for `add_persona`, the assumption is in force, the aside message exists, the pressure text passes its guard) run after the `201`, when the branch starts generating. A failure there does not reach the caller. Deployed, the Prepare state's first attempt copies the parent's events and then fails on the mutation; its retry finds the copied events, skips the copy and the mutation, and the branch generates as a plain fork while `config.branch_mutation` (and the tree's `mutation_kind`) still names the mutation. Locally, the background task logs the error and the branch row is left at `running`.
- The upper bound is the response count, not the last turn number. In `rotation`, `simultaneous` and `hybrid` runs several responses share a turn, so a `from_turn` past the parent's last turn is accepted (a 2-turn, 2-persona simultaneous run accepts `from_turn` up to 4).
- A branch copies the parent's events up to and including `from_turn`, preserving `turn` and `seq`, and removes any copied terminal event.
- The branch's stored config is the parent's, with `personas.withhold_concerns` written out: the parent's value, or `true` for a parent from before 2026-10-02 that recorded none. A fork keeps the concern mode its parent ran in.
- With retrieval on, the parent's run documents are copied to the branch.

### `POST /api/runs/{ref}/hidden`

Hide a run from its owner's Runs list, or show it again. Nothing else changes: the run still opens, resumes, branches, belongs to its ensemble and counts towards spend.

- **Auth:** caller.
- **Success:** `200` with `{"run_id": "<run-id>", "hidden": true}`.

| Body field | Type | Required | Notes |
|---|---|---|---|
| `hidden` | boolean | yes | An empty body is `422`. |

| Status | When |
|---|---|
| `404` | Run not found, or removed between the read and the write. |
| `422` | `hidden` missing or not a boolean. |

Edge cases:
- Idempotent: setting the current state again is a `200`.
- Showing removes the stored attribute rather than writing `false`.

---

## Event log and checkpoints

### `GET /api/runs/{ref}/events`

The run's event log, or the part after a sequence number. The polling path for clients that do not hold a WebSocket (the deployed stack has no WebSocket route).

- **Auth:** caller.
- **Success:** `200` with `{"run_id": "<run-id>", "events": [<event>, ...]}`.
- **Errors:** `404 Run not found`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `after_seq` | integer | `-1` | Events with `seq` strictly greater. `-1` means from the start. |
| `limit` | integer | none | At most this many events. Not range-checked: a negative value drops events from the end instead of failing. |

Each event is `{run_id, turn, seq, event_type, agent_name, payload}`; `payload` is an object (`{}` if unreadable). Events are ordered by `(turn, seq)`. The stored write time is not returned. Event types and payloads: [events.md](events.md).

```json
{"run_id": "<run-id>", "events": [
  {"run_id": "<run-id>", "turn": 0, "seq": 0, "event_type": "sim.started", "agent_name": null,
   "payload": {"topic": "Should the town library open on Sundays?", "agent_count": 2}}]}
```

### `WS /api/runs/{ref}/stream`

Replays the persisted events, then streams live ones until a terminal event. Local only: a live tail exists only for a run generating in the same server process, and the deployed HTTP API carries no WebSocket.

- **Auth:** caller (the same identity resolution, from the handshake).
- **Messages:** JSON frames, one event each, in the shape of `GET /api/runs/{ref}/events`.

Behaviour:
1. Identity is resolved before the handshake is accepted; a failure refuses the connection. The connection is then accepted, and an unknown run gets one frame `{"event_type": "error", "payload": {"detail": "Run not found"}}` (no `run_id`, `turn` or `seq`) before the socket closes.
2. Every persisted event is sent in order.
3. With no live generation in this process: if no terminal event was persisted, one synthetic frame is sent, `sim.completed` when the run status is `complete` else `sim.failed`, with `payload: {"status": "<status>"}` and `seq` one past the last; then the socket closes.
4. With live generation: new events are sent as they are emitted, skipping any `seq` already sent, until a terminal event (`sim.completed`, `sim.failed`, `sim.interrupted`, `sim.capped`, `sim.stopped`) or the end of generation; then the socket closes.

Edge cases:
- Step 3 also applies to a run that is `pending`, or generating in another process (a Step Functions worker), so the synthetic frame can be `sim.failed` with `payload.status` `"running"` for a live run. Its `seq` is the one the next real event will use.

### `GET /api/runs/{ref}/snapshots`

The run's per-turn checkpoints, without their bodies.

- **Auth:** caller.
- **Success:** `200` with `{"run_id": "<run-id>", "snapshots": [{"turn": 1, "status": "running", "created_at": 1790000000}, ...]}`.
- **Errors:** `404 Run not found`.

### `GET /api/runs/{ref}/snapshots/{turn}`

The full checkpoint at one turn.

- **Auth:** caller.
- **Success:** `200` with `{run_id, turn, status, topic, total_turns, conversation, agents}`. `agents` maps name to agent state (see [data-model.md](data-model.md)).

| Status | When |
|---|---|
| `404` | `Run not found`, or `No checkpoint at turn <turn>`. |
| `422` | `turn` is not an integer. |

---

## Introspection

### `GET /api/runs/{ref}/agents/{name}/dossier`

One persona's state as of the latest checkpoint, with the documents and knowledge bases it could search and the passages it retrieved.

- **Auth:** caller and groups.
- **Success:** `200`.

| Status | When |
|---|---|
| `404` | `Run not found`, or `Agent not found for this run` (no checkpoint yet, or no agent of that name). |

| Response field | Type | Notes |
|---|---|---|
| `run_id`, `agent`, `persona`, `goals` | | |
| `memory_stream` | array | `{id, content, importance, tags, timestamp}`. Empty unless cognition ran. |
| `beliefs` | array | The memories tagged `reflection`. |
| `pending_threads` | array | Threads this persona opened, each with `stale` (open and at least `cognition.thread_stale_after` turns old, default 5). |
| `documents` | array | This persona's and cast-wide run documents: `{document_id, title, media_type, char_count, chunk_count, cast_wide}`. |
| `knowledge_bases` | array | `{id, name, scope ("run" or "persona"), readable}`. `readable` is re-checked at request time; `name` is `null` when not readable. |
| `document_retrievals` | array | One per `document.retrieved` event for this persona: `{turn, query, total_chars, passages, researched_passages?}`. |
| `cognition_enabled` | boolean | `config.cognition.enabled`. |
| `cognition_lost_turns` | integer | Responses whose cognition reply was discarded (`cognition_parsed: false`). |
| `structured` | object or null | The persona's convictions without `validity`, ever. `viewpoints[].underlying_concern` is included when the run stated concerns plainly and stripped when it withheld them. |
| `withhold_concerns` | boolean | The run's hidden-agendas setting, read as the engine reads it: `true` for a stored config with no value (every run from before 2026-10-02), and for one that no longer parses. The UI shows a concern only when this is `false`. |
| `relationships` | object | |
| `tokens_in`, `tokens_out`, `cost_usd` | number | The persona's accumulated voice usage. |
| `portrait_key` | string or null | Avatar key, for `GET .../avatar?v=`. |
| `portrait_b64` | string or null | Legacy inline portrait; set only on old runs. |

### `GET /api/runs/{ref}/turns/{turn}/trace`

Why a turn's speaker said what they said, when cognition captured a rationale.

- **Auth:** caller.
- **Success:** `200`.

| Status | When |
|---|---|
| `404` | `Run not found`, or `No turn <turn> in this run` (no `agent.response` at that turn). |

With no captured rationale (cognition off, or the reply discarded): `{"run_id", "turn", "available": false}`. Otherwise:

| Field | Type | Notes |
|---|---|---|
| `available` | boolean | `true`. |
| `speaker`, `utterance`, `rationale`, `goal_served` | string | From the turn's first `agent.response`. |
| `selection_reason` | string or null | From `speaker.selected`. |
| `selection_fallback` | string or null | Set when selection failed and the speaker was drawn at random. |
| `memory_refs` | array | Memory ids surfaced into the prompt. |
| `memories` | array | Those memories resolved from the turn's checkpoint: `{id, content, importance, tags}`. |
| `validation` | object | `{checks: [<validation.checked payloads>], flagged: <validation.flagged payload or null>}`. |

Edge cases:
- In a round where several personas respond at the same turn number, only the first `agent.response` is traced.

### `GET /api/runs/{ref}/turns/{turn}/structured`

A derived Narrative / Consequences / Updated State / Possibilities view of one turn, built from the turn's events and checkpoint. Every line carries the `seq` of the event behind it.

- **Auth:** caller.
- **Success:** `200` with `{turn, narrative, consequences, updated_state, possibilities, run_id}`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `opt_in` | boolean | `false` | Enables the view for this request when `STRUCTURED_OUTPUT` is off. |

| Status | When |
|---|---|
| `403` | `STRUCTURED_OUTPUT` is off and `opt_in` is not true. Checked before the run is looked up. |
| `404` | `Run not found`, or `No turn <turn> in this run`. |

Shape: `narrative.entries[]` (`{speaker, utterance, source_seq}`) and `narrative.note`; `consequences.immediate[]`, `consequences.deferred[]`, `consequences.note`; `updated_state.deltas[]`, `updated_state.note`; `possibilities.open_threads[]`, `possibilities.non_limiting` (boolean), `possibilities.note`.

### `GET /api/runs/{ref}/pending-threads`

The run's pending-thread ledger (setups awaiting payoff) from the latest checkpoint. Distinct from aside threads.

- **Auth:** caller.
- **Success:** `200` with `{run_id, threads, stale_after, as_of_turn}`. With no checkpoint: `{run_id, threads: [], stale_after: null}` (no `as_of_turn`).
- **Errors:** `404 Run not found`.

Each thread: the stored pending thread (`id`, `description`, `thread_type`, `origin_turn`, `origin_agent`, `status`, `resolved_turn`) plus `stale` (open and `as_of_turn - origin_turn >= stale_after`). `stale_after` is `config.cognition.thread_stale_after`, default `5`.

### `GET /api/runs/{ref}/quotes`

Which retrieved passage each persona response quotes verbatim.

- **Auth:** caller and groups.
- **Success:** `200`.
- **Errors:** `404 Run not found`.

| Field | Type | Notes |
|---|---|---|
| `quotes` | object | Keyed by the response event's `seq` (as a string); each value lists the passages quoted, with the quoted words. |
| `messages_with_sources` | integer | Responses that had at least one retrieved passage in view. |
| `messages_quoting` | integer | Responses with at least one verbatim quote. |
| `min_content_words` | integer | The shortest run of content words that counts as a quote (`4`). |

Edge cases:
- Passages are read through the same reach check as the source viewer, so a revoked grant yields no quotes from that collection.
- When a response lists `document_refs`, only those passages are considered.

### `GET /api/runs/{ref}/sources/{document_id}`

The document behind a retrieved passage, so a reader can see what the persona read.

- **Auth:** caller and groups. The document must be reachable from this run: one of its own attachments, or a document in a knowledge base the run binds (run level, any persona, or any consultant) that the caller may read now.
- **Success:** `200`.

| Query | Type | Default | Constraints |
|---|---|---|---|
| `ordinal` | integer | none | `>= 0`. The cited chunk. |

| Response field | Type | Notes |
|---|---|---|
| `document_id`, `title` | string | |
| `scope` | string | `"run"` or `"knowledge_base"`. |
| `kb_id`, `kb_name` | string or null | |
| `owned` | boolean | The caller owns the document (always true for run documents). |
| `origin`, `authority` | string or null | Set on researched documents. |
| `source_url` | string or null | The page a researched document was read from (`http`/`https` only). |
| `cited_ordinal` | integer or null | The `ordinal` query value. |
| `chunk_count` | integer or null | |
| `full` | boolean | `true` when every chunk is returned (the caller owns it). |
| `chunks` | array | `{ordinal, text, display}`; `text` is the chunk as given to the persona, `display` drops the overlap with the previous chunk. |
| `notice` | string | Only when `full` is `false`. |

Edge cases:
- A grantee (`owned: false`) receives only the cited chunk and one chunk either side (`ordinal` omitted means around chunk 0), from the vector index.
- Anything outside the run's reach, and a revoked grant, is `404 Source not found for this run`.

---

## Summaries

### `GET /api/runs/{ref}/summary`

The run's latest summaries.

- **Auth:** caller.
- **Success:** `200`.
- **Errors:** `404 Run not found`.

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | |
| `default_instructions` | string | The default analyst framing, for a "reset to default" control. |
| `generated` | object or null | Latest generated summary: `{id, run_id, kind, payload, tokens_in, tokens_out, cost_usd, instructions, created_at}`. `instructions` is `null` when the default framing was used. |
| `imported` | object or null | Latest imported summary, same shape. |

Summary `payload` fields are in [data-model.md](data-model.md#summary).

### `POST /api/runs/{ref}/summary`

Generate a new summary for a completed run. Each generation is a new version; the previous one is kept.

- **Auth:** caller.
- **Success:** `202` deployed, `200` locally.

| Body field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `fields` | array of strings | no | the run's `config.summary.fields`, else all eight | Unknown names are not removed here; they come back as empty lists and are listed in `payload.omitted`. `concerns` is dropped for a run with no authored underlying concern (or with structured personas off), so its prompt is what it was before the field existed. |
| `focus` | string | no | the run's `config.summary.focus` | |
| `model` | string | no | the run's `models.summary`, then `model` | Branches and imported runs use the settings default instead. |
| `instructions` | string | no | the run's `config.summary.instructions` | Replaces the analyst framing; the guardrails always remain. |

The body may be omitted entirely.

| Status | When |
|---|---|
| `404` | Run not found. |
| `409` | The run's status is not `complete`: `Summary can only be generated for a completed run.` |

Deployed response (`202`): `{run_id, pending: true, default_instructions, generated, imported}`, where `generated` is the summary as it was before this request. The aside worker writes the new one; poll `GET` until `generated.id` changes.

Local response (`200`): `{run_id, generated, default_instructions, imported}` with the new summary.

Edge cases:
- Each generation also re-derives the persona stances and stance basis, and is added to the owner's monthly spend.
- A failed model call still stores a summary whose `overview` says generation was unavailable.
- The analyst is given the run's authored underlying concerns as context in both concern modes (owner decision, 2026-10-02). For a withheld run, `payload.concerns` is therefore the first place they appear, and `payload.concerns_withheld` is `true`.

---

## Asides and threads

Asides are side conversations about a finished transcript with the analyst, one persona, the whole room, or a consultant. They never change the run.

### `GET /api/runs/{ref}/threads`

- **Auth:** caller.
- **Success:** `200` with `{run_id, threads}`, oldest first.
- **Errors:** `404 Run not found`.

Each thread: `{id, thread_id, run_id, target, persona_name, mode, created_at, message_count, total_cost_usd}`, plus the storage keys `pk` and `sk`.

### `POST /api/runs/{ref}/threads`

Open an aside thread.

- **Auth:** caller.
- **Success:** `201` with `{id, run_id, target, persona_name, mode, created_at}`; `mode` is `"aside"`.

| Body field | Type | Required | Notes |
|---|---|---|---|
| `target` | string | yes | `analyst`, `persona`, `room` or `consultant`. |
| `persona_name` | string | for `persona` and `consultant` | Must be a cast member (persona) or one of the run's `config.experts` names (consultant). Ignored for other targets. |

| Status | When |
|---|---|
| `404` | Run not found. |
| `422` | `Unknown target: <t>`; `persona_name must be one of the run's cast: [...]`; `persona_name must be one of the run's consultants: [...]`. |

### `GET /api/threads/{thread_id}`

- **Auth:** caller. The thread's run must be the caller's; otherwise `404 Thread not found`.
- **Success:** `200` with the thread fields, `messages` (oldest first: `{id, thread_id, role, speaker, content, tokens_in, tokens_out, cost_usd, created_at}`, plus `pk` and `sk`), and `total_cost_usd`.

Message `role` is `user`, `target` (the reply) or `error` (a reply the worker could not generate; `speaker` is `system`).

### `POST /api/threads/{thread_id}/messages`

Ask a question in a thread.

- **Auth:** caller.
- **Success:** `202` deployed, `201` locally.

| Body field | Type | Required | Notes |
|---|---|---|---|
| `content` | string | yes | Trimmed; blank is `422 Message content is required`. |
| `model` | string | no | Defaults to the run's `models.aside`, then `model`; branches and imported runs use the settings default. |

Deployed (`202`): the question is stored, the aside worker is invoked, and the response is `{thread_id, pending: true, total_cost_usd}`. Poll `GET /api/threads/{thread_id}` for a `target` or `error` message.

Local (`201`): the reply is generated in the request and both messages are stored after it: `{thread_id, reply, total_cost_usd}`. A `room` reply also carries `reply.replies`, the per-persona breakdown (not stored).

| Status | When |
|---|---|
| `404` | Thread not found, or its run is not the caller's. |
| `409` | Deployed: the last message is an unanswered question less than 240 seconds old (`A reply to the last question is still being written.`). |
| `422` | Blank content. |
| `504` | Local: the reply took longer than 26 seconds; nothing was stored. |

Edge cases:
- The 240-second window is shorter than the aside worker's 15-minute timeout, so a second question is accepted while a slow first reply is still being generated.
- A room reply asks at most 12 personas, the first 12 in the cast.

---

## Run documents and file extraction

Run documents are background material attached to one run, either to one persona or cast-wide. Knowledge bases are the reusable, shareable form.

### `GET /api/runs/{ref}/documents`

- **Auth:** caller.
- **Success:** `200` with `{run_id, persona, documents, total_chars, total_chunks}`.
- **Errors:** `404 Run not found`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `persona` | string | none | Only what this persona can see: its own documents plus cast-wide ones. |

Each document: `{id, run_id, persona_name, title, source_path, media_type, char_count, chunk_count, text_is_original, created_at, kb_id, origin, authority, research_batch}`, newest first. The storage keys (`pk`, `sk`, `s3_key`, `owner_sub`, `document_id`) are removed.

### `POST /api/runs/{ref}/documents`

Attach a document to an existing run, by text or by a server-readable path.

- **Auth:** caller.
- **Success:** `201` with `{run_id, document_id, title, persona_name, media_type, char_count, chunk_count}`.

| Body field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | one of `text` / `path` | Inline content. |
| `path` | string | one of `text` / `path` | A file path readable by the server process. |
| `title` | string | with `text` | Defaults from the file name for `path`. |
| `persona_name` | string | no | Must be a cast member; omitted or `null` makes it cast-wide. |

| Status | When |
|---|---|
| `404` | Run not found. |
| `422` | Both or neither of `text`/`path`; `title` missing with `text`; `persona_name` not in the cast; the file cannot be read or extracted. |

Edge cases:
- A document attached after the run started is not retrievable by turns already generated, and is not embedded until `POST .../documents/embed` runs.
- `path` reads from the server's filesystem. It exists for the local tool.

### `DELETE /api/runs/{ref}/documents/{document_id}`

- **Auth:** caller.
- **Success:** `200` with `{run_id, document_id, deleted: true}`.
- **Errors:** `404 Run not found`; `404 Document not found for this run` (including a document of another run).

### `POST /api/runs/{ref}/documents/reindex`

A legacy recovery route from the SQLite era. There is no derived index to rebuild; the route returns a chunk count.

- **Auth:** caller.
- **Success:** `200` with `{run_id, reindexed_chunks}`.
- **Errors:** `404 Run not found`.

Edge cases:
- `reindexed_chunks` is computed by scanning the whole documents table, so it is the chunk count of every document in the deployment, not of this run. The per-tenant session policy grants no `Scan`, so on the deployed stack this route is expected to fail with a `500`.

### `POST /api/runs/{ref}/documents/embed`

Embed the run's chunks that have no vector yet. Idempotent and resumable.

- **Auth:** caller.
- **Success:** `200` with `{run_id, embedded, skipped, tokens, cost_usd, model, chunks_with_vectors}`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `model` | string | the configured embedding model | A LiteLLM embedding model. |

| Status | When |
|---|---|
| `404` | Run not found. |
| `422` | The embedding provider is unavailable or refused; `detail` is the reason. |

### `GET /api/runs/{ref}/documents/search`

What a lexical (BM25) query retrieves from this run's documents, without running a turn. Read-only.

- **Auth:** caller.
- **Success:** `200`.

| Query | Type | Required | Default | Constraints |
|---|---|---|---|---|
| `q` | string | yes | | At least 1 character. |
| `persona` | string | no | none | Search this persona's slice (own + cast-wide). |
| `k` | integer | no | `5` | `1` to `50`. |
| `max_chars` | integer | no | `2000` | `>= 1`. Character budget over the returned passages. |
| `corpus` | string | no | `run` | `run` or `database`. Both search this run only. |

| Response field | Type | Notes |
|---|---|---|
| `run_id`, `query` | string | |
| `fts_query` | string | The sanitised query, terms joined with `OR`. |
| `terms` | array of strings | |
| `persona`, `corpus` | | Echoed. |
| `passages` | array | `{chunk_id, document_id, title, ordinal, score, chars, content, citation}`. `score` is negative; more negative is a better match. |
| `total_chars` | integer | |
| `matched_before_budget` | integer | Matches before `max_chars` trimmed them. |

When the query has no searchable terms after stopwords: `{run_id, query, fts_query: "", terms: [], passages: [], note: "No searchable terms in the query after removing stopwords."}`.

Edge cases:
- Knowledge-base documents are not searched by this route.

### `GET /api/documents/formats`

Which upload types this install can read, and the size limits.

- **Auth:** none at the app level.
- **Success:** `200` with `{formats, max_upload_bytes, max_document_chars}`.

Each format: `{suffix, media_type, available, needs}`. Suffixes: `.docx`, `.markdown`, `.md`, `.pdf`, `.text`, `.txt`. `media_type` is a short name (`docx`, `md`, `pdf`, `txt`), not a MIME type. `available` is false when the optional extractor package is missing; `needs` names it. `max_upload_bytes` is `MAX_UPLOAD_BYTES`; `max_document_chars` is `MAX_DOCUMENT_CHARS`.

### `POST /api/documents/extract`

Turn an uploaded file into text. Stores nothing.

- **Auth:** none at the app level.
- **Body:** `multipart/form-data` with `file` (required) and `title` (optional form field; default the file's base name).
- **Success:** `200` with `{title, media_type, text, char_count, chunk_count, stored: false}`.

| Status | When |
|---|---|
| `413` | The upload is over `MAX_UPLOAD_BYTES` (checked while streaming), or the extracted text is over `MAX_DOCUMENT_CHARS`. |
| `422` | Unsupported suffix (`Unsupported file type .exe. Supported: ...`); extractor package missing; empty file (`<name> is empty.`); unreadable content, including a PDF with no text layer. |

```bash
curl -F file=@opening-hours.md -F title="Opening hours" http://127.0.0.1:8000/api/documents/extract
```

---

## Knowledge bases, documents and grants

A knowledge base (KB) is a reusable collection of documents with its own vector index. Read access: the owner, or a user or group with a grant. Write access: the owner only. Both failures are `404 Knowledge base not found`.

### `GET /api/knowledge-bases`

The caller's own KBs and every KB granted to the caller or one of their groups.

- **Auth:** caller and groups.
- **Success:** `200` with `{knowledge_bases, count}`. Owned first, then shared; newest first within each.

Each entry: the stored KB row (`id`, `name`, `description`, `owner_sub`, `embedding_model`, `created_at`, `research_for`, and the storage keys `pk`, `sk`), plus `shared` (true when the caller is not the owner) and `document_count` (counted at request time).

### `POST /api/knowledge-bases`

- **Auth:** caller (becomes the owner).
- **Success:** `201` with `{id, name, description, owner_sub, embedding_model: null, created_at, research_for: null, document_count: 0, shared: false}`. The KB's vector index is created at the same time.

| Body field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Trimmed; blank is `422 A name is required`. Not unique. |
| `description` | string | no | |

`id` is a 12-character hex string.

### `GET /api/knowledge-bases/{kb_id}`

- **Auth:** read access.
- **Success:** `200` with the KB row, `shared`, `documents` (the stored document rows, including `s3_key` and `owner_sub`), `document_count`, and `grants` (the owner sees the grant list; a grantee sees `null`).
- **Errors:** `404 Knowledge base not found`.

### `POST /api/knowledge-bases/{kb_id}/documents`

Add a document and embed it in the same request.

- **Auth:** owner.
- **Success:** `201` with `{document_id, kb_id, embedded, cost_usd, model}`.

| Body field | Type | Required | Notes |
|---|---|---|---|
| `title` | string | yes | Trimmed; blank becomes `"untitled"`. |
| `text` | string | yes | Trimmed; blank is `422`; over `MAX_DOCUMENT_CHARS` is `422`. A file becomes text through `POST /api/documents/extract` first. |

| Status | When |
|---|---|
| `404` | KB not found or not owned. |
| `422` | `Document text is required`; `Document is <n> characters; the limit is <max>.` |
| `502` | The document was stored but could not be embedded: `The document was stored as <id> but could not be embedded...`. The document stays; delete it explicitly if unwanted. |

### `DELETE /api/knowledge-bases/{kb_id}/documents/{document_id}`

Removes the document's metadata, vectors and stored text.

- **Auth:** owner.
- **Success:** `200` with `{"deleted": "<document-id>"}`.
- **Errors:** `404 Knowledge base not found`; `404 Document not found`.

### `GET /api/knowledge-bases/{kb_id}/grants`

- **Auth:** owner.
- **Success:** `200` with `{grants: [{kb_id, principal, kind, granted_by, created_at, pk, sk}]}`. `kind` is `user` or `group`.

### `POST /api/knowledge-bases/{kb_id}/grants`

Grant read access to one user (by `sub`) or one Cognito group.

- **Auth:** owner.
- **Success:** `201` with `{kb_id, principal, kind, granted_by, created_at}`.

| Body field | Type | Required | Notes |
|---|---|---|---|
| `user` | string | exactly one of `user` / `group` | A Cognito `sub`. Not checked for existence. |
| `group` | string | exactly one of `user` / `group` | A Cognito group name. Not checked for existence. |

| Status | When |
|---|---|
| `404` | KB not found or not owned. |
| `422` | Both or neither supplied: ``Supply exactly one of `user` or `group`.`` |

Edge cases:
- Granting the same principal again overwrites the grant (new `created_at`).

### `DELETE /api/knowledge-bases/{kb_id}/grants`

Revoke a grant. Takes effect at the next retrieval query, including in runs already bound to the KB.

- **Auth:** owner.
- **Success:** `200` with `{"revoked": "<principal>"}`.

| Query | Type | Notes |
|---|---|---|
| `user` | string | Exactly one of `user` / `group`. |
| `group` | string | Exactly one of `user` / `group`. |

| Status | When |
|---|---|
| `404` | KB not found or not owned. |
| `422` | Both or neither supplied. |

Edge cases:
- Revoking a grant that does not exist is still `200`.

---

## Ensembles

An ensemble runs one brief several times, optionally in labelled cells that vary a permitted setting, and aggregates the results into a report.

### `POST /api/ensembles`

- **Auth:** caller and groups.
- **Success:** `201`.

| Body field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `topic`, `cast`, `config`, `model`, `name`, `description`, `summary` | | as `POST /api/runs` | | The base every member starts from. |
| `cells` | array | no | one cell `{"label": "base", "n": 5}` | An empty list also means the default. |
| `cells[].label` | string | yes | | Non-blank, no leading or trailing space, unique. |
| `cells[].n` | integer | no | `5` | At least `2`. `0` is read as the default `5`. |
| `cells[].overrides` | object | no | `{}` | Dotted config paths. Permitted: `selection.method`, `selection.hybrid_opening_rounds`, `injections`, `assumptions`. |

All cells together may have at most 12 members. The same preflight as `POST /api/runs` runs once (cast, KB bindings, cost cap).

| Response field | Type | Notes |
|---|---|---|
| `ensemble_id` | string (UUID) | |
| `name`, `description`, `topic` | string | Name is supplied (lowercased) or generated; not checked against run names. |
| `status` | string | `running`, `failed` (no member started) or `researching` (deployed with research on; members are created after the pass). |
| `spec` | array | The cells as stored: `{label, n, overrides}`. |
| `members` | array | Members started: `{run_id, cell, index, name}`. Empty while researching. Member names are `<ensemble-name>-<cell><index>`. |
| `failed` | array | Members that could not be created: `{run_id, cell, index, error}`. |
| `renamed` | array | Real public figures' names this request carried, and what each became: `{from, to, reason, source, role}` (see [Conventions](#conventions)). Empty when none. Checked once, for the parent; every member carries the same names. |

| Status | When |
|---|---|
| `402` | Over the monthly cap. |
| `422` | Any create-run validation error; an override that is refused (`max_messages`, `selection.fairness`, `selection.stop_when_converged`, `models.voice`, anything under `personas`) or not permitted, with the reason; fewer than 2 runs in a cell; more than 12 members; duplicate or blank labels; a cell injection whose `speaker` is a cast member. Unknown keys inside a cell are dropped, not refused. |

```json
{"topic": "Should the town library open on Sundays?",
 "cast": [{"name": "Ada", "persona": "A librarian"}, {"name": "Ben", "persona": "A parent"}],
 "config": {"max_messages": 12},
 "cells": [{"label": "moderated", "n": 3},
           {"label": "blind", "n": 3, "overrides": {"selection.method": "simultaneous"}}]}
```

Edge cases:
- Members are started about one second apart (`ENSEMBLE_STAGGER_SECONDS`, default `1.0`), inside the request, so the request takes about one second per member. With research on (deployed), members are created by the research worker after the pass instead.
- A member that fails to create is reported and the others continue.

### `GET /api/ensembles`

- **Auth:** caller.
- **Success:** `200` with `{ensembles: [<ensemble summary>, ...]}`, newest first, at most 100.

Ensemble summary fields:

| Field | Type | Notes |
|---|---|---|
| `ensemble_id`, `name`, `description`, `slug`, `topic` | string | |
| `status` | string | `pending`, `researching`, `running`, `failed`, `complete` (set when a report is stored). |
| `created_at`, `completed_at` | integer or null | |
| `spec` | array | The cells. |
| `base_config` | object | |
| `has_report` | boolean | |
| `report` | object or null | The stored report (the list returns it too). |
| `report_generated_at`, `report_cost_usd` | integer / number or null | |
| `report_error` | string or null | Why the last attempt produced no report. |
| `report_claimed_at` | integer or null | When a builder last took the 20-minute report lease. |

### `GET /api/ensembles/{ensemble_id}`

- **Auth:** caller.
- **Success:** `200` with the ensemble summary plus:

| Field | Type | Notes |
|---|---|---|
| `members` | array | In declared order: `{run_id, cell, index, run}`; `run` is a run summary, or `null` when that member run does not exist. |
| `cells` | array | Per cell, sorted by label: `{cell, declared, complete, settled}`. A missing member counts as settled. |
| `report_ready` | boolean | Every declared member is settled. |
| `research` | object or null | The ensemble's single research record. |

- **Errors:** `404 Ensemble not found`.

Edge cases:
- While an ensemble is `researching`, no member runs exist yet, so every member counts as settled and `report_ready` is `true`.

### `POST /api/ensembles/{ensemble_id}/report`

Ask for the report to be built. Normally unnecessary: the last member to finish triggers it.

- **Auth:** caller.

| Query | Type | Default | Notes |
|---|---|---|---|
| `force` | boolean | `false` | Ignore an existing lease and rebuild; pays for the full extraction and synthesis again. |

| Status | When |
|---|---|
| `202` | Deployed: dispatched to the report worker. Body `{ensemble_id, accepted: true, detail}`. Poll `GET` for `report` or `report_error`. |
| `200` | Local: the report was built (body is the ensemble detail), or another caller holds the lease (body `{ensemble_id, claimed_by_another: true, detail: "A report for this ensemble is already being generated."}`). |
| `404` | Ensemble not found. |
| `409` | Local: the build was refused; `detail` is the stored `report_error` (a member still running, too few usable runs). |

Edge cases:
- The lease is not released when a report finishes. For 20 minutes after a build, a request without `force` reports `claimed_by_another` even though the report is done; after that, a request without `force` rebuilds and pays again.
- Deployed, the `202` is returned whether or not the worker will win the lease.

---

## Avatars

### `POST /api/runs/{ref}/agents/{name}/regenerate-avatar`

Generate a new portrait for one persona (random seed), store it on the latest checkpoint, and append an `avatar.ready` event.

- **Auth:** caller.
- **Body:** none.
- **Success:** `200` with `{run_id, agent, portrait_key}`.

| Status | When |
|---|---|
| `404` | `Run not found`; `Agent not found for this run`. |
| `502` | Image generation unavailable or filtered: `Avatar generation is unavailable or was filtered; try again.` |

Edge cases:
- The appended `avatar.ready` event has `turn: 0`, the next free `seq`, and no `cost_usd`, so the image is not counted in the run's cost or the owner's monthly spend.
- The `seq` is computed from the stored log at request time. Event writes are conditional on the `seq` being free, so on a run that is still generating, whichever of this route and the engine writes that `seq` second fails (a `500` here, or a failed turn in the engine).

### `GET /api/runs/{ref}/agents/{name}/avatar`

The persona's portrait image.

- **Auth:** caller.
- **Success:** `200`, `image/png`, `Cache-Control: public, max-age=31536000, immutable`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `v` | string | the latest checkpoint's `portrait_key` for this persona | The content-addressed image key. |

| Status | When |
|---|---|
| `404` | `Run not found`; `No avatar for this agent`; `Avatar image not found`. |

Edge cases:
- `v` is taken as given: any valid key can be fetched through any run the caller owns. Keys are SHA-256 content hashes.

---

## Exports and briefs

All four are downloads (`Content-Disposition: attachment`), read through the caller's partition, and make no model call.

Every persona and consultant name in a run's export and brief is written `(bot) <name>`: the cast headings, each transcript speaker, and the summary fields that are a name (a dissenter's `speaker`, an evidence row's `asked_by`, a key idea's `proposed_by`) when they name somebody in the cast. An operator's injected message is not marked, and the analyst's prose is never rewritten (`matrix_studio/persona_label.py`). The stored names are unchanged.

### `GET /api/runs/{ref}/export`

The conversation as a document: settings, cast (without private persona fields), transcript, summary and itemised cost.

- **Auth:** caller.

| Query | Type | Default | Constraints |
|---|---|---|---|
| `format` | string | `md` | `md` (`text/markdown; charset=utf-8`) or `html` (`text/html; charset=utf-8`, self-contained). |

- **Success:** `200`, file name `<slug>-run.<format>`.
- **Errors:** `404 Run not found`; `422 format must be one of ['html', 'md']; ...`.

### `GET /api/ensembles/{ensemble_id}/export`

The ensemble's cells, members, per-cell claims and stored report.

- **Auth:** caller.
- **Query:** `format`, as above (default `md`).
- **Success:** `200`, file name `<slug>-ensemble.<format>`.
- **Errors:** `404 Ensemble not found`; `422` bad format.

### `GET /api/runs/{ref}/brief`

A one-page decision brief built from the same stored data as the export.

- **Auth:** caller.

| Query | Type | Default | Constraints |
|---|---|---|---|
| `format` | string | `html` | `md` or `html`. |

- **Success:** `200`, file name `<slug>-run-brief.<format>`.
- **Errors:** `404 Run not found`; `422 format must be md or html`.

### `GET /api/ensembles/{ensemble_id}/brief`

A one-page brief from the ensemble's stored report.

- **Auth:** caller.
- **Query:** `format`, default `html`, `md` or `html`.
- **Success:** `200`, file name `<slug>-ensemble-brief.<format>`.
- **Errors:** `404 Ensemble not found`; `422 format must be md or html`.
