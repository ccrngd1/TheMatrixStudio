# SPDX-License-Identifier: Apache-2.0
"""
The researcher: turn a brief and a cast into corpora of authorities.

`websearch` finds, `webfetch` reads, `documents.ingest_text` stores. This module decides **what to
look for**, **how much weight what came back deserves**, and **what to record when nothing came
back**. `docs/PERSONA-RESEARCH.md` is the design.

## Three jobs, and only two of them are a model's

**Queries are generated.** A model turns a brief into search queries, and turns each persona's
viewpoint into two: one for the position and one for the `evidence_that_shifts` they already
declared. The second is not optional — §2.2 — because searching only for support builds a
confirmation-bias engine where everyone arrives armed and nobody can move.

**Authority is judged.** Controlling, persuasive or commentary (§3). A model has to do this: it is a
reading of what a document *is*, and a URL pattern cannot tell a board's own opinion from a blog
quoting one. It is recorded per document so a wrong call is arguable rather than invisible.

**The documented negative is COMPUTED.** No model call, deliberately. Its entire value is being
falsifiable — "we searched these queries, saw these URLs, read these, could not read these, found no
controlling authority, on this date" — and every one of those is a fact this module already holds. A
model asked to write it could produce a fluent paragraph asserting a search that never happened,
which is the one failure that would make the feature worse than nothing: a fabricated negative is
evidence for a launch decision.

## What a corpus is

One `Corpus` per scope: the shared one, and one per persona. Each carries its documents, the queries
that produced them, and the negative when there is one. Nothing here writes to storage — the caller
ingests, because that is where knowledge-base ownership and binding live (§5.1).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from matrix_studio.jsonio import extract_json_object

logger = logging.getLogger(__name__)

#: Queries per persona viewpoint: one for the position, one for what would defeat it.
QUERIES_PER_VIEWPOINT = 2

#: Queries the shared corpus gets from the brief. Enough to cover a subject from a few angles without
#: turning one run's preparation into a hundred fetches.
SHARED_QUERIES = 6

#: Results taken per query, and pages fetched per query. Fetching is the slow part, so this is the
#: number that decides how long a research pass takes.
RESULTS_PER_QUERY = 5
FETCH_PER_QUERY = 3

#: Characters a source must yield to enter a corpus, whoever supplied the text.
#:
#: Mirrors `webfetch.MIN_TEXT_CHARS` and exists for the same reason arrived at from the other
#: direction. `webfetch` floors pages it reads itself; this floors text a PROVIDER supplied, which
#: was unguarded. Measured: Tavily returns its own ~150-character summary when its extraction fails,
#: so `assoc.org/KB/.../PCR.aspx` came back as 156 characters — and `TavilySearch` deliberately falls
#: back to the summary, so that entered the corpus as a document. Same failure as an Incapsula block
#: page: a source that could not be read, counted as one that was.
MIN_DOCUMENT_CHARS = 200

#: Authority tiers, strongest first. `unknown` exists because a tiering call that fails must not
#: silently promote everything to `commentary` — that would be a judgement nobody made.
TIERS = ("controlling", "persuasive", "commentary", "unknown")


@dataclass
class Query:
    text: str
    #: `background` for the shared corpus, `support` or `opposition` for a persona's.
    #:
    #: Carried through to the negative so it can say WHICH kind of thing was not found. "We looked
    #: for what would change your mind and found nothing" is a different statement from "we looked
    #: for support and found nothing", and a persona should be able to tell them apart.
    intent: str = "background"
    #: The viewpoint this came from, for a persona corpus.
    viewpoint: Optional[str] = None


@dataclass
class ResearchedDocument:
    title: str
    text: str
    url: str
    authority: str = "unknown"
    #: Why this tier, in the tiering model's words. Shown beside the tier so a reader can disagree
    #: with the reasoning rather than only with the label.
    authority_reason: str = ""
    found_by: List[str] = field(default_factory=list)


@dataclass
class Corpus:
    """One scope's research. `persona` is None for the shared corpus."""

    persona: Optional[str]
    queries: List[Query] = field(default_factory=list)
    documents: List[ResearchedDocument] = field(default_factory=list)
    #: URLs a search returned that could not be read, with the reason. Part of the negative: a state
    #: board's own statute page answering 403 is a fact about the search, not an absence of law.
    unreadable: List[Tuple[str, str]] = field(default_factory=list)
    negative: Optional[str] = None
    cost_usd: float = 0.0

    @property
    def controlling(self) -> List[ResearchedDocument]:
        return [d for d in self.documents if d.authority == "controlling"]


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

