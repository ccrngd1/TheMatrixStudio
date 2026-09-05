# Phase 5 — Per-Persona Document Retrieval

**Status:** APPROVED, in build. Supersedes nothing; additive to v0.4.0.

## The requirement

An operator can attach background documents (PDF, Word, text, Markdown) to a
**specific persona**, and that persona draws on them during a run **without the
document sitting in the prompt context on every call.**

Today a persona is one prose blob plus a list of goals. There is no way to give
one agent forty pages of background. Putting it in the persona string is the only
current option, and that pays the full token cost on every single call.

## The decision: SQLite FTS5 first, vectors only if measurement demands it

Retrieval ships as **SQLite FTS5 full-text search over per-persona document
chunks, stored in the existing database file.** No embeddings, no vector store,
no new service, no new runtime dependency for search.

### Why not FAISS or a vector database

Because at this project's scale, an approximate-nearest-neighbour index optimises
something that is already free. Measured on this machine — exhaustive brute-force
cosine over 1024-dim float32, no index at all:

| Corpus | ≈pages | Exhaustive KNN | Share of one turn (4–7 s) |
|---|---|---|---|
| 1,000 chunks | 40 | 0.03 ms | ~0.0005% |
| 5,000 | 200 | 0.10 ms | ~0.002% |
| 20,000 | 800 | 0.57 ms | ~0.01% |
| 100,000 | 4,000 | 5.96 ms | ~0.1% |

ANN indexes earn their complexity around 10⁶–10⁷ vectors. Per-persona background
documents land at 10³–10⁴ chunks. **Search latency is not a real constraint
here**, so the store must be chosen on operational grounds instead:

| | FTS5 *(chosen)* | sqlite-vec | FAISS | Vector DB |
|---|---|---|---|---|
| New dependency | **none — built in** | loadable extension | large wheel | service |
| Atomic with existing writes | **yes, same file** | yes, same file | no — second file | no — network |
| Files to back up | **1** | 1 | 2 | separate lifecycle |
| Metadata filtering | **SQL `WHERE`** | SQL `WHERE` | none — hand-rolled | rich |
| Rebuildable from source of truth | **one SQL command** | re-embed everything | re-embed everything | re-embed everything |
| Needs an embedding provider | **no** | yes | yes | yes |

Two properties decided it, and both are specific to this codebase rather than
general preferences:

1. **Atomicity.** The project is already event-sourced into a single SQLite file.
   An FTS5 index in that same file means a document and its index commit in **one
   transaction**. With FAISS you write SQLite, then write a `.faiss` file — two
   writes with no atomicity, so a crash between them leaves the index disagreeing
   with the record. That is exactly the drift the premise-validation panel
   flagged as its strongest finding.
2. **No embedding provider at all.** Every vector option requires embeddings.
   Local embeddings mean `sentence-transformers`, which means `torch` — hundreds
   of megabytes to multiple gigabytes, which would wreck the five-minute
   quickstart that `PROJECT-SPEC.md` §7 pins down. API embeddings avoid the
   install weight but add a per-chunk ingest cost and a per-query call, which
   runs straight into the cost gate. FTS5 sidesteps the entire question.

Verified on this machine: SQLite 3.53.1, **FTS5 available**, and extension
loading supported (so `sqlite-vec` remains viable later without a rebuild of
anything).

### Honest statement of what FTS5 gives up

BM25 is lexical. It matches words, not meaning. A query about "cost" will not
retrieve a passage that only ever says "spend" or "budget". That is a real
limitation, not a detail — and it is the reason this design ships a **retrieval
inspection endpoint** rather than asking anyone to trust it.

The premise-validation cast deliberately encoded "embeddings or it isn't real
retrieval" as an `overgeneralised` position, because at 10³ chunks of the
operator's *own* documents, the operator's vocabulary usually **is** the
document's vocabulary. That is a testable claim, and Phase 5 is built to test it
rather than assume it either way.

### What would trigger a move to vectors

