# CLI and scripts

The `matrix-studio` command (`matrix_studio/__main__.py`, the only console script in `pyproject.toml`) and every script in `scripts/`. For each: what it does, its arguments as argparse defines them, what it reads and writes, and how safe it is to run. Every argparse `--help` listed here was run locally; no script was run for real.

See also: [explanation](../explanation/) (what the measurements and verifications were for), [how-to guides](../how-to/) (task recipes that use these commands), [settings.md](settings.md) (the environment variables they read), [http-api.md](http-api.md), [run-config.md](run-config.md), [events.md](events.md), [data-model.md](data-model.md).

## Conventions

**Safety labels**, used in every entry:

| Label | Meaning |
|---|---|
| read-only (local) | Reads local files only. |
| read-only (AWS) | Reads AWS resources; writes nothing anywhere except standard output. |
| writes local | Writes local files. |
| writes AWS | Creates or changes DynamoDB items, S3 objects, S3 Vectors, Cognito users or Step Functions executions. |
| destructive | Deletes or moves data in AWS that is not its own fixture. |
| paid model calls | Calls Bedrock (or another LiteLLM provider). |
| paid search calls | Calls a web-search provider. |

**Storage.** `Database` is DynamoDB, S3 and S3 Vectors (`matrix_studio/storage/`). There is no local database: anything that "persists" writes to the tables named by `TABLE_PREFIX` in `AWS_REGION`, with the ambient AWS credentials. Unless a script says otherwise it reads `TABLE_PREFIX` (default `matrix-studio`), `DATA_BUCKET`, `AWS_REGION` and, for vectors, `VECTOR_BUCKET` and `VECTOR_INDEX`. Where a script indexes `os.environ[...]` directly, a missing variable is a `KeyError`.

**Owners.** `--owner` is a Cognito `sub` (placeholder `<owner-sub>` below). Scripts that take one bind the store to that owner but use the ambient credentials, so they are not limited by the API's tenancy boundary. The CLI acts as the single local owner, `local-single-user`.

**Working directory.** Scripts add the repository root to `sys.path` and are run from the repository root. Paths such as `private/...` and `docs/labels/...` are relative to the working directory.

**Other variables read by scripts:** `AWS_PROFILE` (through boto3), `AWS_DEFAULT_REGION` (`verify_tenant_isolation.py`, `verify_vector_retrieval.py`), `TURN_LOOP_ARN`, `USER_POOL_ID` (`start_conversation.py`), the search-provider keys (`research_definition.py`) and `VERIFY_DIM` (set internally by `verify_vector_retrieval.py` from the index it finds).

## Summary

| Command | Purpose | Safety | `--help` run |
|---|---|---|---|
| `matrix-studio run` | Run one conversation from a request file | paid model calls; writes AWS unless `--no-db` | yes |
| `matrix-studio serve` | Start the API and SPA server | depends on requests served | yes |
| `matrix-studio docs ... attach` | Attach a file to a run | writes AWS | yes |
| `matrix-studio docs ... list` | List a run's documents | read-only (AWS) | yes |
| `matrix-studio docs ... search` | Inspect lexical retrieval | read-only (AWS) | yes |
| `matrix-studio docs ... reindex` | Report the chunk count | read-only (AWS) | yes |
| `matrix-studio docs ... embed` | Embed a run's chunks | writes AWS; paid model calls | yes |
| `check_private_terms.py` | Block private terms from commits | read-only (local); `--install` writes local | yes (prints its docstring) |
| `start_conversation.py` | Start a definition on the deployed stack | writes AWS; paid model calls | yes |
| `load_kb_documents.py` | One knowledge base per file | writes AWS; paid model calls | yes |
| `clone_curated_documents.py` | Copy non-researched documents to new KBs | writes AWS; paid model calls; writes local | yes |
| `delete_runs.py` | Delete runs across all stores | destructive | yes |
| `rehome_owner.py` | Move items between owner partitions | destructive | yes |
| `migrate_documents_to_kbs.py` | Copy run documents into per-KB indexes | writes AWS | yes |
| `import_thematrix_run.py` | Import a legacy result file | writes AWS; paid model calls | not run: no argparse |
| `verify_deployment.py` | Run every deployment check | writes AWS; paid model calls | yes |
| `verify_bedrock_logging.py` | Check Bedrock invocation logging | read-only (AWS) | yes |
| `verify_kb_grants.py` | Check KB grants and revocation | writes AWS | yes |
| `verify_kb_fanout_equivalence.py` | Compare KB fan-out to one index | writes AWS | yes |
| `verify_kb_retrieval.py` | KB retrieval end to end over HTTP | writes AWS; paid model calls | yes |
| `verify_tenant_isolation.py` | Check scoped credentials refuse cross-tenant access | writes AWS | yes |
| `verify_turn_loop.py` | Check the deployed turn loop | writes AWS; paid model calls | yes |
| `verify_vector_retrieval.py` | Check S3 Vectors k-NN and filter | writes AWS | yes |
| `analyse_cite_inline.py` | Score the cite-inline comparison | read-only (AWS) | yes |
| `analyse_evidence_lean.py` | Score the evidence-lean comparison | read-only (AWS); paid model calls; writes local | yes |
| `analyse_moderator_assumptions.py` | Score the moderator-assumptions comparison | read-only (AWS); paid model calls with `--labels`; writes local | yes |
| `analyse_section9.py` | Score the research comparison | read-only (AWS) | yes |
| `measure_evidence_plan.py` | Count "not stated" evidence-plan cells | read-only (AWS); paid model calls; writes local | yes |
| `measure_retrieval_recall.py` | Measure retrieval recall on a corpus | writes AWS; paid model calls; writes local | yes |
| `eval_speaker_selection.py` | Score selection arms by replay | read-only (AWS); paid model calls | yes |
| `ensemble_report.py` | Collate several runs of one brief | read-only (AWS); paid model calls; writes local | yes |
| `tune_clustering.py` | Measure claim-clustering recall | read-only (AWS); paid model calls; writes local | yes |
| `shadow_assumption_checks.py` | Re-run the assumption check on stored runs | read-only (AWS); paid model calls; writes local | yes |
| `folding_packets.py` | Build anonymous judging packets | read-only (AWS); writes local | yes |
| `research_definition.py` | Research a definition and print the corpus | paid search calls; paid model calls; writes local with `--write` | yes |
| `build_validation_arms.py` | Generate the validation arm files | writes local | not run: no argparse |
| `score_validation.py` | Score validation arm results | read-only (local); paid model calls with `--judge`; writes local | yes |
| `check_concern_leak.py` | Pre-filter withheld-concern leaks (withheld runs only) | read-only (local) | yes |
| `judge_variance.py` | Measure the judge's spread | paid model calls; writes local | yes |

