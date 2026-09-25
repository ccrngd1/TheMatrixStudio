# SPDX-License-Identifier: Apache-2.0
"""
The researcher: what it looks for, how it weighs what came back, and what it records when nothing did.

Three things here are load-bearing rather than incidental.

**The opposition query.** A persona who only ever sees support for what they already think cannot be
moved by evidence, and this whole tool exists to measure whether they would be
(`docs/PERSONA-RESEARCH.md` §2.2). So the second query — for the `evidence_that_shifts` the persona
already declared — is asserted, and a prompt that stopped demanding it would fail these tests.

**The documented negative is COMPUTED.** No model call. Its value is being falsifiable: a reader must
be able to run the same queries and check. A model asked to write it could produce a fluent paragraph
describing a search that never happened, and a fabricated negative is worse than none because it
becomes evidence for a launch decision. `test_no_model_is_consulted_for_a_negative` is the guard.

**"Could not read" is not "does not exist".** A state board's own statute page answering 403 is a
fact about the search. Conflating it with absence would let "we could not get in" masquerade as
"there is nothing there" — so the negative records both, separately.

Nothing here touches a network or a model: search, fetch and the model call are all injected.
"""

import json

import pytest

from matrix_studio import research as rs


def model(payloads, *, cost=0.01, record=None):
    """A model seam that answers by matching a substring of the prompt."""

    async def call(messages, model=None, temperature=0.0, max_tokens=None):
        prompt = messages[0]["content"]
        if record is not None:
            record.append(prompt)
        for marker, payload in payloads.items():
            if marker in prompt:
                body = payload if isinstance(payload, str) else json.dumps(payload)
                return {"content": body, "cost_usd": cost, "tokens_in": 10, "tokens_out": 5,
                        "finish_reason": "stop"}
        return {"content": "", "cost_usd": cost, "tokens_in": 10, "tokens_out": 0,
                "finish_reason": "stop"}

    return call


class Hit:
    """Stands in for a `websearch.SearchResult`."""

    def __init__(self, url, title="t", text=None):
        self.url, self.title, self.text = url, title, text


class Page:
    def __init__(self, text):
        self.text = text


STATUTE = "The provider has sufficient knowledge of the member to initiate a diagnosis. " * 4

VIEWPOINTS = [
    {
        "position": "Specialty plans are not legend drugs, so no PCR gate applies",
        "firmness": "firm",
        "evidence_that_shifts": ["a state statute defining specialty plans as regulated-only"],
    }
]


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestPersonaQueries:
    async def test_it_searches_for_what_would_change_their_mind(self):
        # THE point of §2.2. Without this the feature arms everyone and moves nobody.
        call = model({"WHAT WOULD CHANGE THEIR MIND": {
            "support": "specialty plan not a legend drug federal",
            "opposition": "state statute specialty plan regulated-only",
        }})
        queries, _ = await rs.persona_queries("Casey", VIEWPOINTS, call=call)

        intents = {q.intent for q in queries}
        assert intents == {"support", "opposition"}
        opp = next(q for q in queries if q.intent == "opposition")
        assert "regulated-only" in opp.text
        assert opp.viewpoint.startswith("Specialty plans are not legend drugs")

    async def test_the_declared_condition_is_given_to_the_model_verbatim(self):
        # The persona already said what would move them. Paraphrasing it into a general
        # counterargument search would look for the wrong thing.
        seen = []
        call = model({"WHAT WOULD CHANGE": {"support": "a", "opposition": "b"}}, record=seen)
        await rs.persona_queries("Casey", VIEWPOINTS, call=call)
        assert "a state statute defining specialty plans as regulated-only" in seen[0]

    async def test_a_viewpoint_with_no_shifting_evidence_gets_no_opposition_query(self, caplog):
        # And it is LOGGED: a viewpoint nothing can shift will not be moved by research either, and
        # that is worth noticing in a log rather than from a conversation where nobody budges.
        call = model({"WHAT WOULD CHANGE": {"support": "a", "opposition": "invented"}})
        with caplog.at_level("INFO"):
            queries, _ = await rs.persona_queries(
                "Jordan", [{"position": "no board has pursued this"}], call=call,
            )
        assert [q.intent for q in queries] == ["support"]
        assert "no evidence_that_shifts" in caplog.text

    async def test_an_unreadable_reply_yields_no_queries_rather_than_junk(self):
        queries, _ = await rs.persona_queries("Casey", VIEWPOINTS, call=model({}))
        assert queries == []

    async def test_cost_is_returned(self):
        call = model({"WHAT WOULD CHANGE": {"support": "a", "opposition": "b"}}, cost=0.02)
        _, cost = await rs.persona_queries("Casey", VIEWPOINTS, call=call)
        assert cost == pytest.approx(0.02)