Explicit, measured triggers — not vibes:

- Measured recall on a real corpus shows FTS5 missing passages a persona needed
  to defend a position (the inspection endpoint exists to produce this number).
- A corpus exceeds ~10⁵ chunks, where exhaustive scan starts to be visible.
- Hosted multi-tenant deployment (already a roadmap item) makes corpora shared
  and large.

If that happens, the migration is additive: `doc_chunks` already holds the text,
so adding a vector column or a `sqlite-vec` virtual table re-uses the same
chunking, the same scoping, and the same retrieval interface. **Nothing in this
design has to be undone.**

## Schema

New tables only. No column added to and no semantics changed on `runs`, `events`,
`snapshots`, `summaries`, `threads`, or `thread_messages` — every existing query
keeps working, and old databases pick the new tables up on connect.

```sql
documents(
  id TEXT PRIMARY KEY, run_id, persona_name, title, source_path,
  media_type, char_count, chunk_count, created_at
)
doc_chunks(
  id INTEGER PRIMARY KEY, document_id, run_id, persona_name,
  ordinal, content, UNIQUE(document_id, ordinal)
)
doc_chunks_fts USING fts5(content, content='doc_chunks',
                          content_rowid='id', tokenize='porter unicode61')
```

`persona_name IS NULL` means the document is shared by the whole cast; a named
persona means only that persona can retrieve it. Scoping is a SQL predicate, so a
persona provably cannot retrieve outside its own slice.

### The rebuild path is satisfied by construction

`doc_chunks_fts` is an FTS5 **external-content** table: it holds no text of its
own, only the index, and `doc_chunks` is the source of truth. Rebuilding is one
statement:

```sql
INSERT INTO doc_chunks_fts(doc_chunks_fts) VALUES('rebuild');
```

This is the panel's hard prerequisite met by design rather than bolted on. The
index cannot hold content that `doc_chunks` does not, so the two cannot
semantically diverge — the worst case is a stale index, repaired by the statement
above, exposed as an explicit reindex operation.

## Retrieval contract

`retrieve_documents(run_id, persona_name, query, k, max_chars)`:

1. Scope to `persona_name = ? OR persona_name IS NULL`.
2. BM25 rank via FTS5 `MATCH`, best first.
3. Truncate to `k` chunks **and** a hard `max_chars` budget — the budget is the
   feature. A forty-page document contributes at most `max_chars` to a call.
4. Return chunks with `document_id`, `title`, `ordinal` so every retrieved
   passage is citable.

**Query sanitisation is mandatory.** Raw conversation text contains quotes and
FTS5 operators (`AND`, `OR`, `NEAR`, `*`, `^`, `-`, `:`) that would either raise a
syntax error or silently change the query's meaning. Queries are reduced to bare
alphanumeric terms and OR-ed. This is a correctness requirement, not hardening.

## Engine integration

Mirrors the established Phase 2c memory / Phase 4b thread pattern exactly:

- Retrieval happens in `_run_turns` **before** `_generate_response`, keyed on the
  speaker.
- Retrieved chunks are injected into the system message as a
  `documents_block` — and, unlike threads, this works with **cognition off**,
  because attaching background to a persona is useful without a cognition loop.
- The chunk ids fed into the prompt are recorded as `document_refs` on the
  `agent.response` event — the same causal-refs discipline as `memory_refs` and
  `thread_refs`, so a turn's document influence is auditable rather than claimed.
- New event `document.retrieved` carries the query terms, chunk ids and BM25
  scores. Generic events table, no migration; the frontend ignores unknown event
  types, so it is additive for the UI too.

## Configuration

Off by default, following the `max_run_cost_usd` / `validation_enabled` /
`cognition.threads` precedent — a run with no `retrieval` block behaves
byte-for-byte as it does today.

```json
"retrieval": { "enabled": true, "k": 3, "max_chars": 1200 }
```

Per-persona attachment in the cast, so the CLI and examples work without the API:

