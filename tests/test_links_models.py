import httpx

from radar import links
from radar.models import RawPost


def test_unwrap_linkedin_safety_redirect():
    wrapped = ("https://www.linkedin.com/safety/go/?url=https%3A%2F%2Flnkd%2Ein%2FgAbc123"
               "&urlhash=x&isSdui=true")
    assert links.unwrap_linkedin(wrapped) == "https://lnkd.in/gAbc123"
    assert links.unwrap_linkedin("https://arxiv.org/abs/2611.01234") == "https://arxiv.org/abs/2611.01234"


def test_label_targets():
    assert links.label("https://arxiv.org/abs/2611.01234v2") == "arxiv"
    assert links.label("https://doi.org/10.1021/acs.jctc.5c02004") == "doi"
    assert links.label("https://pubs.acs.org/doi/10.1021/acs.jctc.5c02004") == "doi"
    assert links.label("https://chemrxiv.org/engage/chemrxiv/article-details/abc") == "preprint"
    assert links.label("https://www.nature.com/articles/s41586-0") == "journal"
    assert links.label("https://github.com/ACEsuit/mace") == "code"
    assert links.label("https://example.org/blog") == "other"


def test_ids_in_text():
    text = "See arXiv:2611.01234v1 and doi 10.1063/5.0321463, plus more."
    assert links.ids_in_text(text) == [
        "https://arxiv.org/abs/2611.01234",
        "https://doi.org/10.1063/5.0321463",
    ]


def test_resolve_follows_redirect_and_interstitial():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(301, headers={"Location": "https://arxiv.org/abs/2611.00001"})
        if request.url.path == "/interstitial":
            html = ('<a data-tracking-control-name="external_url_click" '
                    'href="https://doi.org/10.1/x?a=1&amp;b=2">go</a>')
            return httpx.Response(200, text=html)
        if request.url.path == "/reordered":
            html = ("<abbr>x</abbr><a class='btn' href='https://arxiv.org/abs/2611.00002?q=a&#38;b' "
                    "data-tracking-control-name='external_url_click'>go</a>")
            return httpx.Response(200, text=html)
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert links.resolve("https://lnkd.in/redirect", client=client) == "https://arxiv.org/abs/2611.00001"
    assert links.resolve("https://lnkd.in/interstitial", client=client) == "https://doi.org/10.1/x?a=1&b=2"
    assert links.resolve("https://lnkd.in/reordered", client=client) == "https://arxiv.org/abs/2611.00002?q=a&b"
    # Non-short links are returned untouched without a request.
    assert links.resolve("https://example.org/a", client=client) == "https://example.org/a"


def test_rawpost_normalizes():
    p = RawPost.from_dict({"key": "k", "author": "A", "authorUrl": "https://www.linkedin.com/in/a?x=1",
                           "authorKind": "person", "degree": "3rd+", "text": "  hello  "})
    assert p.author_url == "https://www.linkedin.com/in/a/"
    assert p.degree == "3rd+"
    assert p.text == "hello"
    same = RawPost.from_dict({"key": "other", "author": "A", "text": "HELLO"})
    assert p.text_hash == same.text_hash