## The `matrix-studio` command

Installed as the `matrix-studio` console script (`matrix_studio.__main__:main`); `python -m matrix_studio` is the same. With no subcommand it prints help and exits 1.

| Option | Type | Default | Notes |
|---|---|---|---|
| `--version` | flag | | Prints `matrix-sim-studio <version>` (from package metadata; `0.6.0` when not installed). |
| `-h`, `--help` | flag | | |

### matrix-studio run

**Purpose.** Run one conversation in-process from a request file and print the result. The engine runs directly; no state machine is involved.

```
matrix-studio run [-h] [-o OUTPUT] [--max-messages MAX_MESSAGES] [--no-db] [-v] request
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `request` | path | required | JSON request: `topic`, `cast`, optional `config`, as for `POST /api/runs`. Missing file: exit 1. A `config.personas` block with no `withhold_concerns` gets the new-run value, `false`, written into the stored config (since 2026-10-02); a file that sets it keeps it. |
| `-o`, `--output` | path | stdout | Where the result JSON is written. |
| `--max-messages` | int | from the request | Sets `config.max_messages`. |
| `--no-db` | flag | off | Do not persist the run. |
| `-v`, `--verbose` | flag | off | DEBUG logging. |

**Reads.** The request file; settings (`settings.md`); storage environment unless `--no-db`.
**Writes.** The result JSON to `--output` or stdout. Without `--no-db`, the run (row, events, snapshots) to DynamoDB and S3 as owner `local-single-user`.
**Safety.** Paid model calls. Writes AWS unless `--no-db`. Exit 0 when the run completes, 1 otherwise, 130 on Ctrl-C.
**Example.**

```bash
matrix-studio run examples/<request>.json -o /tmp/result.json --max-messages 6 --no-db
```

### matrix-studio serve

**Purpose.** Start the FastAPI app with uvicorn: the API under `/api` and the built SPA, if present.

```
matrix-studio serve [-h] [--host HOST] [--port PORT] [-v]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--host` | str | `MATRIX_HOST` (`127.0.0.1`) | |
| `--port` | int | `MATRIX_PORT` (`8000`) | |
| `-v`, `--verbose` | flag | off | DEBUG logging. |

**Reads.** All settings and the storage environment.
**Writes.** Whatever the served requests write. At startup, with `STARTUP_SWEEP=true` (the default), every run still `running` is marked `interrupted`.
**Safety.** Depends on the requests served. With `AUTH_MODE=single-user` (the default) every request acts as `local-single-user`. Exit 130 on Ctrl-C.
**Example.**

```bash
TABLE_PREFIX=demo-studio DATA_BUCKET=<data-bucket> matrix-studio serve --port 8001
```

### matrix-studio docs

**Purpose.** Manage one run's documents directly in storage, without a server. Acts as `local-single-user`; a run owned by anyone else is "not found" (exit 1).

```
matrix-studio docs [-h] [-v] run {attach,list,search,reindex,embed} ...
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `run` | str | required | Run id or name. Comes before the action. |
| `-v`, `--verbose` | flag | off | DEBUG logging. |

Exit codes for every action: 0 success, 1 error or run not found, 2 unknown action, 130 on Ctrl-C.

#### docs attach

**Purpose.** Ingest a file and attach it to the run, for one persona or the whole cast.

```
matrix-studio docs <run> attach [-h] [-p PERSONA] [-t TITLE] path
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `path` | str | required | A `.pdf`, `.docx`, `.txt` or `.md` file. PDF and Word need the `documents` extra. |
| `-p`, `--persona` | str | whole cast | Persona that may retrieve it. Not checked against the cast. |
| `-t`, `--title` | str | from the file | |

**Reads.** The file. **Writes.** A document row, its chunks and its text body. **Safety.** Writes AWS. The stored body is rebuilt from the chunks (the original text is not passed), unlike `POST /api/runs/{ref}/documents`.
**Example.** `matrix-studio docs quiet-harbor attach notes.md -p "Example Persona"`

#### docs list

**Purpose.** Print the run's documents: id, persona (`(all)` for cast-wide), chunks, characters, title.

```
matrix-studio docs <run> list [-h] [-p PERSONA]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `-p`, `--persona` | str | all | Restrict to that persona's own and the cast-wide documents. |

**Reads.** The run's document rows. **Writes.** Nothing. **Safety.** read-only (AWS).
**Example.** `matrix-studio docs quiet-harbor list`

#### docs search

**Purpose.** Show what a lexical query retrieves: the extracted terms, the sanitised query, and each passage with its citation and score.

