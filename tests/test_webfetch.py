# SPDX-License-Identifier: Apache-2.0
"""
Fetching a page found by search — mostly, what it refuses to fetch.

The balance of this file is deliberate. Extraction working is easy to check by hand; the SSRF rules
are the part where a plausible-looking implementation is wrong, and where being wrong turns a
research feature into a reader of instance metadata. `docs/PERSONA-RESEARCH.md` §12.2.

Four rules, each with a test that fails if it is loosened:

  https only          `file://` and friends are how a URL-fetcher becomes a file-reader
  resolved addresses  a NAME check is defeated by a name that resolves to 127.0.0.1, which
                      `localtest.me` does publicly and on purpose
  every redirect hop  an allowed URL answering `302 Location: http://169.254.169.254/` defeats a
                      check applied only to the URL the caller passed
  a streamed ceiling  `Content-Length` is a claim

Nothing here touches the network: `validate` is tested with a patched resolver and `fetch` through
its `call` seam.
"""

import pytest

from matrix_studio import webfetch as wf

#: A body that clears `MIN_TEXT_CHARS`, so tests about redirects and extraction are not silently
#: testing the block-page floor instead. Real statute language, for the same reason.
STATUTE = (
    "The provider has sufficient knowledge of the member to initiate at least a general or "
    "preliminary diagnosis of its medical condition, and the provider has arranged for either "
    "continuation of treatment or emergency coverage. The client has agreed to follow the "
    "provider's instructions."
)


def page_body(extra: str = "") -> bytes:
    return f"<html><body><p>{STATUTE}</p><p>{extra}</p></body></html>".encode()


def resolver(mapping):
    """Patch `socket.getaddrinfo` with a name -> addresses map."""

    def getaddrinfo(host, *_a, **_k):
        if host not in mapping:
            raise OSError(f"no such host {host}")
        return [(2, 1, 6, "", (addr, 0)) for addr in mapping[host]]

    return getaddrinfo


def http(status=200, headers=None, body=b"", final=None, record=None):
    """Stands in for one HTTP GET."""

    async def call(url, hdrs):
        if record is not None:
            record.append({"url": url, "headers": hdrs})
        h = {"content-type": "text/html"}
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        return status, h, body, final or url

    return call


def sequence(*responses, record=None):
    """Successive responses, for redirect chains."""
    it = iter(responses)

    async def call(url, hdrs):
        if record is not None:
            record.append(url)
        status, headers, body = next(it)
        h = {"content-type": "text/html"}
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        return status, h, body, url

    return call


# --------------------------------------------------------------------------- #
# what it refuses
# --------------------------------------------------------------------------- #


class TestValidateRefuses:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/x",          # plaintext
            "file:///etc/passwd",            # the reason schemes are an allowlist
            "gopher://example.com/",
            "ftp://example.com/x",
            "//example.com/x",               # no scheme at all
        ],
    )
    def test_only_https(self, url, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"example.com": ["93.184.216.34"]}))
        with pytest.raises(wf.FetchRefused, match="scheme"):
            wf.validate(url)

    @pytest.mark.parametrize(
        "address,what",
        [
            ("127.0.0.1", "loopback"),
            ("169.254.169.254", "instance metadata — the target that matters on this stack"),
            ("10.0.0.5", "private"),
            ("192.168.1.1", "private"),
            ("172.16.0.1", "private"),
            ("100.64.0.1", "carrier-grade NAT"),
            ("::1", "IPv6 loopback"),
            ("fd00::1", "IPv6 unique-local"),
            ("0.0.0.0", "unspecified"),
        ],
    )
    def test_a_name_that_resolves_somewhere_private_is_refused(self, address, what, monkeypatch):
        # THE test. A hostname check would pass every one of these: the name is public and only the
        # ANSWER is private. `localtest.me` resolves to 127.0.0.1 publicly, by design.
        monkeypatch.setattr("socket.getaddrinfo", resolver({"totally-public.example": [address]}))
        with pytest.raises(wf.FetchRefused, match="not a global address"):
            wf.validate("https://totally-public.example/x")

    def test_one_private_answer_among_public_ones_is_still_refused(self, monkeypatch):
        # Checking only the first answer would pass this and then connect to whichever the client
        # happened to pick.
        monkeypatch.setattr(
            "socket.getaddrinfo",
            resolver({"mixed.example": ["93.184.216.34", "127.0.0.1"]}),
        )
        with pytest.raises(wf.FetchRefused, match="not a global address"):
            wf.validate("https://mixed.example/x")

    def test_a_literal_address_never_reaches_dns(self, monkeypatch):
        # Without a direct check an attacker skips resolution by writing the address they want.
        def explode(*_a, **_k):
            raise AssertionError("a literal IP must not be resolved")

        monkeypatch.setattr("socket.getaddrinfo", explode)
        with pytest.raises(wf.FetchRefused, match="not a global address"):
            wf.validate("https://169.254.169.254/latest/meta-data/")

    def test_a_public_literal_address_is_allowed(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({}))
        host, addrs = wf.validate("https://93.184.216.34/x")
        assert (host, addrs) == ("93.184.216.34", ["93.184.216.34"])

    def test_a_name_that_does_not_resolve_is_refused(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({}))
        with pytest.raises(wf.FetchRefused, match="does not resolve"):
            wf.validate("https://nope.example/x")

    def test_a_public_name_passes(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"vmb.ca.gov": ["93.184.216.34"]}))
        host, addrs = wf.validate("https://vmb.ca.gov/laws_regs/")
        assert host == "vmb.ca.gov"
        assert addrs == ["93.184.216.34"]

    def test_http_is_not_in_the_allowlist(self):
        # Asserted as a property rather than only through a URL, so widening the set is a visible
        # change to a named constant.
        assert wf.ALLOWED_SCHEMES == frozenset({"https"})


