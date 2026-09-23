# SPDX-License-Identifier: Apache-2.0
"""
Three search providers behind one interface, and the selection between them.

## What these tests are really protecting

**`supplies_text` must stay honest.** Brave returns about a hundred characters of description;
Tavily and Exa return extracted page content. That difference decides whether the caller has to
fetch pages itself, and fetching is where the risk lives — egress, per-site terms, SSRF
(`docs/PERSONA-RESEARCH.md` §12.2). A wrapper that copied a snippet into `text` would hand a caller
a hundred characters where it expected a statute, and the failure would look like a bad source
rather than a missing fetch. So `text is None` for Brave is asserted, not incidental.

**Selection order is a security argument, not a taste.** Text-supplying providers come first
because they remove the fetcher entirely. An explicit `SEARCH_PROVIDER` that cannot be honoured is
an ERROR rather than a fallback, because falling back would defeat a deliberate choice to avoid
fetching by quietly selecting the provider that requires it.

**Only Brave is verified against the live API.** The Tavily and Exa response fixtures here are
documentation-shaped, not observed, and the tests exist so a mismatch surfaces as a parse failure
naming the provider rather than as an empty result list that reads like "the web had nothing".
"""

import pytest

from matrix_studio import websearch as ws


# Brave's real response shape, trimmed from the live call that verified the key on 2026-09-23.
# The <strong> markup is real and is why the snippet is stripped.
BRAVE_BODY = {
    "web": {
        "results": [
            {
                "title": "California Licensed Medicine Practice Act",
                "url": "https://vmb.ca.gov/applicants/practice_act.shtml",
                "description": "Article 3. Scope of Practice and <strong>Exemptions</strong>, "
                               "§ 4067",
                "language": "en",
            },
            {"title": "No URL here", "description": "should be dropped"},
        ]
    }
}

TAVILY_BODY = {
    "results": [
        {
            "title": "CA Practice Act",
            "url": "https://vmb.ca.gov/laws_regs/",
            "content": "Tavily's own summary of the page.",
            "raw_content": "The full extracted statute text, which is what we want.",
            "score": 0.94,
            "published_date": "2024-02-01",
        }
    ]
}

EXA_BODY = {
    "results": [
        {
            "title": "CA Practice Act",
            "url": "https://vmb.ca.gov/laws_regs/",
            "text": "The full extracted statute text from Exa." * 20,
            "publishedDate": "2024-02-01",
            "score": 0.81,
        }
    ]
}


def fake_http(body, *, record=None):
    """Stands in for one HTTP request. Records what was sent, so headers can be asserted."""

    async def call(method, url, *, headers=None, params=None, json_body=None):
        if record is not None:
            record.append({"method": method, "url": url, "headers": headers or {},
                           "params": params, "json": json_body})
        return body

    return call


# --------------------------------------------------------------------------- #
# per-provider parsing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestBrave:
    async def test_it_parses_the_shape_the_live_api_returned(self):
        sent = []
        results = await ws.BraveSearch("k").search("q", count=5, call=fake_http(BRAVE_BODY, record=sent))

        assert len(results) == 1, "the entry with no URL is dropped rather than carried as empty"
        r = results[0]
        assert r.url == "https://vmb.ca.gov/applicants/practice_act.shtml"
        assert r.provider == "brave"
        assert sent[0]["method"] == "GET"
        assert sent[0]["params"] == {"q": "q", "count": 5}

    async def test_text_is_None_because_brave_supplies_none(self):
        # THE assertion of this file. `None` means "go and fetch"; a snippet copied into `text`
        # would give a caller 100 characters where it expected a statute, and the failure would
        # look like a bad source rather than a missing fetch step.
        results = await ws.BraveSearch("k").search("q", call=fake_http(BRAVE_BODY))
        assert results[0].text is None
        assert ws.BraveSearch.supplies_text is False
        assert results[0].snippet, "the snippet is still worth having, for ranking"

    async def test_match_markup_is_stripped_because_this_text_reaches_a_prompt(self):
        results = await ws.BraveSearch("k").search("q", call=fake_http(BRAVE_BODY))
        assert "<strong>" not in results[0].snippet
        assert "Exemptions" in results[0].snippet

    async def test_the_key_travels_in_the_header_and_not_the_query(self):
        # A key in a query string lands in access logs and in Referer headers.
        sent = []
        await ws.BraveSearch("sekrit").search("q", call=fake_http(BRAVE_BODY, record=sent))
        assert sent[0]["headers"]["X-Subscription-Token"] == "sekrit"
        assert "sekrit" not in str(sent[0]["params"])