```
matrix-studio docs <run> search [-h] [-p PERSONA] [-k K] [--max-chars MAX_CHARS] query [query ...]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `query` | str, one or more | required | Joined with spaces. A query with only stopwords prints a note and exits 0. |
| `-p`, `--persona` | str | all | Search one persona's slice. |
| `-k` | int | `5` | Maximum passages. |
| `--max-chars` | int | `2000` | Ceiling on returned characters. |

**Reads.** The run's chunks. **Writes.** Nothing. **Safety.** read-only (AWS).
**Example.** `matrix-studio docs quiet-harbor search renewal terms -k 3`

#### docs reindex

**Purpose.** Print a chunk count. Despite the help text ("Rebuild the lexical index from doc_chunks"), there is no index to rebuild: `reindex_documents` is a no-op that returns the total `chunk_count` of every document row it scans in the documents table, not only this run's.

```
matrix-studio docs <run> reindex [-h]
```

No further arguments. **Reads.** A scan of the documents table. **Writes.** Nothing. **Safety.** read-only (AWS).
**Example.** `matrix-studio docs quiet-harbor reindex`

#### docs embed

**Purpose.** Embed the run's chunks that have no vector yet, and print the count, tokens and cost.

```
matrix-studio docs <run> embed [-h] [-m MODEL]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `-m`, `--model` | str | `bedrock/amazon.titan-embed-text-v2:0` | LiteLLM embedding model. |

**Reads.** The run's chunks. **Writes.** Vectors to `VECTOR_BUCKET` / `VECTOR_INDEX`. **Safety.** Writes AWS; paid model calls (embeddings). Idempotent: chunks already embedded are skipped. The help text says it "needs the 'vectors' extra"; that extra no longer exists and nothing extra is needed.
**Example.** `matrix-studio docs quiet-harbor embed`

## Repository hygiene

### check_private_terms.py

**Purpose.** Refuse a commit that adds a line matching a private term. The patterns are read from `private/sensitive-terms.txt` (gitignored; one case-insensitive regex per line, `#` comments). Without that file it prints a note and exits 0. Matches are reported by pattern number, never by text.

```
scripts/check_private_terms.py --staged | --message FILE | --all | --install
```

No argparse; the first recognised flag is used. Any other argument (including `--help`) prints the docstring and exits 2, when the patterns file exists.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--staged` | flag | | Scan lines added in the index (`git diff --cached`). |
| `--message FILE` | path | | Scan a commit message file. |
| `--all` | flag | | Scan every tracked file. |
| `--install` | flag | | Write `pre-commit` (`--staged`) and `commit-msg` (`--message "$1"`) hooks into the repository's hooks directory. |

**Reads.** The patterns file; git. **Writes.** Nothing, except hook files with `--install`. **Safety.** read-only (local); `--install` writes local. Exit 1 when any line matches.
**Example.** `scripts/check_private_terms.py --staged`

## Deployment operations

### start_conversation.py

**Purpose.** Start a conversation definition (a create-run body) on the deployed stack, and watch it. Goes through `RunManager.create_run` / `create_ensemble`, the same path as the API route, without HTTP or a JWT.

```
start_conversation.py [-h] [--owner OWNER] [--groups GROUPS] [--max-messages MAX_MESSAGES]
                      [--name NAME] [--ensemble N] [--hybrid N] [--allow-unknown-owner]
                      [--dry-run] [--watch RUN_ID] [--no-watch] [--timeout TIMEOUT] [definition]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `definition` | path | | Definition JSON. Required unless `--watch`. |
| `--owner` | str | required | Cognito `sub` that will own the run. Required even with `--watch`. |
| `--groups` | str, repeatable | `[]` | Cognito groups recorded on the run (used by KB grants). |
| `--max-messages` | int | from the definition | Sets `config.max_messages`. |
| `--name` | str | from the definition | Overrides `name`; for an ensemble, the stem of every member's name. |
| `--ensemble` | int `N` | | Run N times as an ensemble (one cell, `base`). |
| `--hybrid` | int `N` | | With `--ensemble`: a second cell of N runs with `selection.method=hybrid`, `selection.hybrid_opening_rounds=2`. |
| `--allow-unknown-owner` | flag | off | Start even when `--owner` is not a user in `USER_POOL_ID` (or, with no pool configured, owns no runs). |
| `--dry-run` | flag | off | Validate and print the plan; create nothing. With research on, it also reads the account to print the research targets. |
| `--watch` | str `RUN_ID` | | Attach to a running run instead of starting one. |
| `--no-watch` | flag | off | Exit after starting. |
| `--timeout` | int, seconds | `3600` | Watch timeout. |

**Reads.** The definition; `AWS_REGION`, `DATA_BUCKET`, `TURN_LOOP_ARN` (all required unless a dry run without research); `USER_POOL_ID` (Cognito `AdminGetUser`, optional); the owner's runs.
**Writes.** Run (or ensemble) rows and Step Functions executions.
**Safety.** Writes AWS; paid model calls (every turn).
**Example.**

```bash
scripts/start_conversation.py examples/<definition>.json --owner <owner-sub> --max-messages 4 --dry-run
```

### load_kb_documents.py

**Purpose.** Create one knowledge base per `.md` / `.txt` file in a directory, store and embed each file, and print the ids. Refuses when a KB with the same name already exists.