```json
{ "name": "Priya", "persona": "...", "goals": ["..."],
  "documents": ["./background/spec.pdf", "./background/notes.md"] }
```

PDF and Word extraction use `pypdf` and `python-docx` as **optional extras**. A
missing extractor raises a clear error naming the package to install; `.txt` and
`.md` need nothing. Base install weight is unchanged.

## Live verification (2026-09-05)

Slice 5b-1 was run against a real model with real documents, not only mocks.

`docs/PROJECT-SPEC.md` (17,937 chars) attached to one persona and
`PHASE4-REPORT.md` (20,414 chars) to another, 4 turns, cost **$0.0063**:

| Observation | Result |
|---|---|
| Ingestion | 29 and 33 chunks respectively, no errors |
| Document chars reaching a prompt | **913 of 17,771 (~5%)** — the budget working |
| Scoping | each persona retrieved only from its own document, every turn |
| Audit trail | `document.retrieved` recorded query + passages + scores; `document_refs` on every response matched the retrieved chunk ids |

Running the shipped `examples/retrieval-demo.json` (6 turns, $0.0124) produced
personas citing their own material by name — e.g. *"Per distribution-constraints.md
#1…"* and *"Per distribution-constraints.md #0, we have a five-minute window."*

### Two real defects the live run exposed

Both were fixed, and neither was visible in the mocked tests:

1. **Conversational filler dominated the queries.** Turn-2 onward produced
   `"look" OR "make" OR "call" OR "doesn't" OR "blow" OR "it's"`. Contractions
   were slipping past the stopword filter because tokenisation keeps the internal
   apostrophe. Fixed by adding a contraction set, stripping possessives, and
   falling back to the stem for unlisted contractions. Post-fix the same run
   produced `"sqlite" OR "fts5" OR "embedded" OR "vector" OR "index" OR "node"`.
2. **A zero `max_chars` emitted a junk passage.** The oversized-first-passage
   branch produced a bare `" …"` instead of nothing. Fixed with an explicit guard.

### Known limitations, unfixed

- **Retrieval has no diversity or novelty pressure.** Because the query is built
  from a sliding window of recent messages, consecutive turns produce near
  identical queries and can retrieve the *same* passage repeatedly (observed:
  one persona retrieved chunk #31 on both of its turns). Ranking by novelty
  against already-retrieved chunks is not implemented.
- **Query construction is the dominant quality lever**, and it is still crude:
  unweighted OR over recent terms. The topic and the last message are treated
  alike beyond ordering.
- **A model can paraphrase a cited passage loosely.** Retrieval guarantees which
  text was in context, not that the persona represents it faithfully. The
  `document_refs`/`document.retrieved` pair exists so that can be checked.
- The lexical/semantic gap is untested on a real corpus — that is step 5b-4.

## Build order

1. **5b-1** ✅ DONE — `documents.py` (extract + chunk), schema + FTS5 + retrieval +
   reindex in `database.py`, `RetrievalConfig`, engine wiring, `document_refs`,
   events, tests. ← *this slice*
2. **5b-2** ✅ DONE — API: attach / list / delete / reindex, plus the
   **retrieval inspection** endpoint that makes quality measurable.

   Verified against a live server. The inspection endpoint immediately
   demonstrated the lexical gap it exists to measure: on a real corpus
   `q=how much money will this burn` returned **0 passages**, while
   `q=measured token delta cost` returned the correct passage. That is the
   number step 5b-4 has to move or accept.

   One further defect fixed here, found by an API test: `max_chars` was
   documented as a hard ceiling but the truncation ellipsis was appended
   *after* clipping, returning `max_chars + 2`. The ellipsis is now charged
   against the budget, and the ceiling is asserted across awkward sizes
   (1, 2, 3, 4, 17, 99, 100, 301).
3. **5b-3** — CLI ingest, example with a real document, UI affordance in the
   dossier showing which passages a persona drew on.
4. **5b-4** — Measure FTS5 recall on a real corpus. Publish the number. Decide
   vectors on evidence.