# --------------------------------------------------------------------------- #
# redirects
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestRedirectsAreRevalidated:
    async def test_a_redirect_to_the_metadata_endpoint_is_refused(self, monkeypatch):
        # The reason redirects are followed by hand. A client with `follow_redirects=True` would
        # have fetched this, having validated only the URL the caller passed.
        monkeypatch.setattr(
            "socket.getaddrinfo", resolver({"harmless.example": ["93.184.216.34"]})
        )
        call = sequence(
            (302, {"location": "http://169.254.169.254/latest/meta-data/"}, b""),
        )
        assert await wf.fetch("https://harmless.example/x", call=call) is None

    async def test_a_redirect_to_a_private_name_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            "socket.getaddrinfo",
            resolver({"harmless.example": ["93.184.216.34"], "inside.example": ["10.1.2.3"]}),
        )
        call = sequence((302, {"location": "https://inside.example/secrets"}, b""))
        assert await wf.fetch("https://harmless.example/x", call=call) is None

    async def test_an_allowed_redirect_is_followed_and_the_final_url_recorded(self, monkeypatch):
        # A citation should carry the URL actually read, not the one first requested.
        monkeypatch.setattr(
            "socket.getaddrinfo",
            resolver({"a.example": ["93.184.216.34"], "b.example": ["93.184.216.35"]}),
        )
        seen = []
        call = sequence(
            (301, {"location": "https://b.example/final"}, b""),
            (200, {"content-type": "text/html"}, page_body()),
            record=seen,
        )
        page = await wf.fetch("https://a.example/start", call=call)
        assert page is not None
        assert page.url == "https://a.example/start"
        assert page.final_url == "https://b.example/final"
        assert seen == ["https://a.example/start", "https://b.example/final"]

    async def test_a_redirect_loop_terminates(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"a.example": ["93.184.216.34"]}))
        call = sequence(
            (302, {"location": "https://a.example/b"}, b""),
            (302, {"location": "https://a.example/b"}, b""),
        )
        assert await wf.fetch("https://a.example/b", call=call) is None

    async def test_a_redirect_with_no_location_is_not_followed(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"a.example": ["93.184.216.34"]}))
        assert await wf.fetch("https://a.example/x", call=http(status=302)) is None


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


class TestHtmlToText:
    def test_script_and_style_content_never_appears(self):
        html = """<html><head><style>.a{color:red}</style></head><body>
        <script>var secret = 'do not quote me';</script>
        <p>The provider has arranged for continuation of treatment.</p>
        </body></html>"""
        text = wf.html_to_text(html)
        assert "continuation of treatment" in text
        assert "do not quote me" not in text
        assert "color:red" not in text

    def test_block_elements_become_breaks_so_sentences_do_not_merge(self):
        # The text is chunked on sentence boundaries downstream, so two paragraphs running together
        # would produce a chunk straddling an idea.
        text = wf.html_to_text("<p>First sentence.</p><p>Second sentence.</p>")
        assert "First sentence." in text and "Second sentence." in text
        assert "First sentence.Second" not in text

    def test_entities_are_decoded(self):
        assert "§ 4826" in wf.html_to_text("<p>&sect; 4826</p>")

    def test_malformed_markup_keeps_what_was_parsed(self):
        # Malformed HTML is the norm. Returning nothing would look like an empty source rather than
        # a parser giving up.
        text = wf.html_to_text("<p>kept</p><div><span>also kept")
        assert "kept" in text and "also kept" in text

    def test_empty_input_is_empty_output(self):
        assert wf.html_to_text("") == ""