@pytest.mark.asyncio
class TestSharedQueries:
    async def test_it_deduplicates(self):
        # A model asked for six angles often gives four and two rephrasings. Paying to fetch the
        # same results twice is pure waste.
        call = model({"THE BRIEF": {"queries": ["a", "A", "b", "a ", "c"]}})
        queries, _ = await rs.shared_queries("brief", n=6, call=call)
        assert [q.text for q in queries] == ["a", "b", "c"]

    async def test_it_respects_the_ceiling(self):
        call = model({"THE BRIEF": {"queries": [f"q{i}" for i in range(20)]}})
        queries, _ = await rs.shared_queries("brief", n=3, call=call)
        assert len(queries) == 3

    async def test_every_shared_query_is_background(self):
        call = model({"THE BRIEF": {"queries": ["a"]}})
        queries, _ = await rs.shared_queries("brief", call=call)
        assert queries[0].intent == "background"


class TestThePrompts:
    """Pinned because each line is load-bearing and softening one is invisible in the output."""

    def test_the_persona_prompt_says_the_opposition_query_is_the_important_one(self):
        text = rs._PERSONA_QUERY_PROMPT.replace("\n", " ")
        assert "The second is the important one" in text
        assert "cannot be moved by evidence" in text
        assert "STATED condition" in text

    def test_the_shared_prompt_forbids_asking_for_a_conclusion(self):
        # "Return the authorities, not the verdict" — §1. Searching the central question is correct;
        # asking the search engine to settle it is not.
        text = rs._SHARED_QUERY_PROMPT.replace("\n", " ")
        assert "Do NOT write queries that ask for a conclusion" in text
        assert "the answer is what the discussion is for" in text

    def test_both_prompts_ask_for_queries_not_questions(self):
        for prompt in (rs._SHARED_QUERY_PROMPT, rs._PERSONA_QUERY_PROMPT):
            assert "not questions" in prompt.replace("\n", " ")

    def test_the_tier_prompt_names_the_distinction_that_matters(self):
        text = rs._TIER_PROMPT.replace("\n", " ")
        assert "QUOTING a statute is commentary" in text
        assert "Over-promoting a blog to controlling is the damaging error" in text


# --------------------------------------------------------------------------- #
# authority
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestTiering:
    def _docs(self):
        return [
            rs.ResearchedDocument(title="ARS 32-2201", text=STATUTE, url="https://azleg.gov/a"),
            rs.ResearchedDocument(title="Law firm blog", text="We think that…",
                                  url="https://firm.example/b"),
        ]

    async def test_it_sets_the_tier_and_the_reason(self):
        docs = self._docs()
        call = model({"Classify each source": {"verdicts": [
            {"n": 0, "tier": "controlling", "reason": "the statute itself"},
            {"n": 1, "tier": "commentary", "reason": "a firm writing about it"},
        ]}})
        await rs.tier_documents(docs, call=call)
        assert [d.authority for d in docs] == ["controlling", "commentary"]
        assert docs[0].authority_reason == "the statute itself"

    async def test_one_call_for_all_of_them(self):
        # Comparison is what the judgement needs — "this one quotes the statute the other one IS" —
        # and per-document calls cannot see it.
        seen = []
        call = model({"Classify": {"verdicts": []}}, record=seen)
        await rs.tier_documents(self._docs(), call=call)
        assert len(seen) == 1
        assert "azleg.gov/a" in seen[0] and "firm.example/b" in seen[0]

    async def test_a_document_the_model_skipped_stays_unknown(self):
        # Defaulting it to `commentary` would be inventing a judgement nobody made.
        docs = self._docs()
        call = model({"Classify": {"verdicts": [{"n": 0, "tier": "controlling", "reason": "x"}]}})
        await rs.tier_documents(docs, call=call)
        assert docs[1].authority == "unknown"

    async def test_an_unusable_reply_leaves_everything_unknown_and_says_so(self, caplog):
        docs = self._docs()
        with caplog.at_level("WARNING"):
            await rs.tier_documents(docs, call=model({}))
        assert all(d.authority == "unknown" for d in docs)
        assert "stay 'unknown'" in caplog.text

    async def test_an_invented_tier_is_ignored(self):
        docs = self._docs()
        call = model({"Classify": {"verdicts": [{"n": 0, "tier": "definitive", "reason": "x"}]}})
        await rs.tier_documents(docs, call=call)
        assert docs[0].authority == "unknown"

    async def test_no_documents_means_no_call(self):
        seen = []
        assert await rs.tier_documents([], call=model({}, record=seen)) == 0.0
        assert seen == []