```
load_kb_documents.py [-h] --owner OWNER [--prefix PREFIX] [--exclude EXCLUDE] [--dry-run] directory
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `directory` | path | required | `.md` and `.txt` files, one KB each. No matching files: exit with an error. |
| `--owner` | str | required | Cognito `sub` that will own the KBs. |
| `--prefix` | str | `""` | Prepended to each KB name. |
| `--exclude` | str, repeatable | `["README"]` | File stems to skip. Values are added to the default, so `README` is always skipped. |
| `--dry-run` | flag | off | List the files; create nothing. |

**Reads.** The files; `AWS_REGION`, `DATA_BUCKET`, `VECTOR_BUCKET` (required, also for `--dry-run`). **Writes.** KB rows, per-KB vector indexes, documents, vectors. **Safety.** Writes AWS; paid model calls (embeddings).
**Example.** `scripts/load_kb_documents.py /tmp/kb-files --owner <owner-sub> --prefix demo --dry-run`

### clone_curated_documents.py

**Purpose.** For every collection a definition binds, create a new collection holding only the documents whose `origin` is not `researched` (including documents with no `origin`). Never deletes.

```
clone_curated_documents.py [-h] --owner OWNER --definition DEFINITION [--suffix SUFFIX] [--dry-run] [--write PATH]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--definition` | path | required | A definition that binds at least one KB. |
| `--suffix` | str | `-baseline` | Appended to each new collection's name. |
| `--dry-run` | flag | off | Print the manifest; create nothing. |
| `--write` | path | | Write a copy of the definition rebound to the new collections. |

**Reads.** The definition; the bound KBs and documents. **Writes.** New KBs, documents, vectors; the `--write` file. **Safety.** Writes AWS; paid model calls (embeddings, cents); writes local with `--write`.
**Example.** `scripts/clone_curated_documents.py --owner <owner-sub> --definition /tmp/def.json --dry-run`

### delete_runs.py

**Purpose.** Delete runs and everything they own: run row and name marker, events, snapshots, summaries, threads, documents, their S3 bodies and their vectors. Dry run by default.

```
delete_runs.py [-h] [--owner OWNER] [--all-owners] [--except-owner EXCEPT_OWNER] [--apply]
               [--region REGION] [--table-prefix TABLE_PREFIX]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str, repeatable | | Delete this owner's runs. One of `--owner` or `--all-owners` is required. |
| `--all-owners` | flag | off | Every owner's runs. |
| `--except-owner` | str, repeatable | `[]` | Owners to leave alone. |
| `--apply` | flag | off | Delete. Without it, report what would be deleted. |
| `--region` | str | `us-east-1` | |
| `--table-prefix` | str | `TABLE_PREFIX`, else `matrix-studio` | |

**Reads.** A scan of the runs table; `DATA_BUCKET` (required); `VECTOR_BUCKET`. **Writes.** Deletions, with `--apply`. **Safety.** Destructive and irreversible with `--apply`; read-only (AWS) without. Exit 1 when nothing matches.
**Example.** `python scripts/delete_runs.py --owner <owner-sub>`

### rehome_owner.py

**Purpose.** Move items from one owner's partition to another's in the `runs`, `events` and `snapshots` tables: each item is written under the new key, then deleted from the old. `NAME#` markers are rewritten (a collision is fatal); `SPEND#` items are reported and not moved. Run-keyed items (summaries, threads, documents) need no move.

```
rehome_owner.py [-h] --from SRC --to DST [--ensemble ENSEMBLE] [--apply]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--from` | str | required | The `sub` to move from. Equal to `--to`: exits. |
| `--to` | str | required | The `sub` to move to. |
| `--ensemble` | str, repeatable | `[]` | Move only these ensembles and their member runs. Without it, the whole partition moves. |
| `--apply` | flag | off | Write and delete. Without it, report only. |

**Reads.** `AWS_REGION` (default `us-east-1`), `TABLE_PREFIX`; the source partition. **Writes.** With `--apply`, new items and deletions. **Safety.** Destructive with `--apply`; read-only (AWS) without.
**Example.** `scripts/rehome_owner.py --from <old-sub> --to <new-sub> --ensemble <ensemble-id>`

### migrate_documents_to_kbs.py

**Purpose.** Copy existing run documents into knowledge bases: one KB per (run, persona) for persona documents and one per run for cast-wide documents, copying vectors rather than re-embedding. Does not delete the shared index. Dry run by default.

```
migrate_documents_to_kbs.py [-h] [--apply] [--bind] [--table-prefix TABLE_PREFIX] [--region REGION]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--apply` | flag | off | Write. Without it, report only. |
| `--bind` | flag | off | Also add each new KB to its run's bindings. |
| `--table-prefix` | str | `TABLE_PREFIX`, else `matrix-studio` | |
| `--region` | str | `us-east-1` | |

**Reads.** A scan of the documents table; `DATA_BUCKET`; `VECTOR_BUCKET` (required for `--apply`). **Writes.** With `--apply`: KBs, indexes, documents, vectors; with `--bind`, run configs. **Safety.** Writes AWS with `--apply`; read-only (AWS) without. No Bedrock calls.
**Example.** `python scripts/migrate_documents_to_kbs.py`

### import_thematrix_run.py

**Purpose.** Import a legacy TheMatrix request and result into storage as a completed run (row, events, completion snapshot, and the legacy summary as an `imported` summary), owned by `local-single-user`. Costs are recorded as 0.0. The docstring says "SQLite store"; storage is DynamoDB and S3.

