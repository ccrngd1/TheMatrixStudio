# SPDX-License-Identifier: Apache-2.0
"""
Fetch a page found by search, and turn it into text.

Needed because Brave returns descriptions of 100–500 characters — enough to rank a result, nowhere
near enough to cite a statute. `docs/PERSONA-RESEARCH.md` §12.2 records that choosing Brave means
owning this, and owning it means owning the risks a search API would otherwise have absorbed.

## What this refuses to fetch, and why each rule is here

**`https` only.** `http` is not merely insecure here; `file://`, `gopher://` and friends are how a
URL-fetcher becomes a file-reader.

**No private or reserved address.** Checked against every address the hostname RESOLVES to, not
against the hostname text. A name check is defeated by a hostname that simply resolves to
`127.0.0.1` — `localtest.me` does, publicly, by design — and the target that matters on this stack is
`169.254.169.254`, the instance metadata endpoint.

**Every redirect hop is re-validated.** Redirects are followed by hand rather than by the client,
because an allowed URL that answers `302 Location: http://169.254.169.254/` defeats a check applied
only to the URL the caller passed in.

**A byte ceiling, enforced while streaming.** A `Content-Length` header is a claim, not a limit. The
ASSOC model practice act came back from a live search as a PDF; statute sites serve large ones.

## The residual risk, stated rather than papered over

Validation resolves the hostname and then the client resolves it again to connect. Between those
two moments the answer can change — DNS rebinding — and closing that properly means connecting to
the validated IP with the original hostname carried in `Host` and SNI, which is awkward enough
through `httpx` to be its own piece of work.

It is not closed here. What makes that acceptable for now is the caller: **URLs come from a search
provider's response, never from user input** (§12.2), so an attacker would have to control a search
result AND win a rebinding race. If this fetcher is ever pointed at an operator-supplied URL, that
argument evaporates and the connection pinning becomes required.

## Failure is always "no page"

`fetch` returns `None` rather than raising, for every failure: refused, timed out, too large,
unreadable, wrong type. §5.2 — research is additive, and one unfetchable page must not lose a
corpus. Everything is logged with the reason, because a silently empty corpus is the failure this
module could most easily cause.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Bytes read from one page before giving up.
#:
#: 8 MB holds a long statute PDF and refuses a scanned volume. Enforced while streaming, because a
#: `Content-Length` header is a claim.
MAX_BYTES = 8 * 1024 * 1024

#: Seconds for one page, including its redirects.
FETCH_TIMEOUT_S = 20.0

#: Redirect hops followed. Each one is re-validated; this only bounds a loop.
MAX_REDIRECTS = 5

#: Characters a page must yield to count as a source.
#:
#: From real data, not taste. A live fetch of the ASSOC model practice act PDF returned HTTP 200 with
#: 954 bytes of `text/html` reading "Request unsuccessful. Incapsula incident ID: ..." — a bot block
#: wearing a success code. It extracted to 83 characters and passed an emptiness check, so it would
#: have entered a corpus as a found source: retrieval could rank it, a persona could cite it, and the
#: run would have counted a citation it never had.
#:
#: 200 is low enough to keep a terse statute subsection and high enough to reject every block page
#: and cookie interstitial seen so far. A page below it is logged with its text, because that text is
#: how the next person recognises a new kind of block.
MIN_TEXT_CHARS = 200

#: Schemes that may be fetched. `http` is excluded deliberately — see the module docstring.
ALLOWED_SCHEMES = frozenset({"https"})

#: `Content-Type` prefix to the file suffix `documents.extract_text` dispatches on. HTML is handled
#: here instead, because `SUPPORTED_SUFFIXES` has no entry for it and adding one would make every
#: knowledge-base upload accept HTML as a side effect of a research feature.
CONTENT_TYPES: Dict[str, str] = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

#: Elements whose text is never page content.
_DROP_ELEMENTS = frozenset({"script", "style", "noscript", "template", "svg", "head"})


@dataclass
class FetchedPage:
    url: str
    text: str
    media_type: str
    #: The URL actually read, after redirects. Differs from `url` when hops were followed, and it is
    #: the one a citation should carry.
    final_url: str
    bytes_read: int


class FetchRefused(ValueError):
    """The URL failed a safety rule. Never raised out of `fetch` — logged and turned into None."""


def _resolved_addresses(host: str) -> List[str]:
    """Every address `host` resolves to, or raise."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchRefused(f"{host}: does not resolve ({exc})") from exc
    return sorted({info[4][0] for info in infos})


