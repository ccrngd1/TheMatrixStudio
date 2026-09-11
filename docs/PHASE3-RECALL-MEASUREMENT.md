# Phase 3 — Retrieval recall re-measured on S3 Vectors

**Status:** COMPLETE. Run 2026-09-11 against the deployed stack in account
791580863750, us-east-1. This is the last unmet acceptance criterion for Phases 2
and 3.

`docs/PHASE5-RETRIEVAL-MEASUREMENT.md` measured retrieval on **SQLite FTS5 +
sqlite-vec**. The port replaced all three moving parts at once:

| | before | after |
|---|---|---|
| lexical | SQLite FTS5 `bm25()` | in-process BM25 (`storage/lexical.py`) |
| vector store | `sqlite-vec` | S3 Vectors |
| similarity metric | L2 distance | cosine distance |

Three substitutions, none of them previously measured **together**. A plumbing
suite cannot see a recall regression, so this reproduces the earlier numbers on the
new stack.

Reproduce with:

```bash
export TABLE_PREFIX=matrix-studio AWS_REGION=us-east-1
export DATA_BUCKET=matrix-studio-data-791580863750-us-east-1
export VECTOR_BUCKET=matrix-studio-vectors-791580863750
export VECTOR_INDEX=matrix-studio-chunks

scripts/measure_retrieval_recall.py docs README.md \
    --sample 40 --modes baseline,vector,hybrid --diluted --dimensions 1024 \
    --queries-out /tmp/q.json --json-out /tmp/recall-aws.json
```

Corpus: this project's own documentation — **22 files, 666 chunks, 461,270
chars**. 40 sampled chunks >= 300 chars, k=5, seed 11, **all 40 yielded usable
queries**. Wall clock ~11 minutes; total cost **$0.15** ($0.145 query generation,
$0.0024 embedding 666 chunks).

## Results

**recall@5 (strict)**

| arm | mean overlap | fts (BM25) | vector | hybrid |
|---|---|---|---|---|
| natural | 0.697 | **1.000** | 0.950 | 0.975 |
| **diluted** (engine-shaped) | 0.270 | 0.650 | 0.825 | **0.875** |
| paraphrased | 0.183 | 0.400 | **0.650** | 0.625 |

**recall@1 (strict)** — the figure that matters, because a turn injects only `k`
passages and the first dominates the prompt

| arm | fts (BM25) | vector | hybrid |
|---|---|---|---|
| natural | **0.825** | 0.700 | **0.825** |
| **diluted** | 0.125 | **0.650** | 0.450 |
| paraphrased | 0.175 | **0.325** | 0.275 |

**MRR (strict)**: natural 0.894 / 0.803 / **0.900**; diluted 0.295 / **0.724** /
0.634; paraphrased 0.272 / **0.438** / 0.426.

Random-guess recall@5 on this corpus: **0.0075**. Zero-result rate 0.000 in every
arm and mode, unchanged from before.

## What reproduced

**The vector arm reproduced closely, which is the point of the exercise.** Diluted
recall@5 0.817 → 0.825; paraphrased 0.617 → 0.650. S3 Vectors + Titan Embed v2
retrieves what sqlite-vec + Titan Embed v2 retrieved.

**The central conclusion held.** Vectors beat lexical decisively on the query shape
the engine actually produces: diluted recall@1 goes 0.125 → 0.650 (5.2×), diluted
recall@5 0.650 → 0.825. Lexical search put the right passage first in 1 turn out of
8.

**The hybrid-vs-vector ordering held where it counts.** As before, hybrid wins the
natural arm and **loses to pure vector at k=1 on both diluted and paraphrased**
(0.450 vs 0.650; 0.275 vs 0.325). Equal-weight RRF lets a confident lexical wrong
answer outrank a correct semantic one. Hybrid's new win on diluted recall@5 (0.875
vs 0.825) does not buy back the top slot.

## What did not reproduce, and the honest caveat

Two arms came out **higher** than before: lexical diluted recall@5 0.400 → 0.650,
and vector diluted recall@1 0.367 → 0.650.

**These are not attributable.** The corpus grew with the project (16 files/381
chunks → 22/666), so this is a reproduction on a *different* corpus, not a
controlled A/B. n=40 against n=60 also widens the error bars. It is tempting to read
the lexical gain as "the in-process BM25 beats FTS5" and the vector gain as a
benefit of the metric fix; neither claim is supported:

- The metric fix **cannot** change ranking. Both formulas are monotonic in distance,
  so they order results identically. It changes only the *floor*, and the floor
  rejected nothing in either run.
- The BM25 comparison confounds implementation with corpus.

Establishing either would need a re-run pinned to the original 16-file corpus. Not
done, because the acceptance criterion was "does the port still retrieve well", and
the answer does not depend on it.

## The similarity floor, validated for the first time

The floor was **inert** before the port: `distance_to_cosine` applied the L2
conversion to a cosine distance, so an orthogonal passage scored 0.5 against a 0.15
threshold. Nothing was ever rejected, and that looked exactly like a corpus that
never produced a weak match.

Measured best-match cosine across all 120 vector queries in this run:

| arm | n | min | median | max | min among rank-1 hits |
|---|---|---|---|---|---|
| natural | 40 | 0.361 | 0.641 | 0.879 | 0.441 |
| diluted | 40 | 0.366 | 0.593 | 0.833 | 0.430 |
| paraphrased | 40 | 0.253 | 0.401 | 0.585 | 0.339 |
| **all** | 120 | **0.253** | — | — | — |

The default floor of 0.15 sits **0.103 below the weakest genuine match in 120
retrievals**, so it costs nothing on this corpus — the same conclusion the original
calibration reached, now on the metric that is actually in use.

And it fires. Three probes against the indexed corpus:

| query | S3 distance | cosine | verdict | old L2 formula |
|---|---|---|---|---|
| "how does the session policy scope DynamoDB access per tenant" | 0.400 | 0.600 | kept | 0.920 kept |
| "recipe for sourdough bread with a rye starter and a dutch oven" | 0.870 | **0.130** | **REJECTED** | 0.622 **kept** |
| "zzqx flurble wompat gribbly" | 0.791 | 0.209 | kept | 0.687 kept |

The middle row is the bug, end to end and in production: a bread recipe asked of a
corpus of AWS architecture docs used to come back as a **supporting source** for a
persona's claim. It is now refused.

**Limitation, stated because the table invites the wrong conclusion:** the nonsense
row is *kept* at 0.209. Titan does not embed random tokens near orthogonal to real
text, so the guard catches coherent-but-unrelated queries, not gibberish. It is an
off-topic guard, not an input validator, and lowering the floor to catch gibberish
would start discarding real paraphrased hits (min 0.253).

## The instrument had to be repaired first, and that is worth recording

The script did not run at all against the new storage layer, and two of the three
faults were silent.

**1. It read chunks with raw SQL** (`db._conn.execute("SELECT ... FROM
doc_chunks")`). `Database` is `DynamoStorage` now: no `_conn`, no such table. This
one was loud — an `AttributeError` before any measurement.

The fix matters more than the crash. The chunk inventory now comes from
`chunks_missing_vectors()` — **the same call the embedder uses** — read before
embedding, so "missing" means "all". Deriving the sample independently would have
been the classic instrument bug: scoring gold ids the retriever never returns and
reporting a key mismatch as a quality problem.

It also now passes `text=doc.text` when attaching. Without it the store keeps a
`join_chunks` reassembly, which re-chunks to 2–7% different ordinals — so the gold
chunk id would name different text from the vector at that ordinal, and recall
would have read low for a reason unrelated to retrieval.

**2. `max_tokens=300` silently discarded 30% of the sample.** The first full run
reported `n (queries) 28` against `--sample 40`. Sonnet 5 runs to the cap
(`finish_reason: length` on every call) and when it pads before closing the JSON the
object arrives truncated; `json.JSONDecodeError` then hit a bare `return None`. Four
rejection paths returned `None` without a word, so a truncated response was
indistinguishable from a passage that legitimately produced no question. The
original run lost 1 of 60 on a different model, which is why 300 had looked
generous.

Fixed at both levels: the cap is 1000 (`--gen-max-tokens`), every rejection prints
its reason and `finish_reason`, and the loss report now fires on **any** shortfall
rather than only below half the sample. Re-run: **40 of 40**.

That threshold is the part worth remembering. `usable < len(sample) // 2` let a 30%
loss through in silence, and n is the first thing that makes a recall figure
trustworthy.

**3. The query cache was keyed on the chunk's storage id.** That was stable only
because SQLite handed the same `AUTOINCREMENT` ids to the same corpus. DynamoDB
mints a fresh document uuid per ingest and the chunk id hashes it, so an id-keyed
cache matches nothing on the second run — which `--queries-in` reported as "re-run
with the same targets and seed", sending the reader to hunt a mistake they had not
made. Keys are now a content hash, which is also the more honest identity: a
generated question is ground truth about a *passage*, not a database row. Old cache
files are rejected by name rather than silently mismatched.

Two other repairs: `--run-id` (defaulting to `eval-{timestamp}`) because a fixed
`"eval"` collides with the run-name uniqueness guard on the second attempt in a
shared account, and the dead `tempfile.TemporaryDirectory()` from the SQLite era.

## Consequence: the retrieval default changed

`RetrievalConfig.mode` was `"fts"`, justified on the grounds that vector/hybrid
"require the sqlite-vec extra AND an embedding provider". Both halves are now false:
S3 Vectors is a managed service that is always present, and Bedrock is already a
hard dependency because generation uses it.

**The default is now `"vector"`**, on diluted recall@1 (0.125 → 0.650). Pure vector
over hybrid because hybrid loses the top slot on the engine-shaped arm, in both this
measurement and the original. Cost is one embedding call per turn (~$1e-7), and a
run whose chunks were never embedded degrades to lexical rather than returning
nothing.

All three defaults moved together — `RetrievalConfig.mode`,
`RetrievalConfigModel.mode` and `retrieve_for_turn`'s signature — because three
defaults that disagree read to a user as a setting that does nothing.

**The whole 872-test suite passed both before and after that flip**, which means a
user-visible retrieval behaviour was unguarded. `test_retrieval_mode_default_is_vector`
now pins all three, and mutation-testing confirms it fails when the value reverts.

`"fts"` is retained as a mode *name* for compatibility with stored `config_json`,
though it is in-process BM25 now and no FTS5 remains anywhere in the system.