```
python scripts/import_thematrix_run.py <request.json> <result.json>
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| first positional | path | required | Legacy request (`personas` or `cast`). |
| second positional | path | required | Legacy result (`conversation`, `metadata`, `summary`). |

No argparse: arguments are read from `sys.argv`. **Reads.** Both files; storage environment. **Writes.** A run in AWS. **Safety.** Writes AWS; paid model calls (one codename call). Not run: no `--help`.
**Example.** `python scripts/import_thematrix_run.py /tmp/legacy-request.json /tmp/legacy-result.json`

## Verification

All verification scripts exit non-zero when a check fails or cannot be performed.

### verify_deployment.py

**Purpose.** Run the deployment checks below as subprocesses and print one scoreboard (PASS, FAIL, NEEDS-SETUP, ERROR).

```
verify_deployment.py [-h] [--only ONLY] [--timeout TIMEOUT]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--only` | str, repeatable | all | Check names: `bedrock-logging`, `kb-grants`, `vector-retrieval`, `tenant-isolation`, `turn-loop`, `kb-retrieval`. |
| `--timeout` | int, seconds | `1800` | Per check. |

**Reads.** `TABLE_PREFIX`, `DATA_BUCKET`, `VECTOR_BUCKET` (all required; exit 2 when missing). **Writes.** What the checks write. `tenant-isolation` runs with `--stack matrix-studio-stack`; `turn-loop` with `--turns 6`. **Safety.** Writes AWS; paid model calls (about $0.10 for the full set).
**Example.** `python scripts/verify_deployment.py --only kb-grants`

### verify_bedrock_logging.py

**Purpose.** Check that Bedrock model-invocation logging is enabled in `us-east-1` (text models) and `us-west-2` (avatar model), its log-group retention (reported when under 30 days), and the delivery role's attached policies.

```
verify_bedrock_logging.py [-h]
```

No arguments. **Reads.** Bedrock logging configuration, IAM role policies, CloudWatch Logs. **Writes.** Nothing. **Safety.** read-only (AWS).
**Example.** `AWS_PROFILE=<profile> python scripts/verify_bedrock_logging.py`

### verify_kb_grants.py

**Purpose.** Against real DynamoDB and S3 Vectors: a document in one KB is searchable by two personas in two runs, a binding is not permission, and a revoked grant stops resolving immediately. Uses fixed `verify-kb-` owners.

```
verify_kb_grants.py [-h] [--region REGION] [--table-prefix TABLE_PREFIX]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--region` | str | `us-east-1` | |
| `--table-prefix` | str | `TABLE_PREFIX`, else `matrix-studio` | |

**Reads.** `VECTOR_BUCKET`, `DATA_BUCKET` (required). **Writes.** One reused KB with one vector, runs and grants; at the end it deletes its runs and revokes its grants, and leaves the KB. **Safety.** Writes AWS. No Bedrock calls.
**Example.** `python scripts/verify_kb_grants.py`

### verify_kb_fanout_equivalence.py

**Purpose.** Show that querying two KB indexes and merging returns the same top-k passages, in the same order, as one filtered index, using the corpus's own stored vectors as queries.

```
verify_kb_fanout_equivalence.py [-h] --run-id RUN_ID [--owner OWNER] [--queries QUERIES] [--k K]
                                [--table-prefix TABLE_PREFIX] [--region REGION]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--run-id` | str | required | A run whose corpus is embedded. |
| `--owner` | str | `local-single-user` | |
| `--queries` | int | `200` | |
| `--k` | int | `5` | |
| `--table-prefix` | str | `TABLE_PREFIX`, else `matrix-studio` | |
| `--region` | str | `us-east-1` | |

**Reads.** `VECTOR_BUCKET`, `DATA_BUCKET` (required); the run's vectors. **Writes.** Two temporary KBs with indexes and copied vectors, left in place and printed. **Safety.** Writes AWS. No Bedrock calls.
**Example.** `python scripts/verify_kb_fanout_equivalence.py --run-id <run-id> --queries 50`

### verify_kb_retrieval.py

**Purpose.** End to end through the deployed HTTP API with a real token: a throwaway Cognito user signs in over SRP, creates a KB, uploads a document, starts a 4-turn run bound to it, and checks the persona retrieves and quotes only that document; also checks that an unreadable binding is refused at creation.

```
verify_kb_retrieval.py [-h] [--stack STACK] [--region REGION] [--timeout TIMEOUT] [--keep]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--stack` | str | `matrix-studio-stack` | Stack whose outputs give the SPA URL, pool, client and vector bucket. |
| `--region` | str | `AWS_REGION`, else `us-east-1` | |
| `--timeout` | int, seconds | `900` | |
| `--keep` | flag | off | Leave the user, collection and run in place. |

**Reads.** CloudFormation outputs; `DATA_BUCKET` (required), `TABLE_PREFIX`. Needs `pycognito` (in the `dev` extra). **Writes.** A Cognito user, KB, index, document and run; all removed at the end unless `--keep`. **Safety.** Writes AWS; paid model calls (about $0.03).
**Example.** `python scripts/verify_kb_retrieval.py --keep`

### verify_tenant_isolation.py

**Purpose.** Assume the tenant role with a session policy for one test owner and check it is refused access to another owner's DynamoDB partition and S3 prefix. The tenant role must trust the caller, which needs the `verify_principal_arn` stack context.

```
verify_tenant_isolation.py [-h] [--stack STACK] [--prefix PREFIX] [--region REGION]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--stack` | str | `matrix-studio-stack` | |
| `--prefix` | str | `matrix-studio` | Table prefix. |
| `--region` | str | `AWS_REGION`, else `AWS_DEFAULT_REGION`, else the boto3 session | |

**Reads.** CloudFormation outputs; STS. **Writes.** Test items and objects under two fixed test owners, deleted at the end. **Safety.** Writes AWS. No Bedrock calls.
**Example.** `AWS_PROFILE=<profile> python scripts/verify_tenant_isolation.py --stack matrix-studio-stack`

### verify_turn_loop.py

**Purpose.** Check the deployed state machine: a long run completes, a stop lands after the in-flight turn, a cost cap ends a run as `capped`, and branch and resume execute. Writes run rows directly and starts executions directly.

```
verify_turn_loop.py [-h] [--turns TURNS] [--only {completion,stop,cap,branch,resume}]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--turns` | int | `40` | Turns for the completion check. |
| `--only` | choice | all | `completion`, `stop`, `cap`, `branch` or `resume`. |

**Reads.** `TABLE_PREFIX`, `DATA_BUCKET` (required); `AWS_REGION` (default `us-east-1`); `TURN_LOOP_ARN`, else the first state machine whose name ends in `turn-loop`. **Writes.** Runs under the owner `phase5-verify`, left in place; executions. **Safety.** Writes AWS; paid model calls (the deployment's default model, plus a summary).
**Example.** `scripts/verify_turn_loop.py --only completion --turns 6`

### verify_vector_retrieval.py

**Purpose.** Check S3 Vectors k-NN and its metadata filter against the real service, on the first index in the stack's vector bucket.

```
verify_vector_retrieval.py [-h] [--stack STACK] [--region REGION]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--stack` | str | `matrix-studio-stack` | |
| `--region` | str | `AWS_REGION`, else `AWS_DEFAULT_REGION` | Neither set: exit 1. |

**Reads.** CloudFormation outputs; the index description. **Writes.** Test vectors, deleted at the end. **Safety.** Writes AWS. No Bedrock calls.
**Example.** `AWS_PROFILE=<profile> python scripts/verify_vector_retrieval.py`

## Measurement and analysis

These scripts implement pre-registered comparisons and measurements. Those that write an analysis file and take `--out` refuse (exit 2) unless the path starts with `private/`, because the output quotes stored transcripts.

### analyse_cite_inline.py

**Purpose.** Score the cite-inline comparison (off runs against on runs) against its pre-registered criteria and print MET or MISSED for the primary and each guardrail.

```
analyse_cite_inline.py [-h] --owner OWNER --off OFF [OFF ...] --on ON [ON ...] [--comparison {1,2}]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--off` | str, one or more | required | Run ids. |
| `--on` | str, one or more | required | Run ids. |
| `--comparison` | 1 or 2 | `1` | Which pre-registered criteria. |

**Reads.** Stored runs (`DATA_BUCKET`, `AWS_REGION` required). **Writes.** Nothing. **Safety.** read-only (AWS).
**Example.** `scripts/analyse_cite_inline.py --owner <owner-sub> --off <run-a> --on <run-b>`

### analyse_evidence_lean.py

**Purpose.** Score the evidence-lean comparison: one analyst call per run per repeat, then the pre-registered primaries and guardrails.

```
analyse_evidence_lean.py [-h] --owner OWNER --off OFF [OFF ...] --on ON [ON ...] --out OUT
                         [--criteria {1,2}] [--repeats REPEATS]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--off`, `--on` | str, one or more | required | Run ids. |
| `--out` | path | required | Must start with `private/`. |
| `--criteria` | 1 or 2 | `1` | Which pre-registered criteria. |
| `--repeats` | int | `1` | Analyst passes per run. |

**Reads.** Stored runs. **Writes.** `--out`. **Safety.** read-only (AWS); paid model calls; writes local.
**Example.** `scripts/analyse_evidence_lean.py --owner <owner-sub> --off <run-a> --on <run-b> --out private/docs/lean.json`

### analyse_moderator_assumptions.py

**Purpose.** Two passes. Without `--labels`: list every assumption the moderator made and every check, for labelling as FACT, PLAN, DECISION or POSITION. With `--labels`: score the primaries from the labels, then the guardrails with one analyst call per run.

```
analyse_moderator_assumptions.py [-h] --owner OWNER --on ON [ON ...] --off OFF [OFF ...] [--labels LABELS] --out OUT
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--on`, `--off` | str, one or more | required | Run ids. |
| `--labels` | path | | JSON of `<run8>:<assumption-id>` to label. A missing or invalid label: exit 2. |
| `--out` | path | required | Must start with `private/`. Written in both passes. |

**Reads.** Stored runs; the labels file. **Writes.** `--out`. **Safety.** read-only (AWS); paid model calls only with `--labels`; writes local.
**Example.** `scripts/analyse_moderator_assumptions.py --owner <owner-sub> --on <run-a> --off <run-b> --out private/docs/ma.json`

### analyse_section9.py

**Purpose.** Score the research comparison between a control ensemble and a research ensemble exactly as pre-registered, from the parent rows, member event logs and reports.

```
analyse_section9.py [-h] --owner OWNER --control CONTROL --research RESEARCH [--run {1,2}] [--show-1b]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--control` | str | required | Ensemble id. |
| `--research` | str | required | Ensemble id. |
| `--run` | 1 or 2 | `1` | Which pre-registration (first or second comparison). |
| `--show-1b` | flag | off | Print every unresolved item the 1b rule matched. |

**Reads.** Stored ensembles and runs. **Writes.** Nothing. **Safety.** read-only (AWS); no model calls.
**Example.** `scripts/analyse_section9.py --owner <owner-sub> --control <ensemble-a> --research <ensemble-b>`

### measure_evidence_plan.py

**Purpose.** For stored runs, ask the analyst for only the evidence plan and conditional recommendation, and count how often each column is "not stated".

```
measure_evidence_plan.py [-h] --owner OWNER --out OUT [--runs [RUNS ...]] [--latest LATEST] [--min-turns MIN_TURNS]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--out` | path | required | Must start with `private/`. |
| `--runs` | str, zero or more | `[]` | Run ids. Empty: the latest complete runs. |
| `--latest` | int | `8` | How many runs when `--runs` is empty. |
| `--min-turns` | int | `20` | Minimum turns for a run to be picked. |

**Reads.** Stored runs. **Writes.** `--out`. **Safety.** read-only (AWS); paid model calls (one per run); writes local.
**Example.** `scripts/measure_evidence_plan.py --owner <owner-sub> --out private/docs/plan.json --latest 4`

### measure_retrieval_recall.py

**Purpose.** Measure retrieval recall on a corpus: index the target files as a run, have a model write a natural and a paraphrased question per sampled chunk, and report strict and lenient recall per retrieval mode.

```
measure_retrieval_recall.py [-h] [--sample SAMPLE] [--k K] [--seed SEED] [--concurrency CONCURRENCY]
                            [--model MODEL] [--json-out JSON_OUT] [--queries-out QUERIES_OUT]
                            [--queries-in QUERIES_IN] [--compare] [--diluted] [--modes MODES]
                            [--embedding-model EMBEDDING_MODEL] [--dimensions DIMENSIONS]
                            [--run-id RUN_ID] [--gen-max-tokens GEN_MAX_TOKENS] [--term-limit TERM_LIMIT]
                            [--max-df-ratio MAX_DF_RATIO] [--score-ratio SCORE_RATIO]
                            targets [targets ...]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `targets` | path, one or more | required | Files and directories to index. |