@pytest.mark.asyncio
class TestTavily:
    async def test_it_prefers_the_extraction_over_the_summary(self):
        # `content` is Tavily's paraphrase and `raw_content` is the extraction. This corpus is meant
        # to be citable, and a paraphrase is not.
        results = await ws.TavilySearch("k").search("q", call=fake_http(TAVILY_BODY))
        assert results[0].text == "The full extracted statute text, which is what we want."
        assert results[0].snippet == "Tavily's own summary of the page."

    async def test_it_falls_back_to_the_summary_rather_than_to_None(self):
        # `supplies_text` has promised the caller no fetcher is needed, so returning None here would
        # break that contract for one result and send the caller down a path it does not have.
        body = {"results": [{"title": "t", "url": "https://x", "content": "summary only"}]}
        results = await ws.TavilySearch("k").search("q", call=fake_http(body))
        assert results[0].text == "summary only"

    async def test_it_asks_for_raw_content_and_advanced_depth(self):
        # Without both, Tavily is a snippet provider — which is what choosing it was meant to avoid.
        sent = []
        await ws.TavilySearch("k").search("q", count=3, call=fake_http(TAVILY_BODY, record=sent))
        assert sent[0]["json"]["include_raw_content"] is True
        assert sent[0]["json"]["search_depth"] == "advanced"
        assert sent[0]["json"]["max_results"] == 3

    async def test_the_key_is_a_bearer_header(self):
        sent = []
        await ws.TavilySearch("sekrit").search("q", call=fake_http(TAVILY_BODY, record=sent))
        assert sent[0]["headers"]["Authorization"] == "Bearer sekrit"
        assert "sekrit" not in str(sent[0]["json"])


@pytest.mark.asyncio
class TestExa:
    async def test_it_returns_text_and_derives_a_snippet_from_it(self):
        # Exa has no snippet field, so the head of the text stands in — a caller wanting a preview
        # should not have to carry the whole page.
        results = await ws.ExaSearch("k").search("q", call=fake_http(EXA_BODY))
        assert results[0].text.startswith("The full extracted statute text from Exa.")
        assert len(results[0].snippet) == 300

    async def test_a_page_with_no_text_is_empty_not_None(self):
        # `""` and `None` mean different things: this provider supplies text, so an empty page is
        # "there was nothing here", not "go and fetch it".
        body = {"results": [{"title": "t", "url": "https://x"}]}
        results = await ws.ExaSearch("k").search("q", call=fake_http(body))
        assert results[0].text == ""
        assert results[0].text is not None

    async def test_it_does_not_force_neural_search(self):
        # A statute lookup is a keyword query. Forcing neural on one returns thematically related
        # commentary instead of the statute.
        sent = []
        await ws.ExaSearch("k").search("q", call=fake_http(EXA_BODY, record=sent))
        assert sent[0]["json"]["type"] == "auto"
        assert sent[0]["json"]["contents"] == {"text": True}

    async def test_the_key_is_an_x_api_key_header(self):
        sent = []
        await ws.ExaSearch("sekrit").search("q", call=fake_http(EXA_BODY, record=sent))
        assert sent[0]["headers"]["x-api-key"] == "sekrit"