_SHARED_QUERY_PROMPT = """You are preparing to research the subject of a discussion, before the \
discussion happens. Produce search-engine queries that would surface the AUTHORITIES on it: \
statutes, regulations, board or regulator opinions, decided cases, official guidance.

THE BRIEF:
{brief}

Rules:
  Write queries, not questions. "arizona licensed practice act continuation of treatment", not \
"is it legal to renew a plan in Arizona?"
  Name jurisdictions explicitly where the brief implies them. A query without a jurisdiction \
returns commentary; a query with one returns the statute.
  Use the vocabulary the authorities use, not the brief's internal shorthand. A statute says \
"provider-client relationship", not "the PCR gate".
  Cover the subject from different angles rather than rephrasing one angle {n} times.
  Do NOT write queries that ask for a conclusion. Look for what the law SAYS, not for whether \
something is allowed — the answer is what the discussion is for.

Reply with ONLY a JSON object, no prose:
{{"queries": ["<query>", ...]}}

Exactly {n} queries."""

_PERSONA_QUERY_PROMPT = """You are preparing research for ONE participant in a discussion, before \
it happens. They hold a position, and they have already stated what would change their mind.

THE SUBJECT OF THE DISCUSSION — every query must stay inside this domain:
{brief}

PARTICIPANT: {name}
POSITION: {position}
WHAT WOULD CHANGE THEIR MIND: {shifts}

Produce exactly two search-engine queries:
  1. One that would find the strongest AUTHORITY supporting their position.
  2. One that would find the thing they said would change their mind.

The second is the important one. They named a condition; go and look for it. A participant who \
only ever sees support for what they already think cannot be moved by evidence, and this \
discussion exists to find out whether they would be.

Rules:
  Queries, not questions. Use the vocabulary authorities use, not the participant's shorthand.
  Name jurisdictions where the position implies them.
  The second query must look for the STATED condition, not for a general counterargument.
  **Stay in the domain above.** A position phrased in general terms — "individual licence exposure",
  "prior authorization", "disclosure requirements" — belongs to THIS subject, not to whichever field
  those words are most common in. A query that lands in human healthcare, insurance or general
  employment law when the subject is licensed practice has found the wrong body of law, however
  well it matches the words.

Reply with ONLY a JSON object, no prose:
{{"support": "<query>", "opposition": "<query>"}}"""


#: Attempts per model call, and the wait before each retry.
#:
#: A research pass makes one call per persona plus one for the brief plus one tiering call per
#: corpus — thirteen or more for a six-persona cast. Measured on the first live attempt: Bedrock
#: answered one of them with `ServiceUnavailableError: Bedrock is unable to process your request`,
#: which is transient. Without a retry, one such answer in thirteen loses the whole pass.
ASK_ATTEMPTS = 4
ASK_BACKOFF_S = 3.0

#: Output budget per research call. **Sized for the model's reasoning, not for the answer.**
#:
#: A persona's two queries are perhaps 80 tokens. The first live run asked for them with a 1000-token
#: budget and got `finish=length` with EMPTY content — the fourth time this project has been caught
#: by a truncated reply returning nothing rather than a fragment, and the second time by forgetting
#: that Sonnet 5 spends most of an output budget reasoning. The clustering measurement is the
#: reference: ~29,000 output tokens to produce ~1,100 tokens of answer.
ASK_MAX_TOKENS = 8000