| `--sample` | int | `40` | Chunks sampled. |
| `--k` | int | `5` | Retrieval depth. |
| `--seed` | int | `11` | |
| `--concurrency` | int | `8` | |
| `--model` | str | settings default | Query-generation model. |
| `--json-out` | path | | Results file. |
| `--queries-out` | path | | Save generated queries. |
| `--queries-in` | path | | Reuse saved queries instead of generating. |
| `--compare` | flag | off | Also evaluate the tuned lexical pipeline. |
| `--diluted` | flag | off | Add a diluted-query arm. |
| `--modes` | str, comma-separated | `baseline` | Any of `baseline`, `tuned`, `vector`, `hybrid`. |
| `--embedding-model` | str | `""` (default model) | |
| `--dimensions` | int | provider default | Embedding width. |
| `--run-id` | str | `eval-<timestamp>` | Run id the corpus is indexed under. |
| `--gen-max-tokens` | int | `1000` | Query-generation token cap. |
| `--term-limit` | int | `8` | Tuned pipeline. |
| `--max-df-ratio` | float | `0.5` | Tuned pipeline. |
| `--score-ratio` | float | `0.25` | Tuned pipeline. |

**Reads.** The targets; storage environment including `VECTOR_BUCKET` and `VECTOR_INDEX`. **Writes.** A real run with documents and vectors in AWS; the output files. **Safety.** Writes AWS; paid model calls (generation and embeddings); writes local.
**Example.** `scripts/measure_retrieval_recall.py docs --sample 20 --modes baseline,vector --queries-out /tmp/q.json`