def _check_address(address: str) -> None:
    """Refuse anything not a global unicast address.

    An allowlist of "global" rather than a denylist of known-bad ranges. A denylist has to enumerate
    loopback, link-local, private, carrier-grade NAT, multicast, reserved, unique-local v6 and
    IPv4-mapped v6 — and the one that matters here, `169.254.169.254`, sits in a range people forget.
    `is_global` is false for all of them by construction.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise FetchRefused(f"unparseable address {address!r}") from exc
    if not ip.is_global:
        raise FetchRefused(
            f"{address} is not a global address (loopback, private, link-local or reserved). "
            "Refused: this is how a fetcher becomes a reader of instance metadata."
        )


def validate(url: str) -> Tuple[str, List[str]]:
    """Check one URL and return `(host, resolved addresses)`. Raises `FetchRefused`.

    Separate from fetching so a caller can validate a redirect target with the identical rules,
    which is what makes hop-by-hop checking cheap enough that nobody skips it.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchRefused(
            f"scheme {parsed.scheme or '(none)'!r} is not allowed; "
            f"allowed: {', '.join(sorted(ALLOWED_SCHEMES))}"
        )
    host = parsed.hostname
    if not host:
        raise FetchRefused(f"no host in {url!r}")

    # A bare IP in the URL never reaches DNS, so it is checked directly. Without this an attacker
    # skips resolution entirely by writing the address they want.
    #
    # Parsing is separated from checking on purpose. Written as one `try` around both, the
    # `except ValueError` that detects "not an IP literal" also swallows `FetchRefused` — which
    # subclasses `ValueError` — so a literal private address fell through to DNS instead of being
    # refused. It happened to be refused anyway, because `getaddrinfo` resolves a literal to itself
    # and the loop below catches it, which is the worst kind of correct: the explicit check was dead
    # and nothing said so. A test caught it. Same shape as the speaker-selection `try` that laundered
    # a call-site error into a fallback.
    literal: Optional[str] = None
    try:
        ipaddress.ip_address(host)
        literal = host
    except ValueError:
        pass
    if literal is not None:
        _check_address(literal)
        return literal, [literal]

    addresses = _resolved_addresses(host)
    for address in addresses:
        # EVERY address, not the first. A hostname with one public and one loopback answer would
        # otherwise pass and then connect to whichever the client picked.
        _check_address(address)
    return host, addresses


def html_to_text(html: str) -> str:
    """Page text from HTML, using the standard library only.

    Deliberately crude, and honest about it: it drops scripts, styles and markup and collapses
    whitespace. It does NOT remove navigation, cookie banners or footers, so a fetched page carries
    boilerplate that a dedicated extractor would strip.

    Stdlib rather than a dependency because this has to work wherever the package is installed, and
    because the alternative failure — a research feature that silently produces nothing because an
    optional extractor is missing — is worse than one that produces noisy text. `trafilatura` or
    `readability-lxml` would improve quality and can be added behind the same function.
    """
    from html.parser import HTMLParser

    class Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: List[str] = []
            self._skip_depth = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in _DROP_ELEMENTS:
                self._skip_depth += 1
            elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
                # A paragraph break, so sentences from adjacent blocks do not run together — which
                # matters because the text is then chunked on sentence boundaries.
                self.parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag in _DROP_ELEMENTS and self._skip_depth:
                self._skip_depth -= 1

        def handle_data(self, data: str) -> None:
            if not self._skip_depth and data.strip():
                self.parts.append(data)

    parser = Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001
        # Malformed markup is the norm, not the exception. Whatever was parsed before the failure is
        # kept: a partial page beats no page, and returning nothing here would look like an empty
        # source rather than a parser giving up.
        logger.debug("HTML parsing stopped early (%s); keeping what was parsed", exc)

    from matrix_studio.documents import normalise_text

    return normalise_text("".join(parser.parts))


def _suffix_for(content_type: str, url: str) -> Optional[str]:
    """The extractor suffix for a response, or None if it is not text we can read."""
    base = (content_type or "").split(";")[0].strip().lower()
    if base in CONTENT_TYPES:
        return CONTENT_TYPES[base]
    if base in ("text/html", "application/xhtml+xml"):
        return ".html"
    if base.startswith("text/"):
        # An unfamiliar `text/*` is worth reading as plain text rather than discarding; statute sites
        # serve some odd ones.
        return ".txt"
    # A URL ending .pdf served as `application/octet-stream` is common enough to be worth catching.
    if url.lower().split("?")[0].endswith(".pdf"):
        return ".pdf"
    return None