async def _ask(
    prompt: str, *, call: Any, model: Optional[str], max_tokens: int = ASK_MAX_TOKENS
) -> Tuple[Optional[Dict[str, Any]], float]:
    """One model call returning parsed JSON and its cost. `None` when it could not be had.

    **Never raises.** §5.2 says research is additive, and the first live run proved how badly that
    was violated here: a single transient Bedrock 503 on the second of thirteen calls propagated out
    and lost every query already generated. One persona's queries failing should cost that persona's
    queries.

    Retried because the failure it hit is transient, and not retried forever because a persistent
    failure should surface as a thin corpus quickly rather than as a long stall.
    """
    cost = 0.0
    for attempt in range(1, ASK_ATTEMPTS + 1):
        try:
            result = await call(
                [{"role": "user", "content": prompt}],
                model=model,
                temperature=0.0,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            if attempt < ASK_ATTEMPTS:
                logger.info(
                    "A research call failed (%s); retrying %d of %d after %.0fs",
                    exc, attempt + 1, ASK_ATTEMPTS, ASK_BACKOFF_S,
                )
                await asyncio.sleep(ASK_BACKOFF_S * attempt)
                continue
            logger.warning(
                "A research call failed %d times and is being given up on (%s). Whatever it was "
                "for will be missing from the corpus rather than losing the pass.",
                ASK_ATTEMPTS, exc,
            )
            return None, cost

        cost += float(result.get("cost_usd") or 0.0)
        content = (result.get("content") or "").strip()
        if not content:
            logger.error(
                "A research call returned no content: %d in, %d out, finish=%s. A truncated reply "
                "comes back EMPTY rather than partial, so check the output budget "
                "(max_tokens=%d).",
                result.get("tokens_in", 0), result.get("tokens_out", 0),
                result.get("finish_reason"), max_tokens,
            )
            return None, cost
        return extract_json_object(content), cost
    return None, cost


async def shared_queries(
    brief: str, *, n: int = SHARED_QUERIES, call: Any, model: Optional[str] = None
) -> Tuple[List[Query], float]:
    parsed, cost = await _ask(
        _SHARED_QUERY_PROMPT.format(brief=brief[:6000], n=n), call=call, model=model
    )
    raw = (parsed or {}).get("queries")
    if not isinstance(raw, list):
        logger.warning("Shared query generation produced no usable list; corpus will be empty")
        return [], cost
    seen: set = set()
    out: List[Query] = []
    for q in raw:
        text = str(q).strip()
        # Deduplicated because a model asked for six angles sometimes gives four and two
        # rephrasings, and paying to fetch the same results twice is pure waste.
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(Query(text=text, intent="background"))
    return out[:n], cost


async def persona_queries(
    name: str,
    viewpoints: Sequence[Dict[str, Any]],
    *,
    brief: str = "",
    call: Any,
    model: Optional[str] = None,
) -> Tuple[List[Query], float]:
    """Two queries per viewpoint: the position, and the condition that would defeat it.

    A viewpoint with no `evidence_that_shifts` still gets its support query, and the missing half is
    logged rather than silently skipped: a persona authored without a shifting condition is a
    persona nothing can move, and that is worth seeing in a log rather than discovering from a
    conversation where they never budge.
    """
    out: List[Query] = []
    cost = 0.0
    for viewpoint in viewpoints:
        position = str(viewpoint.get("position") or "").strip()
        if not position:
            continue
        shifts = viewpoint.get("evidence_that_shifts") or []
        shifts_text = "; ".join(str(s) for s in shifts) if shifts else ""
        if not shifts_text:
            logger.info(
                "%s holds %r with no evidence_that_shifts, so only a support query is possible. "
                "A viewpoint nothing can shift will not be moved by research either.",
                name, position[:60],
            )
        parsed, spent = await _ask(
            _PERSONA_QUERY_PROMPT.format(
                brief=(brief or "(not supplied)")[:2500],
                name=name, position=position,
                shifts=shifts_text or "(the participant did not say)",
            ),
            call=call, model=model,
        )
        cost += spent
        got = parsed or {}
        support = str(got.get("support") or "").strip()
        opposition = str(got.get("opposition") or "").strip()
        if support:
            out.append(Query(text=support, intent="support", viewpoint=position))
        if opposition and shifts_text:
            out.append(Query(text=opposition, intent="opposition", viewpoint=position))
    return out, cost


# --------------------------------------------------------------------------- #
# Authority
# --------------------------------------------------------------------------- #

_TIER_PROMPT = """Classify each source by what KIND of thing it is. You are not judging whether it \
supports any position — only its authority.

  controlling   the law or a binding decision itself: a statute, a regulation, a licensing board's \
own opinion or order, a decided case. Something a regulator would be bound by.
  persuasive    authoritative but not binding on this question: another jurisdiction's statute, a \
model act, a regulator's non-binding guidance, a professional body's standard.
  commentary    someone writing ABOUT the law: trade press, a law-firm article, a blog, a forum, a \
vendor page, a summary.

The distinction that matters most: a page QUOTING a statute is commentary; the statute is \
controlling. Judge the source, not the subject matter.

When genuinely unsure, say commentary. Over-promoting a blog to controlling is the damaging error — \
a participant will cite it as though it settled the question.

For each source give the tier and a reason in under 20 words.

Reply with ONLY a JSON object, no prose:
{{"verdicts": [{{"n": <number>, "tier": "controlling|persuasive|commentary", "reason": "<under 20 \
words>"}}]}}

THE SOURCES:
{sources}"""


async def tier_documents(
    docs: Sequence[ResearchedDocument], *, call: Any, model: Optional[str] = None
) -> float:
    """Set `authority` on each document in place. Returns the cost.

    One call for all of them rather than one each: comparison is what the judgement needs — "this
    one quotes the statute the other one IS" — and per-document calls cannot see that.

    A document the model does not rule on keeps `unknown` rather than defaulting to `commentary`.
    Defaulting would be inventing a judgement, and `unknown` is honest about a call not made.
    """
    if not docs:
        return 0.0
    listing = "\n\n".join(
        f"{i}. {d.title or '(untitled)'}\n   URL: {d.url}\n   OPENING: {d.text[:700]}"
        for i, d in enumerate(docs)
    )
    parsed, cost = await _ask(
        _TIER_PROMPT.format(sources=listing), call=call, model=model, max_tokens=4000,
    )
    verdicts = (parsed or {}).get("verdicts")
    if not isinstance(verdicts, list):
        logger.warning(
            "Authority tiering produced no usable verdicts; %d document(s) stay 'unknown'. The "
            "retrieval floor will not privilege anything, which under-uses a corpus rather than "
            "mis-ranking it.", len(docs),
        )
        return cost
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            i = int(v.get("n"))
        except (TypeError, ValueError):
            continue
        tier = str(v.get("tier") or "").strip().lower()
        if 0 <= i < len(docs) and tier in TIERS:
            docs[i].authority = tier
            docs[i].authority_reason = str(v.get("reason") or "")[:200]
    return cost


# --------------------------------------------------------------------------- #
# The documented negative — computed, never generated
# --------------------------------------------------------------------------- #


def documented_negative(corpus: Corpus, *, on: Optional[str] = None) -> Optional[str]:
    """The record of a search that found no controlling authority, or None if one was found.

    **Assembled from facts this module holds, with no model call.** Its whole value is being
    falsifiable — a reader must be able to run the same queries and check. A model asked to write
    this could produce a fluent paragraph describing a search that did not happen, and a fabricated
    negative is worse than no negative because it becomes evidence for a launch decision.

    Records what could not be READ as well as what was not found. A state board's own statute page
    answering 403 is a fact about the search, not an absence of law, and conflating the two would
    let "we could not get in" masquerade as "there is nothing there".
    """
    if corpus.controlling:
        return None
    when = on or date.today().isoformat()
    scope = f"for {corpus.persona}" if corpus.persona else "for the shared record"

    if not corpus.queries:
        # A negative with no queries behind it must NOT say "this search found nothing" — measured on
        # a live run where query generation failed and the document went on to report a search that
        # never happened. That is the exact overstatement §4 exists to prevent, arrived at from the
        # inside: the whole value of a negative is that it describes work actually done, and this one
        # would have entered a corpus as evidence for a launch decision.
        return "\n".join([
            f"# Research could not be carried out {scope}",
            "",
            f"On {when}, no search was performed, because no queries could be generated. The "
            "reasons are below.",
            "",
            "**This says nothing whatsoever about the law.** It is not a negative finding. Nobody "
            "looked. Treat this exactly as you would treat the absence of any research at all — "
            "and if the question matters, re-run it.",
            "",
            "## Why",
            "",
            *(f"- {what} — {why}" for what, why in corpus.unreadable or [("(unrecorded)", "no reason was captured")]),
        ])

    lines = [
        f"# No controlling authority found {scope}",
        "",
        f"Searched on {when}. This document records a search that did not find a statute, "
        "regulation, board opinion or decided case on this question. It is not a conclusion that "
        "none exists — it is a record of what was looked for and what came back, so it can be "
        "checked and superseded.",
        "",
        "## Queries run",
    ]
    for q in corpus.queries:
        intent = {"support": "for the position",
                  "opposition": "for what would change their mind",
                  "background": "background"}.get(q.intent, q.intent)
        lines.append(f"- `{q.text}`  ({intent})")

    lines += ["", "## Sources read, and what they were"]
    if corpus.documents:
        for d in corpus.documents:
            lines.append(f"- [{d.authority}] {d.title or '(untitled)'} — {d.url}")
    else:
        lines.append("- none: no result could be read")

    if corpus.unreadable:
        lines += [
            "",
            "## Sources that could NOT be read",
            "",
            "These were returned by search and not retrieved. A source that refused us is not "
            "evidence of absence, and some of these are primary sources.",
            "",
        ]
        for url, why in corpus.unreadable:
            lines.append(f"- {url} — {why}")

    lines += [
        "",
        "## What this does and does not support",
        "",
        "It supports: *as of this date, this search surfaced no controlling authority.*",
        "",
        "It does not support: *no controlling authority exists.* A different query, a "
        "subscription database, or one of the unreadable sources above could hold one.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Running one corpus
# --------------------------------------------------------------------------- #


async def gather(
    corpus: Corpus,
    *,
    search: Any,
    fetch: Any,
    call: Any,
    model: Optional[str] = None,
    results_per_query: int = RESULTS_PER_QUERY,
    fetch_per_query: int = FETCH_PER_QUERY,
) -> Corpus:
    """Search, read and tier one corpus's queries. Mutates and returns it.

    `search(query, count)` and `fetch(url)` are injected so this is testable without a network, and
    so the caller decides whether fetching is needed at all — a provider that supplies text
    (`websearch.Provider.supplies_text`) makes the fetch step unnecessary.
    """
    by_url: Dict[str, ResearchedDocument] = {}

    for query in corpus.queries:
        try:
            results = await search(query.text, results_per_query)
        except Exception as exc:  # noqa: BLE001
            # §5.2: research is additive. One failed query loses that query, not the corpus.
            logger.warning("Search failed for %r: %s", query.text, exc)
            corpus.unreadable.append((f"(query: {query.text})", f"search failed: {exc}"))
            continue

        fetched = 0
        for r in results:
            url = getattr(r, "url", "") or ""
            if not url:
                continue
            if url in by_url:
                # The same page answering two queries is corroboration, not a duplicate: both
                # queries are recorded against the one document.
                by_url[url].found_by.append(query.text)
                continue
            text = getattr(r, "text", None)
            if text is None:
                if fetched >= fetch_per_query:
                    continue
                fetched += 1
                page = await fetch(url)
                if page is None:
                    # The reason is already logged by the fetcher; recorded here so the negative can
                    # say a source refused us rather than implying it held nothing.
                    corpus.unreadable.append((url, "could not be read"))
                    continue
                text = page.text
            body = str(text).strip()
            if len(body) < MIN_DOCUMENT_CHARS:
                # Too thin to be the source it claims to be. Recorded as unreadable rather than
                # dropped, so the negative can say a source was seen and not obtained — which for a
                # provider-supplied summary is exactly what happened.
                corpus.unreadable.append(
                    (url, f"only {len(body)} chars returned; a summary, not the source")
                )
                continue
            by_url[url] = ResearchedDocument(
                title=getattr(r, "title", "") or url,
                text=body,
                url=url,
                found_by=[query.text],
            )

    corpus.documents = list(by_url.values())
    corpus.cost_usd += await tier_documents(corpus.documents, call=call, model=model)
    corpus.negative = documented_negative(corpus)
    return corpus


async def plan_and_gather(
    brief: str,
    cast: Sequence[Dict[str, Any]],
    *,
    search: Any,
    fetch: Any,
    call: Any,
    model: Optional[str] = None,
) -> List[Corpus]:
    """Every corpus for one run: the shared one, then one per persona with a viewpoint.

    Corpora are gathered CONCURRENTLY. They are independent by construction, each is minutes of
    search and fetch, and a run's start waits on all of them — the same reasoning that took the
    ensemble report from 12–19 minutes to 8.
    """
    shared = Corpus(persona=None)
    shared.queries, cost = await shared_queries(brief, call=call, model=model)
    shared.cost_usd = cost

    persona_corpora: List[Corpus] = []
    for member in cast:
        name = str(member.get("name") or "").strip()
        viewpoints = ((member.get("structured") or {}).get("viewpoints")) or []
        if not name or not viewpoints:
            continue
        corpus = Corpus(persona=name)
        corpus.queries, spent = await persona_queries(
            name, viewpoints, brief=brief, call=call, model=model,
        )
        corpus.cost_usd = spent
        if not corpus.queries:
            # Kept, not dropped. A persona whose query generation failed would otherwise vanish,
            # leaving five corpora for six personas and nothing saying why — the same silent
            # short-count the ensemble work refused, where a missing member is reported so the
            # denominator stays honest. Recorded as unreadable so the negative says it out loud.
            corpus.unreadable.append(
                (f"(queries for {name})", "could not be generated")
            )
        persona_corpora.append(corpus)

    everything = [shared] + persona_corpora
    await asyncio.gather(*(
        gather(c, search=search, fetch=fetch, call=call, model=model) for c in everything
    ))
    return everything


def summarise(corpora: Sequence[Corpus]) -> str:
    """A human-readable report of what a research pass found. For a script, not for a prompt."""
    lines = []
    total = 0.0
    for c in corpora:
        who = c.persona or "SHARED"
        tiers: Dict[str, int] = {}
        for d in c.documents:
            tiers[d.authority] = tiers.get(d.authority, 0) + 1
        lines.append(
            f"{who:<16} {len(c.queries):>2} queries  {len(c.documents):>2} documents  "
            f"{dict(sorted(tiers.items())) or '{}'}"
            + (f"  UNREADABLE {len(c.unreadable)}" if c.unreadable else "")
            + ("  [documented negative]" if c.negative else "")
        )
        total += c.cost_usd
    lines.append(f"\nmodel cost ${total:.4f} (search and fetch are not model calls)")
    return "\n".join(lines)