# --------------------------------------------------------------------------- #
# the documented negative
# --------------------------------------------------------------------------- #


class TestTheDocumentedNegative:
    def _corpus(self):
        c = rs.Corpus(persona="Casey")
        c.queries = [
            rs.Query("state statute specialty plan regulated-only", intent="opposition",
                     viewpoint="not a legend drug"),
            rs.Query("specialty plan federal legend", intent="support"),
        ]
        c.documents = [rs.ResearchedDocument(
            title="Trade press summary", text="x", url="https://press.example/a",
            authority="commentary",
        )]
        c.unreadable = [("https://vetboard.az.gov/statutes-and-rules", "HTTP 403")]
        return c

    def test_no_model_is_consulted_for_a_negative(self):
        # THE guard. A model asked to write this could produce a fluent paragraph describing a search
        # that never happened, and a fabricated negative becomes evidence for a launch decision.
        # `documented_negative` takes no `call` argument at all, so it cannot.
        import inspect

        assert "call" not in inspect.signature(rs.documented_negative).parameters

    def test_it_records_the_queries_that_were_run(self):
        text = rs.documented_negative(self._corpus(), on="2026-09-23")
        assert "state statute specialty plan regulated-only" in text
        assert "for what would change their mind" in text
        assert "2026-09-23" in text

    def test_it_separates_could_not_read_from_does_not_exist(self):
        # A board's own statute page answering 403 is a fact about the search. Conflating it with
        # absence lets "we could not get in" masquerade as "there is nothing there".
        text = rs.documented_negative(self._corpus())
        assert "could NOT be read" in text
        assert "vetboard.az.gov" in text
        assert "not evidence of absence" in text

    def test_it_states_what_it_does_not_support(self):
        text = rs.documented_negative(self._corpus())
        assert "It does not support" in text
        assert "no controlling authority exists" in text

    def test_there_is_no_negative_when_a_controlling_authority_was_found(self):
        c = self._corpus()
        c.documents.append(rs.ResearchedDocument(
            title="ARS 32-2201", text=STATUTE, url="https://azleg.gov/a",
            authority="controlling",
        ))
        assert rs.documented_negative(c) is None

    def test_a_shared_corpus_negative_is_worded_for_the_record(self):
        c = self._corpus()
        c.persona = None
        assert "for the shared record" in rs.documented_negative(c)

    def test_it_says_so_when_nothing_could_be_read_at_all(self):
        c = self._corpus()
        c.documents = []
        assert "no result could be read" in rs.documented_negative(c)


