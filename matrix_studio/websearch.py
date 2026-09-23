# SPDX-License-Identifier: Apache-2.0
"""
Web search behind one interface, for pre-conversation persona research.

See `docs/PERSONA-RESEARCH.md`. This module only *finds* things; ingesting them into a knowledge
base is `documents.ingest_text`, and deciding what to search for is the researcher's job.

## The one distinction the interface refuses to hide

**Some providers return the page text and some return a snippet.** Brave gives a title, a URL and
about a hundred characters of description — enough to rank a result, nowhere near enough to cite a
statute. Tavily and Exa return extracted content.

That difference decides whether the caller must fetch pages itself, and fetching is where the real
risk lives (§12.2: egress, per-site terms, SSRF). So `SearchResult.text` is `None` rather than
faked when a provider has none, and `Provider.supplies_text` says so up front. A wrapper that
papered over it — say, by copying the snippet into `text` — would hand a caller a hundred
characters where it expected a statute and make the failure look like a bad source rather than a
missing fetch.

## Choosing a provider

By which key is present, with a documented precedence and an explicit override. The order is not
alphabetical and not a quality judgement: **providers that supply text come first, because they
remove the need for a fetcher altogether.** That is a security argument, not a preference.

## Verification status, stated because only one of these is verified

  Brave   VERIFIED 2026-09-23 against the live API. HTTP 200; the request shape, headers and
          response path below are what it actually returned.
  Tavily  UNVERIFIED. Request and response shapes are from documentation, not observation.
  Exa     UNVERIFIED, likewise.

The two unverified providers are pinned by tests against the shape this module *expects*, so a
mismatch surfaces as a parse failure naming the provider rather than as an empty result list that
reads like "the web had nothing". Do not treat their presence here as evidence they work.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Default results per query. Small on purpose: research runs one query per viewpoint per persona,
#: so the count multiplies fast, and a long tail of low-ranked results is mostly commentary.
DEFAULT_COUNT = 5

#: Seconds for one search call. Generous enough for a cold provider, short enough that a hanging
#: search cannot consume a research state's whole budget.
SEARCH_TIMEOUT_S = 20.0


class SearchUnavailable(RuntimeError):
    """No provider is configured, or the configured one refused the request."""


@dataclass
class SearchResult:
    """One hit. `text` is the extracted page content, or None if the provider has none.

    `None` and `""` mean different things and both occur: `None` is "this provider does not supply
    text, go and fetch it", while `""` is "it supplies text and this page had none". A caller
    deciding whether to fetch must be able to tell those apart.
    """

    url: str
    title: str
    snippet: str = ""
    text: Optional[str] = None
    published: Optional[str] = None
    provider: str = ""
    #: Whatever else the provider returned, kept for the authority tiering in §3 to read without
    #: this module having to know what any of it means.
    extra: Dict[str, Any] = field(default_factory=dict)


class Provider:
    """One search backend.

    `supplies_text` is part of the contract rather than an implementation detail: the caller's
    fetch-or-not decision depends on it, and getting it wrong silently produces corpora of
    hundred-character snippets.
    """

    name: str = ""
    supplies_text: bool = False
    env_var: str = ""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise SearchUnavailable(f"{self.name}: no API key")
        self._key = api_key

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, call: Optional[Any] = None
    ) -> List[SearchResult]:
        raise NotImplementedError

    # --------------------------------------------------------------------- #

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        call: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """The single HTTP seam, so the whole module is testable without a network.

        `call` mirrors `analysis._acompletion`'s injection point and for the same reason: one
        function to patch, so no test in this project needs to stand up a fake server.
        """
        if call is not None:
            return await call(
                method, url, headers=headers, params=params, json_body=json_body,
            )

        import httpx

        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S) as client:
            response = await client.request(
                method, url, headers=headers, params=params, json=json_body,
            )
            if response.status_code == 429:
                # Called out separately because it is the one an operator can act on, and because
                # Brave's free tier is one query per second — which a research pass over six
                # personas will hit immediately if it fans out without pacing.
                raise SearchUnavailable(
                    f"{self.name}: rate limited (429). Pace the queries or raise the plan."
                )
            if response.status_code >= 400:
                # The body may echo the key back on some providers, so it is NOT included.
                raise SearchUnavailable(
                    f"{self.name}: HTTP {response.status_code} from {url}"
                )
            return response.json()


class BraveSearch(Provider):
    """Brave Search API. VERIFIED against the live API on 2026-09-23.

    Returns `description`, not page text: measured at about a hundred characters. The caller must
    fetch pages itself — see §12.2 for what that obliges.
    """

    name = "brave"
    supplies_text = False
    env_var = "BRAVE_API_KEY"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, call: Optional[Any] = None
    ) -> List[SearchResult]:
        body = await self._request(
            "GET",
            self.endpoint,
            headers={"Accept": "application/json", "X-Subscription-Token": self._key},
            params={"q": query, "count": count},
            call=call,
        )
        results = ((body or {}).get("web") or {}).get("results") or []
        out: List[SearchResult] = []
        for r in results[:count]:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            out.append(
                SearchResult(
                    url=str(r["url"]),
                    title=str(r.get("title") or ""),
                    # Brave marks matched terms with <strong>. Stripped here rather than left for
                    # every caller to remember, since this text can reach a prompt.
                    snippet=_strip_tags(str(r.get("description") or "")),
                    text=None,
                    published=r.get("age") or r.get("page_age"),
                    provider=self.name,
                    extra={k: r[k] for k in ("profile", "meta_url", "language") if k in r},
                )
            )
        return out


class TavilySearch(Provider):
    """Tavily. UNVERIFIED — shapes are from documentation, not observation.

    Asks for `include_raw_content`, which is what makes a fetcher unnecessary. `content` is
    Tavily's own summary of the page and `raw_content` is the extraction; the extraction is
    preferred and the summary is the fallback, because a summary is a model's paraphrase and this
    corpus is meant to be citable.
    """

    name = "tavily"
    supplies_text = True
    env_var = "TAVILY_API_KEY"
    endpoint = "https://api.tavily.com/search"

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, call: Optional[Any] = None
    ) -> List[SearchResult]:
        body = await self._request(
            "POST",
            self.endpoint,
            # Bearer, which is Tavily's current scheme. Older clients put the key in the body;
            # if this 401s against a live key, that is the first thing to try.
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json"},
            json_body={
                "query": query,
                "max_results": count,
                "include_raw_content": True,
                # Advanced costs more and is the point: basic returns snippets, and a snippet
                # provider is what we chose Tavily to avoid being.
                "search_depth": "advanced",
            },
            call=call,
        )
        out: List[SearchResult] = []
        for r in ((body or {}).get("results") or [])[:count]:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            raw = r.get("raw_content")
            summary = str(r.get("content") or "")
            out.append(
                SearchResult(
                    url=str(r["url"]),
                    title=str(r.get("title") or ""),
                    snippet=summary,
                    # Prefer the extraction; fall back to the summary rather than to None, because
                    # `supplies_text` has promised the caller it will not need a fetcher.
                    text=str(raw) if raw else summary,
                    published=r.get("published_date"),
                    provider=self.name,
                    extra={"score": r["score"]} if "score" in r else {},
                )
            )
        return out


class ExaSearch(Provider):
    """Exa. UNVERIFIED — shapes are from documentation, not observation.

    `type: auto` lets Exa choose between neural and keyword search. Neural is its selling point,
    but a statute lookup is a keyword query and forcing neural on one is how you get thematically
    related commentary instead of the statute.
    """

    name = "exa"
    supplies_text = True
    env_var = "EXA_API_KEY"
    endpoint = "https://api.exa.ai/search"

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, call: Optional[Any] = None
    ) -> List[SearchResult]:
        body = await self._request(
            "POST",
            self.endpoint,
            headers={"x-api-key": self._key, "Content-Type": "application/json"},
            json_body={
                "query": query,
                "numResults": count,
                "type": "auto",
                "contents": {"text": True},
            },
            call=call,
        )
        out: List[SearchResult] = []
        for r in ((body or {}).get("results") or [])[:count]:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            text = r.get("text")
            out.append(
                SearchResult(
                    url=str(r["url"]),
                    title=str(r.get("title") or ""),
                    # Exa has no snippet field; the head of the text stands in, so a caller that
                    # only wants a preview does not have to carry the whole page.
                    snippet=(str(text)[:300] if text else ""),
                    text=str(text) if text is not None else "",
                    published=r.get("publishedDate"),
                    provider=self.name,
                    extra={k: r[k] for k in ("score", "author") if k in r},
                )
            )
        return out


#: Preference order, and the reason it is this order.
#:
#: Providers that supply page text come FIRST, because they remove the need to fetch pages at all —
#: and fetching is where the risk lives: egress from a Lambda, per-site terms of service, and SSRF
#: if a URL ever arrives from anywhere but a search response (§12.2). Choosing Tavily or Exa over
#: Brave therefore deletes a whole class of exposure rather than expressing a taste.
#:
#: Tavily before Exa is arbitrary and labelled as such: both supply text, neither has been measured
#: here, and pretending to a preference would be inventing evidence. Override with
#: `SEARCH_PROVIDER` when it matters.
PROVIDERS: Sequence[type] = (TavilySearch, ExaSearch, BraveSearch)


def _strip_tags(text: str) -> str:
    """Brave wraps matched terms in <strong>. This text can reach a prompt, so the markup goes."""
    import re

    return re.sub(r"<[^>]+>", "", text)


def available() -> List[str]:
    """Names of every provider whose key is present, in preference order."""
    return [p.name for p in PROVIDERS if os.environ.get(p.env_var)]


def select(
    preferred: Optional[str] = None, *, env: Optional[Dict[str, str]] = None
) -> Provider:
    """The provider to use. Raises `SearchUnavailable` when none is configured.

    `preferred` (or `SEARCH_PROVIDER`) names one explicitly and is an ERROR if its key is missing,
    rather than a hint that falls back. Silently using a different provider than the one asked for
    would make a deliberate choice — say, picking the text-supplying one to avoid a fetcher — fail
    open into exactly the configuration it was avoiding.

    Raising rather than returning None: research is opt-in, so reaching this function at all means
    somebody asked for it, and an empty corpus is a worse answer than a refusal.
    """
    environ = env if env is not None else dict(os.environ)
    wanted = (preferred or environ.get("SEARCH_PROVIDER") or "").strip().lower()

    if wanted:
        for provider in PROVIDERS:
            if provider.name == wanted:
                key = environ.get(provider.env_var)
                if not key:
                    raise SearchUnavailable(
                        f"SEARCH_PROVIDER={wanted} but {provider.env_var} is not set. Set the key "
                        "or unset SEARCH_PROVIDER to choose by what is available."
                    )
                return provider(key)
        raise SearchUnavailable(
            f"unknown search provider {wanted!r}; known: "
            + ", ".join(p.name for p in PROVIDERS)
        )

    for provider in PROVIDERS:
        key = environ.get(provider.env_var)
        if key:
            if not provider.supplies_text:
                logger.info(
                    "Using %s, which returns snippets rather than page text, so pages must be "
                    "fetched separately. A TAVILY_API_KEY or EXA_API_KEY would remove that step.",
                    provider.name,
                )
            return provider(key)

    raise SearchUnavailable(
        "No search provider configured. Set one of: "
        + ", ".join(f"{p.env_var} ({p.name})" for p in PROVIDERS)
    )