async def fetch(url: str, *, call: Optional[Any] = None) -> Optional[FetchedPage]:
    """Fetch and extract one page. `None` on any failure, with the reason logged.

    `call(url, headers)` is the HTTP seam, returning
    `(status, headers, body_bytes, final_url)` — one function to patch, so no test needs a server.
    """
    seen: List[str] = []
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        try:
            # Re-validated on EVERY hop. A permitted URL answering `302` to the metadata endpoint is
            # the whole reason redirects are followed by hand.
            validate(current)
        except FetchRefused as exc:
            logger.warning("Refused %s (hop %d): %s", current, hop, exc)
            return None

        try:
            status, headers, body, final_url = await _request(current, call=call)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fetch of %s failed: %s", current, exc)
            return None

        if status in (301, 302, 303, 307, 308):
            location = headers.get("location") or headers.get("Location")
            if not location:
                logger.warning("%s returned %d with no Location", current, status)
                return None
            from urllib.parse import urljoin

            nxt = urljoin(current, location)
            if nxt in seen:
                logger.warning("Redirect loop at %s", nxt)
                return None
            seen.append(current)
            current = nxt
            continue

        if status >= 400:
            logger.info("%s returned HTTP %d; skipping", current, status)
            return None

        suffix = _suffix_for(headers.get("content-type") or headers.get("Content-Type") or "",
                            final_url or current)
        if suffix is None:
            logger.info(
                "%s is %r, which is not text this deployment reads; skipping",
                current, headers.get("content-type"),
            )
            return None

        return _extract(body, suffix, url=url, final_url=final_url or current)

    logger.warning("Too many redirects from %s", url)
    return None


async def _request(
    url: str, *, call: Optional[Any] = None
) -> Tuple[int, Dict[str, str], bytes, str]:
    """One HTTP GET, streamed and capped. Returns `(status, headers, body, final_url)`."""
    if call is not None:
        return await call(url, {"User-Agent": _USER_AGENT})

    import httpx

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S,
        # Off, because redirects are followed by hand so each hop can be re-validated.
        follow_redirects=False,
    ) as client:
        async with client.stream("GET", url, headers={"User-Agent": _USER_AGENT}) as response:
            chunks: List[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    # Stop reading rather than reject afterwards: the point of a ceiling is not to
                    # download the thing. A `Content-Length` check alone trusts the server.
                    logger.info("%s exceeded %d bytes; truncating", url, MAX_BYTES)
                    chunks.append(chunk)
                    break
                chunks.append(chunk)
            return (
                response.status_code,
                {k.lower(): v for k, v in response.headers.items()},
                b"".join(chunks)[:MAX_BYTES],
                str(response.url),
            )


#: Identifies the fetcher rather than impersonating a browser. A site that would refuse this is
#: entitled to, and pretending otherwise is the part of scraping worth not doing.
_USER_AGENT = "MatrixStudio-Research/1.0 (+persona research; contact the operator)"


def _extract(body: bytes, suffix: str, *, url: str, final_url: str) -> Optional[FetchedPage]:
    """Bytes to text, reusing `documents.extract_text` for everything but HTML."""
    from matrix_studio.documents import ExtractionError, normalise_text

    try:
        if suffix == ".html":
            text = html_to_text(body.decode("utf-8", errors="replace"))
            media_type = "html"
        else:
            # Via a temp file because `extract_text` dispatches on a path suffix. Worth the detour:
            # it is the same PDF and docx extraction the knowledge-base upload flow uses, already
            # capability-checked by `/api/documents/formats`, rather than a second implementation
            # that could disagree with it.
            import tempfile
            from pathlib import Path

            from matrix_studio.documents import extract_text

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as handle:
                handle.write(body)
                handle.flush()
                raw, media_type = extract_text(Path(handle.name), display_name=url)
            text = normalise_text(raw)
    except ExtractionError as exc:
        logger.info("Could not extract %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unexpected failure extracting %s: %s", url, exc)
        return None

    stripped = text.strip()
    if not stripped:
        # A scanned PDF with no text layer lands here, and it is the most likely empty case. Logged
        # at info because it is ordinary, not a fault.
        logger.info("%s yielded no text (a scan with no text layer?); skipping", url)
        return None
    if len(stripped) < MIN_TEXT_CHARS:
        # Almost always a bot block, a cookie wall or a redirect notice served with a 200. The text
        # is logged because it is the only way to recognise a new kind of block, and because a
        # legitimately terse page being refused here should be visible rather than guessed at.
        logger.info(
            "%s yielded only %d characters, below the %d needed to count as a source; skipping. "
            "Text was: %r",
            url, len(stripped), MIN_TEXT_CHARS, stripped[:200],
        )
        return None

    return FetchedPage(
        url=url, text=text, media_type=media_type, final_url=final_url, bytes_read=len(body),
    )