# --------------------------------------------------------------------------- #
# gathering
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestGather:
    def _search(self, mapping, record=None):
        async def search(query, count):
            if record is not None:
                record.append((query, count))
            return mapping.get(query, [])

        return search

    def _fetch(self, mapping, record=None):
        async def fetch(url):
            if record is not None:
                record.append(url)
            return mapping.get(url)

        return fetch

    async def test_a_provider_that_supplies_text_is_not_fetched(self):
        # `supplies_text` exists so this step can be skipped, and fetching is where the risk lives.
        fetched = []
        corpus = rs.Corpus(persona=None, queries=[rs.Query("q")])
        await rs.gather(
            corpus,
            search=self._search({"q": [Hit("https://a", text=STATUTE)]}),
            fetch=self._fetch({}, record=fetched),
            call=model({"Classify": {"verdicts": [{"n": 0, "tier": "controlling", "reason": "r"}]}}),
        )
        assert fetched == [], "a provider with text must not trigger a fetch"
        assert corpus.documents[0].text == STATUTE.strip()  # stored stripped

    async def test_a_snippet_provider_is_fetched(self):
        corpus = rs.Corpus(persona=None, queries=[rs.Query("q")])
        await rs.gather(
            corpus,
            search=self._search({"q": [Hit("https://a")]}),
            fetch=self._fetch({"https://a": Page(STATUTE)}),
            call=model({"Classify": {"verdicts": [{"n": 0, "tier": "controlling", "reason": "r"}]}}),
        )
        assert corpus.documents[0].text == STATUTE.strip()  # stored stripped

    async def test_an_unfetchable_result_becomes_an_unreadable_record(self):
        # So the negative can say a source refused us, rather than implying it held nothing.
        corpus = rs.Corpus(persona=None, queries=[rs.Query("q")])
        await rs.gather(
            corpus,
            search=self._search({"q": [Hit("https://blocked")]}),
            fetch=self._fetch({}),
            call=model({}),
        )
        assert corpus.unreadable == [("https://blocked", "could not be read")]
        assert corpus.negative is not None

    async def test_a_failed_search_loses_that_query_not_the_corpus(self):
        # §5.2: research is additive.
        async def search(query, count):
            if query == "bad":
                raise RuntimeError("rate limited")
            return [Hit("https://a", text=STATUTE)]

        corpus = rs.Corpus(persona=None, queries=[rs.Query("bad"), rs.Query("good")])
        await rs.gather(
            corpus, search=search, fetch=self._fetch({}),
            call=model({"Classify": {"verdicts": [{"n": 0, "tier": "persuasive", "reason": "r"}]}}),
        )
        assert len(corpus.documents) == 1
        assert any("rate limited" in why for _, why in corpus.unreadable)

    async def test_one_page_answering_two_queries_is_corroboration_not_a_duplicate(self):
        corpus = rs.Corpus(persona=None, queries=[rs.Query("q1"), rs.Query("q2")])
        hit = [Hit("https://same", text=STATUTE)]
        await rs.gather(
            corpus, search=self._search({"q1": hit, "q2": hit}), fetch=self._fetch({}),
            call=model({"Classify": {"verdicts": [{"n": 0, "tier": "controlling", "reason": "r"}]}}),
        )
        assert len(corpus.documents) == 1
        assert corpus.documents[0].found_by == ["q1", "q2"]

    async def test_fetching_is_capped_per_query(self):
        # Fetching is the slow part, so this is the number that decides how long a pass takes.
        fetched = []
        corpus = rs.Corpus(persona=None, queries=[rs.Query("q")])
        hits = [Hit(f"https://a/{i}") for i in range(9)]
        await rs.gather(
            corpus, search=self._search({"q": hits}),
            fetch=self._fetch({f"https://a/{i}": Page(STATUTE) for i in range(9)}, record=fetched),
            call=model({"Classify": {"verdicts": []}}),
            fetch_per_query=2,
        )
        assert len(fetched) == 2

    async def test_a_controlling_find_means_no_negative(self):
        corpus = rs.Corpus(persona=None, queries=[rs.Query("q")])
        await rs.gather(
            corpus, search=self._search({"q": [Hit("https://a", text=STATUTE)]}),
            fetch=self._fetch({}),
            call=model({"Classify": {"verdicts": [{"n": 0, "tier": "controlling", "reason": "r"}]}}),
        )
        assert corpus.negative is None
        assert corpus.controlling


@pytest.mark.asyncio
class TestPlanAndGather:
    async def test_it_builds_a_shared_corpus_and_one_per_persona_with_viewpoints(self):
        call = model({
            "THE BRIEF": {"queries": ["background q"]},
            "WHAT WOULD CHANGE": {"support": "s", "opposition": "o"},
            "Classify": {"verdicts": [{"n": 0, "tier": "commentary", "reason": "r"}]},
        })

        async def search(query, count):
            return [Hit("https://a/" + query.replace(" ", ""), text=STATUTE)]

        async def fetch(url):
            return None

        cast = [
            {"name": "Casey", "structured": {"viewpoints": VIEWPOINTS}},
            {"name": "NoViews", "structured": {}},
            {"name": "", "structured": {"viewpoints": VIEWPOINTS}},
        ]
        corpora = await rs.plan_and_gather(
            "the brief", cast, search=search, fetch=fetch, call=call,
        )
        assert [c.persona for c in corpora] == [None, "Casey"], (
            "a persona with no viewpoints gets no corpus, and neither does an unnamed one"
        )

    async def test_the_summary_reports_tiers_and_negatives(self):
        c = rs.Corpus(persona="Casey", queries=[rs.Query("q")])
        c.documents = [rs.ResearchedDocument(title="t", text="x", url="u", authority="commentary")]
        c.unreadable = [("https://blocked", "HTTP 403")]
        c.negative = "..."
        c.cost_usd = 0.05
        text = rs.summarise([c])
        assert "commentary" in text and "UNREADABLE 1" in text
        assert "documented negative" in text
        assert "$0.0500" in text