@pytest.mark.asyncio
class TestEveryProviderIsRobustToJunk:
    @pytest.mark.parametrize(
        "provider", [ws.BraveSearch, ws.TavilySearch, ws.ExaSearch], ids=lambda p: p.name
    )
    @pytest.mark.parametrize("body", [{}, None, {"results": None}, {"web": {}},
                                      {"results": ["not a dict"]}])
    async def test_a_malformed_body_is_no_results_rather_than_a_crash(self, provider, body):
        # A provider changing its response shape must degrade to an empty corpus, which the run
        # survives (§5.2), rather than to an exception that loses the whole research state.
        assert await provider("k").search("q", call=fake_http(body)) == []

    @pytest.mark.parametrize(
        "provider", [ws.BraveSearch, ws.TavilySearch, ws.ExaSearch], ids=lambda p: p.name
    )
    async def test_count_is_respected_as_a_ceiling(self, provider):
        body = {"results": [{"url": f"https://x/{i}", "title": "t", "text": "x", "content": "c"}
                            for i in range(9)]}
        body["web"] = {"results": body["results"]}
        assert len(await provider("k").search("q", count=2, call=fake_http(body))) == 2

    @pytest.mark.parametrize(
        "provider", [ws.BraveSearch, ws.TavilySearch, ws.ExaSearch], ids=lambda p: p.name
    )
    async def test_no_key_is_refused_at_construction(self, provider):
        with pytest.raises(ws.SearchUnavailable, match="no API key"):
            provider("")


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


class TestSelection:
    """The order is a security argument: text-supplying providers remove the fetcher entirely."""

    def test_text_suppliers_come_before_snippet_ones(self):
        names = [p.name for p in ws.PROVIDERS]
        assert names.index("tavily") < names.index("brave")
        assert names.index("exa") < names.index("brave")
        # And the claim the order rests on.
        assert ws.TavilySearch.supplies_text and ws.ExaSearch.supplies_text
        assert not ws.BraveSearch.supplies_text

    def test_it_picks_the_only_key_present(self):
        assert ws.select(env={"BRAVE_API_KEY": "k"}).name == "brave"
        assert ws.select(env={"EXA_API_KEY": "k"}).name == "exa"

    def test_with_several_keys_it_prefers_one_that_avoids_fetching(self):
        chosen = ws.select(env={"BRAVE_API_KEY": "k", "TAVILY_API_KEY": "k", "EXA_API_KEY": "k"})
        assert chosen.name == "tavily"
        assert chosen.supplies_text

    def test_an_explicit_choice_wins_over_the_order(self):
        chosen = ws.select("brave", env={"BRAVE_API_KEY": "k", "TAVILY_API_KEY": "k"})
        assert chosen.name == "brave"

    def test_the_env_var_form_of_an_explicit_choice_also_wins(self):
        chosen = ws.select(env={"SEARCH_PROVIDER": "exa", "EXA_API_KEY": "k",
                                "TAVILY_API_KEY": "k"})
        assert chosen.name == "exa"

    def test_an_explicit_choice_with_no_key_is_an_error_not_a_fallback(self):
        # Falling back here would defeat the point of asking: an operator who named the
        # text-supplying provider to avoid a fetcher would silently get the one that needs it.
        with pytest.raises(ws.SearchUnavailable, match="TAVILY_API_KEY is not set"):
            ws.select("tavily", env={"BRAVE_API_KEY": "k"})

    def test_an_unknown_name_lists_the_known_ones(self):
        with pytest.raises(ws.SearchUnavailable, match="brave"):
            ws.select("bing", env={"BRAVE_API_KEY": "k"})

    def test_no_key_at_all_raises_and_names_every_variable(self):
        # Raising rather than returning None: research is opt-in, so getting here means somebody
        # asked for it, and an empty corpus is a worse answer than a refusal.
        with pytest.raises(ws.SearchUnavailable) as err:
            ws.select(env={})
        for var in ("BRAVE_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY"):
            assert var in str(err.value)

    def test_choosing_a_snippet_provider_is_logged_with_the_way_out(self, caplog):
        with caplog.at_level("INFO"):
            ws.select(env={"BRAVE_API_KEY": "k"})
        assert "fetched separately" in caplog.text
        assert "TAVILY_API_KEY" in caplog.text

    def test_available_reports_what_is_configured_in_order(self, monkeypatch):
        for var in ("BRAVE_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        monkeypatch.setenv("EXA_API_KEY", "k")
        assert ws.available() == ["exa", "brave"]