### eval_speaker_selection.py

**Purpose.** Replay a recorded transcript and record which speaker each selection arm would pick at every turn; print distribution metrics per run and arm.

```
eval_speaker_selection.py [-h] --run RUN --owner OWNER [--arm {baseline,best,best+counts,counts,counts+budget,counts+budget+decline}]
                          [--floor] [--closed-loop] [--model MODEL] [--check-baseline]
                          [--check-baseline-arm {...}]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--run` | str, repeatable | required | Recorded run id. |
| `--owner` | str | required | |
| `--arm` | choice, repeatable | `baseline` | `baseline`, `best`, `best+counts`, `counts`, `counts+budget`, `counts+budget+decline`. |
| `--floor` | flag | off | Also apply the participation floor. |
| `--closed-loop` | flag | off | Participation counts come from the arm's own picks. |
| `--model` | str | `bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0` | Selector model. |
| `--check-baseline` | flag | off | Check the engine's selection prompt matches an arm, then exit. |
| `--check-baseline-arm` | choice | `counts+budget+decline` | Arm the engine's prompt should match. |

**Reads.** Stored runs (`DATA_BUCKET` required). **Writes.** Nothing. **Safety.** read-only (AWS); paid model calls (one selection call per turn per arm). Exit 1 when any replay fails.
**Example.** `scripts/eval_speaker_selection.py --run <run-id> --owner <owner-sub> --arm baseline --arm counts`

### ensemble_report.py

**Purpose.** Collate several runs of one brief from the command line: per-run metrics, each persona across runs, standing dissents, and one synthesis. Uses the same aggregation as the ensemble report (`matrix_studio/ensemble.py`).

```
ensemble_report.py [-h] --owner OWNER [--run RUN] [--match MATCH] [--no-synthesis]
                   [--no-extraction] [--cache CACHE] [--refresh]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--run` | str, repeatable | `[]` | Run ids. |
| `--match` | str | | Instead of ids: every complete run whose name contains this. |
| `--no-synthesis` | flag | off | Skip the synthesis call. |
| `--no-extraction` | flag | off | Metrics only; no model calls. |
| `--cache` | path | `/tmp/ensemble-cache.json` | Per-run extractions, reused. |
| `--refresh` | flag | off | Ignore the cache. |

**Reads.** Stored runs (`DATA_BUCKET` required); the cache. **Writes.** The cache file. **Safety.** read-only (AWS); paid model calls (one extraction per uncached run, one synthesis) unless `--no-extraction`; writes local.
**Example.** `scripts/ensemble_report.py --owner <owner-sub> --match quiet-harbor --no-synthesis`

### tune_clustering.py

**Purpose.** Measure claim-clustering recall on a stored ensemble without regenerating a report: extractions are cached once, then each clustering variant is run and summarised; `--review` prints the merges most worth a human check.

```
tune_clustering.py [-h] --owner OWNER --ensemble ENSEMBLE [--cache CACHE] [--refresh]
                   [--variant {none,one-pass,two-pass}] [--review REVIEW] [--model MODEL]
                   [--batch BATCH] [--repeat REPEAT]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--ensemble` | str | required | Ensemble id. |
| `--cache` | path | `/tmp/clustering-extractions.json` | |
| `--refresh` | flag | off | Re-extract even if cached. |
| `--variant` | choice, repeatable | `none` and `one-pass` | `none`, `one-pass`, `two-pass`. |
| `--review` | int | `0` | Print the N most suspicious merges. |
| `--model` | str | the summary role's model | `low` selects `LOW_VARIANCE_MODEL`. |
| `--batch` | int | | Claims per clustering call within a kind. |
| `--repeat` | int | `1` | Runs per variant. |

**Reads.** Stored ensemble and members (`DATA_BUCKET`, `AWS_REGION` required). **Writes.** The cache file. **Safety.** read-only (AWS); paid model calls; writes local.
**Example.** `scripts/tune_clustering.py --owner <owner-sub> --ensemble <ensemble-id> --review 12`

### shadow_assumption_checks.py

**Purpose.** Run the moderator's dynamic-assumption check on stored transcripts at every point it would have fired, through the engine's own filters, without recording anything to the runs.