@pytest.mark.asyncio
class TestOneFailedCallDoesNotLoseThePass:
    """§5.2, violated on the first live run and fixed here.

    A pass makes one call per persona plus one for the brief plus one tiering call per corpus —
    thirteen or more for a six-persona cast. Bedrock answered the SECOND of them with
    `ServiceUnavailableError: Bedrock is unable to process your request`, which is transient, and the
    exception propagated out of `_ask` and lost every query already generated.
    """

    async def test_a_transient_failure_is_retried(self):
        attempts = {"n": 0}

        async def flaky(messages, model=None, temperature=0.0, max_tokens=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("Bedrock is unable to process your request")
            return {"content": json.dumps({"queries": ["worked"]}), "cost_usd": 0.01,
                    "tokens_in": 1, "tokens_out": 1}

        queries, _ = await rs.shared_queries("brief", call=flaky)
        assert [q.text for q in queries] == ["worked"]
        assert attempts["n"] == 2

    async def test_a_persistent_failure_returns_nothing_rather_than_raising(self, caplog):
        async def always_fails(messages, model=None, temperature=0.0, max_tokens=None):
            raise RuntimeError("down")

        with caplog.at_level("WARNING"):
            queries, _ = await rs.shared_queries("brief", call=always_fails)
        assert queries == []
        assert "rather than losing the pass" in caplog.text

    async def test_one_persona_failing_does_not_lose_the_others(self):
        # The shape of the live failure: the whole pass died on one persona's call.
        calls = {"n": 0}

        async def flaky(messages, model=None, temperature=0.0, max_tokens=None):
            calls["n"] += 1
            if "Casey" in messages[0]["content"]:
                raise RuntimeError("down")
            return {"content": json.dumps({"support": "s", "opposition": "o"}),
                    "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}

        cast = [
            {"name": "Casey", "structured": {"viewpoints": VIEWPOINTS}},
            {"name": "Jordan", "structured": {"viewpoints": VIEWPOINTS}},
        ]

        async def search(q, c):
            return []

        async def fetch(u):
            return None

        corpora = await rs.plan_and_gather("brief", cast, search=search, fetch=fetch, call=flaky)
        by_name = {c.persona: c for c in corpora}

        assert by_name["Jordan"].queries, "Jordan's queries survived Casey's failure"
        # And Casey is still THERE, with the failure recorded. Dropping her would leave one corpus
        # for two personas and nothing saying why — the silent short-count the ensemble work
        # refused, where a missing member is reported so the denominator stays honest.
        assert "Casey" in by_name
        assert by_name["Casey"].queries == []
        assert any("could not be generated" in why for _, why in by_name["Casey"].unreadable)
        assert by_name["Casey"].negative is not None, (
            "a persona whose research failed gets a documented negative saying so"
        )

    async def test_retrying_does_not_lose_the_cost_already_spent(self):
        # A retried call that eventually fails still cost money on each attempt, and a pass that
        # under-reports its spend makes the cap under-report too.
        async def fails_after_charging(messages, model=None, temperature=0.0, max_tokens=None):
            return {"content": "", "cost_usd": 0.03, "tokens_in": 1, "tokens_out": 0,
                    "finish_reason": "length"}

        _, cost = await rs.shared_queries("brief", call=fails_after_charging)
        assert cost == pytest.approx(0.03)


@pytest.mark.asyncio
class TestPersonaQueriesStayInTheDomain:
    """Measured drift, from the first live run.

    Quinn's viewpoint mentions disclosure and financial friction, and the model produced "CMS
    guidance minimum necessary patient financial responsibility revenue cycle" — US HUMAN healthcare
    billing. Avery's produced "medical practice act" rather than licensed. Cause: the persona prompt
    received the position and the shifting condition but never the SUBJECT, so a generically-worded
    viewpoint made the model guess a domain, and it guessed the field those words are commonest in.
    """

    async def test_the_brief_reaches_the_prompt(self):
        seen = []
        call = model({"WHAT WOULD CHANGE": {"support": "a", "opposition": "b"}}, record=seen)
        await rs.persona_queries(
            "Quinn", VIEWPOINTS, brief="licensed specialty plan renewal", call=call,
        )
        assert "licensed specialty plan renewal" in seen[0]

    async def test_the_prompt_names_the_drift_it_is_preventing(self):
        text = rs._PERSONA_QUERY_PROMPT.replace("\n", " ")
        assert "Stay in the domain above" in text
        assert "human healthcare" in text, "the observed drift is named, not implied"

    async def test_a_missing_brief_is_stated_rather_than_left_blank(self):
        # An empty domain block would read as "no constraint"; saying so is honest and keeps the
        # prompt's shape stable.
        seen = []
        call = model({"WHAT WOULD CHANGE": {"support": "a", "opposition": "b"}}, record=seen)
        await rs.persona_queries("Casey", VIEWPOINTS, call=call)
        assert "(not supplied)" in seen[0]


class TestTheOutputBudget:
    def test_it_is_sized_for_reasoning_not_for_the_answer(self):
        # A persona's two queries are ~80 tokens. Asked with a 1000-token budget on the first live
        # run, one call returned finish=length with EMPTY content — the fourth time this project has
        # been caught by a truncated reply returning nothing, and the second time by forgetting that
        # Sonnet 5 spends most of an output budget reasoning (~29,000 tokens for ~1,100 of answer,
        # measured in the clustering work).
        assert rs.ASK_MAX_TOKENS >= 8000


class TestANegativeNeverOverstatesTheSearch:
    """Measured on a live run, and the worst failure this module could have shipped.

    Query generation failed, so nothing was searched — and the negative went on to say "this document
    records a search that did not find a statute". A fluent, confident account of work that never
    happened, which is exactly the overstatement §4 exists to prevent, arrived at from the inside
    rather than from a model. It would have entered a corpus as evidence for a launch decision.
    """

    def test_with_no_queries_it_says_nobody_looked(self):
        c = rs.Corpus(persona=None)
        c.unreadable = [("(queries for the brief)", "could not be generated")]
        text = rs.documented_negative(c, on="2026-09-23")

        assert "could not be carried out" in text
        assert "Nobody looked" in text
        assert "says nothing whatsoever about the law" in text
        # And it must NOT claim a search.
        assert "did not find a statute" not in text
        assert "this search surfaced no controlling authority" not in text

    def test_it_gives_the_reason(self):
        c = rs.Corpus(persona="Casey")
        c.unreadable = [("(queries for Casey)", "could not be generated")]
        assert "could not be generated" in rs.documented_negative(c)

    def test_it_copes_with_no_reason_recorded(self):
        # Better a negative that admits the reason is missing than one that invents a search.
        text = rs.documented_negative(rs.Corpus(persona=None))
        assert "no reason was captured" in text
        assert "Nobody looked" in text

    def test_with_queries_it_still_reports_the_search(self):
        # The ordinary path is unchanged: queries were run, nothing controlling came back.
        c = rs.Corpus(persona=None, queries=[rs.Query("a real query")])
        text = rs.documented_negative(c)
        assert "did not find a statute" in text
        assert "a real query" in text
        assert "Nobody looked" not in text


class TestRetryPatience:
    def test_it_waits_long_enough_for_a_capacity_error(self):
        # Bedrock answered `ServiceUnavailableError` on two of three live runs. Six seconds of total
        # backoff was not patient enough, and the cost of giving up is a whole corpus missing.
        total = sum(rs.ASK_BACKOFF_S * i for i in range(1, rs.ASK_ATTEMPTS))
        assert total >= 15, f"only {total}s of total backoff"


@pytest.mark.asyncio
class TestAThinSourceIsNotADocument:
    """From real Tavily results, and the same failure `webfetch` already floors from the other side.

    Tavily returns its own ~150-character summary when its extraction fails, and `TavilySearch`
    deliberately falls back to that summary because `supplies_text` promises the caller no fetcher is
    needed. So `assoc.org/KB/.../PCR.aspx` came back as 156 characters and entered the corpus as a
    document — a source that could not be read, counted as one that was. Exactly the Incapsula
    block-page problem, unguarded on the provider path.
    """

    async def _gathered(self, hit):
        corpus = rs.Corpus(persona=None, queries=[rs.Query("q")])

        async def search(q, c):
            return [hit]

        async def fetch(u):
            return None

        await rs.gather(corpus, search=search, fetch=fetch, call=model({}))
        return corpus

    async def test_a_summary_sized_result_is_refused(self):
        corpus = await self._gathered(Hit("https://assoc.org/pcr", text="x" * 156))
        assert corpus.documents == []
        assert any("a summary, not the source" in why for _, why in corpus.unreadable)

    async def test_it_is_recorded_as_unreadable_not_silently_dropped(self):
        # So the negative can say a source was SEEN and not obtained. Dropping it would make the
        # search look like it never surfaced the ASSOC at all.
        corpus = await self._gathered(Hit("https://assoc.org/pcr", text="x" * 156))
        assert corpus.unreadable[0][0] == "https://assoc.org/pcr"
        assert "assoc.org" in corpus.negative

    async def test_a_full_page_is_kept(self):
        corpus = await self._gathered(Hit("https://azleg.gov/ars", text=STATUTE))
        assert len(corpus.documents) == 1

    async def test_the_floor_matches_the_fetcher_s(self):
        # Two floors for the same reason in two modules; if they drift, one path admits what the
        # other refuses and the corpus depends on which provider was configured.
        from matrix_studio import webfetch

        assert rs.MIN_DOCUMENT_CHARS == webfetch.MIN_TEXT_CHARS


@pytest.mark.asyncio
class TestIngest:
    """Writing a corpus into a knowledge base that already exists."""

    async def _kb(self, db):
        return (await db.create_knowledge_base(name="provider law", description="d"))["id"]

    def _corpus(self):
        c = rs.Corpus(persona="Casey", queries=[rs.Query("q")])
        c.documents = [
            rs.ResearchedDocument(title="ARS 32-2201", text=STATUTE,
                                  url="https://azleg.gov/ars/32/02201.htm",
                                  authority="controlling"),
            rs.ResearchedDocument(title="A blog", text=STATUTE,
                                  url="https://firm.example/x", authority="commentary"),
        ]
        return c

    async def test_documents_are_marked_researched_with_their_tier_and_url(self, db):
        kb_id = await self._kb(db)
        out = await rs.ingest(db, self._corpus(), kb_id, batch="b1")

        assert len(out["written"]) == 2
        docs = {d["title"]: d for d in await db.list_kb_documents(kb_id)}
        assert docs["ARS 32-2201"]["origin"] == "researched"
        assert docs["ARS 32-2201"]["authority"] == "controlling"
        assert docs["ARS 32-2201"]["source_path"] == "https://azleg.gov/ars/32/02201.htm"
        assert docs["ARS 32-2201"]["research_batch"] == "b1"

    async def test_the_negative_is_ingested_so_the_room_can_retrieve_it(self, db):
        # A conversation that cannot find a statute should be able to retrieve the RECORD of the
        # search that looked for one, rather than re-litigating the gap every run — which is exactly
        # what five replicate runs did.
        kb_id = await self._kb(db)
        c = self._corpus()
        c.documents = [c.documents[1]]          # commentary only, so a negative exists
        c.negative = rs.documented_negative(c)
        out = await rs.ingest(db, c, kb_id, batch="b1")

        assert out["negative"] is True
        titles = [d["title"] for d in await db.list_kb_documents(kb_id)]
        assert "No controlling authority found — Casey" in titles

    async def test_the_negative_is_not_tiered_as_commentary(self, db):
        # It is not a source ABOUT the law; it is a record of a search. Tiering it `commentary` would
        # let the retrieval floor treat it as one.
        kb_id = await self._kb(db)
        c = self._corpus()
        c.documents = []
        c.negative = rs.documented_negative(c)
        await rs.ingest(db, c, kb_id, batch="b1")
        doc = (await db.list_kb_documents(kb_id))[0]
        assert doc["authority"] == "unknown"

    async def test_a_second_pass_replaces_the_first_and_spares_curation(self, db):
        kb_id = await self._kb(db)
        await db.add_kb_document(kb_id, title="curated by hand", text="c" * 300)
        await rs.ingest(db, self._corpus(), kb_id, batch="b1")

        out = await rs.ingest(db, self._corpus(), kb_id, batch="b2")
        assert len(out["replaced"]) == 2

        docs = await db.list_kb_documents(kb_id)
        assert sum(1 for d in docs if d["title"] == "curated by hand") == 1
        assert {d["research_batch"] for d in docs if d["origin"] == "researched"} == {"b2"}

    async def test_one_document_failing_does_not_lose_the_others(self, db, monkeypatch):
        kb_id = await self._kb(db)
        real = db.add_kb_document
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("s3 hiccup")
            return await real(*a, **k)

        monkeypatch.setattr(db, "add_kb_document", flaky)
        out = await rs.ingest(db, self._corpus(), kb_id, batch="b1")
        assert len(out["written"]) == 1

    async def test_it_never_creates_a_knowledge_base(self, db):
        # The caller owns that decision, because a read grant is not a write grant and `_owned_kb` is
        # where ownership is settled. Ingesting into a missing KB must fail, not conjure one.
        out = await rs.ingest(db, self._corpus(), "does-not-exist", batch="b1")
        assert out["written"] == []


# --------------------------------------------------------------------------- #
# Per-query negatives — PERSONA-RESEARCH.md §9.3
# --------------------------------------------------------------------------- #
#
# §9 run 1 measured the corpus-level negative firing 0 times in 159 retrieval turns: every scope
# found SOME controlling authority, and the corpus negative is written only when a corpus finds
# none. The room's recurring question was narrower — "has any board acted on plan renewal?"
# — and nothing ever said "we searched for that and there is none". These pin the fix.


def _corpus_with(found):
    """A corpus whose documents were found by the given queries. `found`: [(title, tier, [query])]."""
    c = rs.Corpus(persona="Dr. Jordan")
    texts = sorted({q for _t, _a, qs in found for q in qs} | {"board enforcement plan renewal"})
    c.queries = [rs.Query(text=t, intent="opposition") for t in texts]
    c.documents = [
        rs.ResearchedDocument(title=t, text="x" * 400, url=f"https://ex/{i}", authority=a,
                              found_by=list(qs))
        for i, (t, a, qs) in enumerate(found)
    ]
    return c


def test_a_query_with_no_controlling_result_gets_its_own_negative():
    c = _corpus_with([
        ("Texas Occupations Code Ch. 801", "controlling", ["practice act continuation of treatment"]),
        ("Law firm blog on plan rules", "commentary", ["board enforcement plan renewal"]),
    ])
    out = rs.query_negatives(c, on="2026-09-25")
    assert [q.text for q, _t in out] == ["board enforcement plan renewal"]
    text = out[0][1]
    # The question's own words in the heading — what a turn asking it can be matched against.
    assert text.startswith(f"# {rs.NEGATIVE_TITLE} — board enforcement plan renewal")
    # And what the search DID find, with its tier, so the record is checkable.
    assert "[commentary] Law firm blog on plan rules" in text


def test_a_query_that_did_find_controlling_authority_gets_none():
    c = _corpus_with([
        ("Texas Occupations Code Ch. 801", "controlling", ["practice act continuation of treatment"]),
        ("Board Policy 23-01", "controlling", ["board enforcement plan renewal"]),
    ])
    assert rs.query_negatives(c) == []


def test_a_corpus_with_no_controlling_authority_is_left_to_the_corpus_negative():
    # Emitting both would put the same absence in the prompt twice.
    c = _corpus_with([("A blog", "commentary", ["board enforcement plan renewal"])])
    assert rs.documented_negative(c) is not None
    assert rs.query_negatives(c) == []


def test_a_query_whose_SEARCH_FAILED_gets_no_negative():
    """"Nobody looked" must never be written up as "we looked and there is nothing".

    The rule `documented_negative`'s no-queries branch already enforces, for the same reason: a
    negative's value is that it describes work actually done, and it enters a corpus as evidence.
    """
    c = _corpus_with([
        ("Texas Occupations Code Ch. 801", "controlling", ["practice act continuation of treatment"]),
    ])
    c.unreadable.append(("(query: board enforcement plan renewal)", "search failed: 429"))
    assert rs.query_negatives(c) == []


def test_a_query_that_returned_nothing_readable_says_so():
    c = _corpus_with([
        ("Texas Occupations Code Ch. 801", "controlling", ["practice act continuation of treatment"]),
    ])
    out = rs.query_negatives(c)
    assert len(out) == 1
    assert "nothing that could be read" in out[0][1]


def test_a_query_negative_never_claims_that_none_exists():
    c = _corpus_with([
        ("Texas Occupations Code Ch. 801", "controlling", ["practice act continuation of treatment"]),
    ])
    text = rs.query_negatives(c)[0][1]
    assert "It does not support: *none exists.*" in text


def test_a_query_negative_stays_one_chunk_even_with_many_sources():
    """Short on purpose: a turn that retrieves it should get the whole record, not a slice of it."""
    many = [(f"Blog {i}", "commentary", ["board enforcement plan renewal"]) for i in range(30)]
    c = _corpus_with(many + [("Statute", "controlling", ["practice act continuation of treatment"])])
    text = rs.query_negatives(c)[0][1]
    assert "…and 24 more, none controlling" in text
    assert len(text) < 1500, len(text)


def test_no_model_is_consulted_for_a_query_negative():
    # Computed from facts the module holds. The signature is the guard: there is no `call` to pass.
    import inspect
    assert "call" not in inspect.signature(rs.query_negatives).parameters


async def test_ingest_writes_each_query_negative_as_its_own_document():
    c = _corpus_with([
        ("Texas Occupations Code Ch. 801", "controlling", ["practice act continuation of treatment"]),
    ])
    c.query_negatives = rs.query_negatives(c)
    written = []

    class Store:
        async def add_kb_document(self, kb_id, **kw):
            written.append(kw)
            return f"d{len(written)}"

        async def replace_research_documents(self, kb_id, **kw):
            return []

    await rs.ingest(Store(), c, "kb1", batch="b1")
    negs = [w for w in written if w["title"].startswith(rs.NEGATIVE_TITLE)]
    assert len(negs) == 1
    assert negs[0]["title"] == f"{rs.NEGATIVE_TITLE} — board enforcement plan renewal"
    # A record of a search, not a source about the law — the floor must not treat it as one.
    assert negs[0]["authority"] == "unknown"
    assert negs[0]["origin"] == "researched"
    assert negs[0]["research_batch"] == "b1"