@pytest.mark.asyncio
class TestFetchExtraction:
    async def test_html_is_extracted(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        page = await wf.fetch(
            "https://x.example/a",
            call=http(body=page_body("Article 4. Requirements.")),
        )
        assert page.media_type == "html"
        assert "Article 4. Requirements." in page.text

    async def test_plain_text_is_read(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        page = await wf.fetch(
            "https://x.example/a.txt",
            call=http(headers={"content-type": "text/plain; charset=utf-8"},
                      body=f"section 4826(b). {STATUTE}".encode()),
        )
        assert "4826(b)" in page.text
        assert page.media_type == "txt"

    async def test_a_pdf_url_served_as_octet_stream_is_still_treated_as_pdf(self, monkeypatch):
        # Common on statute sites, and the ASSOC model practice act arrived as a PDF from a live
        # search — so PDFs are primary sources here rather than an edge case.
        monkeypatch.setattr("socket.getaddrinfo", resolver({"assoc.org": ["93.184.216.34"]}))
        assert wf._suffix_for("application/octet-stream",
                              "https://assoc.org/model-licensed-practice-act.pdf") == ".pdf"

    async def test_an_image_is_skipped_rather_than_stored_as_junk(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        assert await wf.fetch(
            "https://x.example/a.png",
            call=http(headers={"content-type": "image/png"}, body=b"\x89PNG"),
        ) is None

    async def test_a_page_with_no_text_is_skipped(self, monkeypatch):
        # A scan with no text layer is the likeliest empty case, and an empty document in a corpus is
        # worse than a missing one: retrieval would rank it and return nothing.
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        assert await wf.fetch(
            "https://x.example/a", call=http(body=b"<html><body><div></div></body></html>")
        ) is None

    async def test_an_http_error_is_skipped(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        assert await wf.fetch("https://x.example/a", call=http(status=404)) is None

    async def test_a_transport_failure_is_skipped_not_raised(self, monkeypatch):
        # §5.2: research is additive, and one unfetchable page must not lose a corpus.
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))

        async def explode(url, headers):
            raise OSError("connection reset")

        assert await wf.fetch("https://x.example/a", call=explode) is None

    async def test_the_user_agent_identifies_the_fetcher(self, monkeypatch):
        # Rather than impersonating a browser. A site entitled to refuse this should be able to.
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        seen = []
        await wf.fetch("https://x.example/a", call=http(body=page_body(), record=seen))
        assert "MatrixStudio-Research" in seen[0]["headers"]["User-Agent"]


class TestTheCeiling:
    def test_it_is_a_size_a_statute_pdf_fits_in(self):
        # Pinned as a number: too small silently drops the primary sources this feature exists to
        # find, and the symptom would be an empty corpus rather than an error.
        assert wf.MAX_BYTES >= 4 * 1024 * 1024

    def test_a_timeout_exists(self):
        assert 0 < wf.FETCH_TIMEOUT_S <= 60


class TestABlockPageIsNotASource:
    """From a live fetch, not from taste.

    The ASSOC model licensed practice act PDF — a primary source this feature exists to find —
    returned HTTP 200 with 954 bytes of `text/html` reading "Request unsuccessful. Incapsula
    incident ID: 1018000081381335745-...". A bot block wearing a success code.

    It extracted to 83 characters and passed the emptiness check, so it would have entered a corpus
    as a found source: retrieval could rank it, a persona could cite it, and the run would have
    counted a citation it never had. That is worse than fetching nothing, because it is invisible.
    """

    def test_the_floor_admits_a_terse_statute_and_rejects_a_block_page(self):
        assert 100 <= wf.MIN_TEXT_CHARS <= 400

    @pytest.mark.asyncio
    async def test_the_real_incapsula_body_is_refused(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"assoc.org": ["93.184.216.34"]}))
        body = (b"<html><body>Request unsuccessful. Incapsula incident ID: "
                b"1018000081381335745-663803568078848558</body></html>")
        assert await wf.fetch(
            "https://assoc.org/model-licensed-practice-act.pdf",
            call=http(headers={"content-type": "text/html"}, body=body),
        ) is None

    @pytest.mark.asyncio
    async def test_the_rejected_text_is_logged_so_a_new_block_is_recognisable(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        with caplog.at_level("INFO"):
            await wf.fetch(
                "https://x.example/a",
                call=http(body=b"<html><body>Please enable cookies.</body></html>"),
            )
        assert "Please enable cookies." in caplog.text

    @pytest.mark.asyncio
    async def test_a_page_just_over_the_floor_is_kept(self, monkeypatch):
        monkeypatch.setattr("socket.getaddrinfo", resolver({"x.example": ["93.184.216.34"]}))
        long_enough = "Sufficient knowledge of the member to initiate a diagnosis. " * 6
        page = await wf.fetch(
            "https://x.example/a",
            call=http(body=f"<html><body><p>{long_enough}</p></body></html>".encode()),
        )
        assert page is not None and len(page.text) >= wf.MIN_TEXT_CHARS