```
shadow_assumption_checks.py [-h] --owner OWNER --runs RUNS [RUNS ...] [--every EVERY] --out OUT
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--runs` | str, one or more | required | Run ids. |
| `--every` | int | `4` (`assumptions.DEFAULT_EVERY`) | Check after every Nth completed turn. |
| `--out` | path | required | Must start with `private/`. |

**Reads.** Stored runs. **Writes.** `--out`. **Safety.** read-only (AWS); paid model calls (the run's `speaker_selection` model); writes local.
**Example.** `scripts/shadow_assumption_checks.py --owner <owner-sub> --runs <run-id> --out private/labels/shadow.json`

### folding_packets.py

**Purpose.** Build anonymous judging packets (each run's defended positions and final 10 turns, under shuffled labels `P01`, `P02`, ...) and a separate key mapping labels to run ids.

```
folding_packets.py [-h] --owner OWNER --runs RUNS [RUNS ...] --packets PACKETS --key KEY [--seed SEED]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--owner` | str | required | |
| `--runs` | str, one or more | required | Run ids. |
| `--packets` | path | required | Markdown output. Must start with `private/`. |
| `--key` | path | required | JSON output. Must start with `private/`. |
| `--seed` | int | `20260929` | Shuffle seed. |

**Reads.** Stored runs. **Writes.** The two files. **Safety.** read-only (AWS); writes local; no model calls.
**Example.** `scripts/folding_packets.py --owner <owner-sub> --runs <run-a> <run-b> --packets private/p.md --key private/k.json`

### research_definition.py

**Purpose.** Run the pre-conversation research pass on a definition and print what was found (queries, sources, tiers). Stores nothing in AWS.

```
research_definition.py [-h] [--dry-run] [--only ONLY] [--write DIR] [--provider PROVIDER]
                       [--results RESULTS] [--fetch FETCH] definition
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `definition` | path | required | |
| `--dry-run` | flag | off | Generate and print the queries; no search, no fetch. |
| `--only` | str, repeatable | `[]` | Restrict to these personas; `shared` selects the shared corpus. |
| `--write` | path | | Write each corpus to this directory as files. |
| `--provider` | str | by available key | `brave`, `tavily` or `exa`. |
| `--results` | int | `5` | Results per query. |
| `--fetch` | int | `3` | Pages fetched per query; `0` disables fetching. |

**Reads.** The definition; `AWS_REGION`; a search key (`TAVILY_API_KEY`, `EXA_API_KEY` or `BRAVE_API_KEY`). **Writes.** The `--write` directory. **Safety.** Paid model calls (query planning and tiering); paid search calls unless `--dry-run`; writes local with `--write`.
**Example.** `scripts/research_definition.py /tmp/def.json --dry-run`

### build_validation_arms.py

**Purpose.** Generate the premise-validation arm request files from one definition, so the arms differ only in the intended field. Writes `examples/validation/arm-a-control.json` through `arm-g-cognition.json` (seven files).

```
python scripts/build_validation_arms.py
```

No arguments; no argparse. **Reads.** Nothing outside the script. **Writes.** Seven files under `examples/validation/`, overwriting them. **Safety.** Writes local. Not run: no `--help`.
**Example.** `python scripts/build_validation_arms.py`

### score_validation.py

**Purpose.** Score the validation arms' result files against each other: deterministic transcript metrics, and optionally one blind judge call per arm under shuffled labels.

```
score_validation.py [-h] [--judge] [--model MODEL] [--seed SEED] [--json-out JSON_OUT] results_dir
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `results_dir` | path | required | Directory of result files named `arm-a-control.json` ... `arm-g-cognition.json`; arms D to G are optional. |
| `--judge` | flag | off | Also run the blind judge. |
| `--model` | str | `LITELLM_MODEL` | Judge model. |
| `--seed` | int | `7` | Blinding shuffle seed. |
| `--json-out` | path | | Report file. |

**Reads.** The result files. **Writes.** `--json-out`. **Safety.** read-only (local); paid model calls with `--judge`; writes local with `--json-out`.
**Example.** `scripts/score_validation.py /tmp/val-results`

### check_concern_leak.py

**Purpose.** Pre-filter run results for a persona's withheld `underlying_concern` reaching its utterances, memories or reflections, by overlap with the concern's private words. The concerns come from `scripts/build_validation_arms.py` (loaded as a module, not run). Exit 1 when anything is flagged. **Only for runs that withheld their concerns** (`config.personas.withhold_concerns: true`, which the validation arms set). Since 2026-10-02 a new run states its concerns plainly by default, and there a concern in the transcript is the intended behaviour, not a leak.

```
check_concern_leak.py [-h] [--threshold THRESHOLD] results [results ...]
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `results` | path, one or more | required | Result JSON files of runs that withheld their concerns. |
| `--threshold` | float | `0.5` | Private-word overlap that flags. |

**Reads.** The result files. **Writes.** Nothing. **Safety.** read-only (local).
**Example.** `scripts/check_concern_leak.py /tmp/val-results/arm-e-mandatory.json`

### judge_variance.py

**Purpose.** Judge the same transcripts repeatedly with the shipped validation judge (`score_validation.judge_arm`) and report each field's spread.

```
judge_variance.py [-h] [--repeats REPEATS] [--model MODEL] --out OUT
```

| Argument | Type | Default | Notes |
|---|---|---|---|
| `--repeats` | int | `5` | Judgements per transcript. |
| `--model` | str | `LITELLM_MODEL` | |
| `--out` | path | required | Must start with `private/`. |

**Reads.** `docs/labels/sonnet-transcripts.json` (relative to the working directory). **Writes.** `--out`. **Safety.** Paid model calls; writes local.
**Example.** `scripts/judge_variance.py --repeats 3 --out private/docs/judge.json`
