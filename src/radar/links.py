"""Outbound link handling: unwrap LinkedIn redirects, expand short links, label targets."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import httpx

SHORTENERS = ("lnkd.in", "bit.ly", "t.co", "buff.ly", "ow.ly", "tinyurl.com")

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("arxiv", re.compile(r"arxiv\.org/(abs|pdf|html)/", re.IGNORECASE)),
    ("doi", re.compile(r"(doi\.org/10\.|/doi/(abs/|full/|pdf/)?10\.)", re.IGNORECASE)),
    ("preprint", re.compile(r"(biorxiv|chemrxiv|medrxiv|ssrn|openreview|researchsquare)\.", re.IGNORECASE)),
    ("journal", re.compile(
        r"(nature\.com|science\.org|pubs\.acs\.org|pubs\.aip\.org|link\.springer\.com|"
        r"sciencedirect\.com|wiley\.com|iopscience|pnas\.org|rsc\.org|cell\.com|aps\.org|"
        r"mdpi\.com|tandfonline\.com|proceedings\.mlr\.press|neurips\.cc|aclanthology)", re.IGNORECASE)),
    ("code", re.compile(r"(github\.com|gitlab\.com|huggingface\.co)", re.IGNORECASE)),
]

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"\barxiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)


def unwrap_linkedin(url: str) -> str:
    """linkedin.com/safety/go/?url=X  ->  X."""
    p = urlparse(url)
    if p.netloc.endswith("linkedin.com") and p.path.startswith("/safety/go"):
        target = parse_qs(p.query).get("url")
        if target:
            return target[0]
    return url


def label(url: str) -> str:
    for name, pat in _RULES:
        if pat.search(url):
            return name
    return "other"


def is_short(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == s or host.endswith("." + s) for s in SHORTENERS)


def ids_in_text(text: str) -> list[str]:
    """DOIs and arXiv ids mentioned in plain post text, as resolvable URLs."""
    out = []
    for m in _ARXIV_RE.finditer(text):
        out.append(f"https://arxiv.org/abs/{m.group(1)}")
    for m in _DOI_RE.finditer(text):
        out.append("https://doi.org/" + m.group(0).rstrip(".,;)"))
    return out


_ANCHOR = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _interstitial_target(page: str) -> str | None:
    """Find the external link on a lnkd.in warning page, whatever the attribute order."""
    for tag in _ANCHOR.findall(page):
        if "external_url_click" in tag:
            m = _HREF.search(tag)
            if m:
                return html.unescape(m.group(1))
    return None


def resolve(url: str, client: httpx.Client | None = None, timeout: float = 10.0) -> str:
    """Follow redirects for short links. lnkd.in sometimes serves an interstitial page
    instead of a 30x; the real target is then in an anchor on that page."""
    url = unwrap_linkedin(url)
    if not is_short(url):
        return url
    own = client is None
    client = client or httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (research-radar link resolver)"},
    )
    try:
        r = client.get(url)
        final = str(r.url)
        if urlparse(final).netloc.endswith("lnkd.in") or "linkedin.com" in urlparse(final).netloc:
            target = _interstitial_target(r.text)
            if target:
                return target
        return final
    except httpx.HTTPError:
        return url
    finally:
        if own:
            client.close()


def resolve_all(urls: list[str], text: str = "") -> list[dict]:
    """Resolve and label every link in a post, plus ids found in its text. Deduped, ordered."""
    seen: set[str] = set()
    out: list[dict] = []
    with httpx.Client(
        follow_redirects=True,
        timeout=10.0,
        headers={"User-Agent": "Mozilla/5.0 (research-radar link resolver)"},
    ) as client:
        for raw in list(urls) + ids_in_text(text):
            final = resolve(raw, client=client)
            if final in seen:
                continue
            seen.add(final)
            out.append({"url": final, "kind": label(final)})
    return out
