"""Looking things up on English Wikipedia: the only website the plant can use. Standard library only.

The program fetches the article, never the plant itself, and only from en.wikipedia.org: redirects to anywhere
else are refused. She can look up anything, on any subject, whenever she likes. By default she gets the whole
article as plain text (without its reference lists), plus the titles of other articles that matched, so she can
browse a subject. "X about Y" gives just the opening, the section list and the parts about Y, for a quick look.

Try it from the project folder:  python3 -m plantagent.wikipedia "Pilea peperomioides about propagation"
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from .memory import find_passages

HOST = "en.wikipedia.org"
API = f"https://{HOST}/w/api.php"
USER_AGENT = "PlantBot/1.0 (a personal plant companion; https://github.com/SamanthaMakesStuff/PlantBot)"
TIMEOUT = 15            # seconds
MAX_BYTES = 3_000_000   # the longest Wikipedia articles are well under this as plain text
LEAD_CHARS = 3_000      # the article's opening
PASSAGE_CHARS = 9_000   # the parts most about her topic
FULL_CHARS = 120_000    # a whole article (about 40,000 tokens at most; most are far shorter)
_BACK_MATTER = ("references", "external links", "further reading", "notes", "see also", "sources", "bibliography",
                "citations", "footnotes")


class WikiError(Exception):
    """Something went wrong fetching; the message is fit to show the plant and its human."""


class _StayOnWikipedia(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != HOST:
            raise WikiError("Wikipedia tried to send the lookup to another website, so it was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _split(query: str):
    """'Pilea peperomioides about propagation' -> ('Pilea peperomioides', 'propagation', False);
    'Pilea peperomioides in full' -> ('Pilea peperomioides', '', True)."""
    query = " ".join(query.split())[:200]
    full = re.search(r"[\s,;(]*\b(in full|full article|whole article|full)\)?$", query, re.I)
    if full and full.start() > 0:
        return query[:full.start()].strip(), "", True
    m = re.match(r"^(.+?)\s+about\s+(.+)$", query, re.I)
    return (m.group(1), m.group(2), False) if m else (query, "", False)


def _without_back_matter(text: str) -> str:
    """The article without its reference lists, link lists and the like."""
    keep, skipping = [], False
    for block in re.split(r"(?m)^(?===+[^=])", text):
        heading = re.match(r"^==+\s*(.+?)\s*==+", block)
        if heading and heading.group(0).count("=") <= 4:  # a main section: decide whether its back matter
            skipping = heading.group(1).strip().lower() in _BACK_MATTER
        if not skipping:
            keep.append(block)
    return "".join(keep).strip()


def whole(text: str) -> str:
    """The whole article, without its reference lists, up to FULL_CHARS."""
    text = _without_back_matter(text)
    return text if len(text) <= FULL_CHARS else text[:FULL_CHARS] + "\n[... the rest of the article is cut off here]"


def fetch(search: str):
    """The best-matching article: (title, address, plain text with == Headings ==, other matching titles,
    titles of the articles it links to), or None if there isn't one. One request does all of it."""
    params = {
        "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
        "generator": "search", "gsrsearch": search, "gsrlimit": "1", "gsrnamespace": "0",
        "prop": "extracts|info|links", "inprop": "url", "explaintext": "1", "exsectionformat": "wiki",
        "plnamespace": "0", "pllimit": "max",
        "list": "search", "srsearch": search, "srlimit": "8", "srnamespace": "0", "srprop": "",
    }
    request = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(_StayOnWikipedia).open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
    except WikiError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise WikiError("Wikipedia couldn't be reached just now. Try again later.")
    if len(raw) > MAX_BYTES:
        raise WikiError("That Wikipedia answer was too big to read.")
    try:
        query = json.loads(raw.decode("utf-8")).get("query", {})
        pages = query.get("pages", [])
        matches = [str(r.get("title", "")) for r in query.get("search", []) if isinstance(r, dict)]
    except (ValueError, AttributeError):
        raise WikiError("Wikipedia sent back something that couldn't be read.")
    page = next((p for p in pages if isinstance(p, dict) and p.get("extract")), None)
    if not page:
        return None
    address = str(page.get("fullurl", ""))
    if urllib.parse.urlsplit(address).netloc != HOST:
        address = f"https://{HOST}/wiki/" + urllib.parse.quote(str(page.get("title", "")).replace(" ", "_"))
    title = str(page.get("title", ""))
    others = [t for t in matches if t and t != title][:7]
    links = [str(l.get("title", "")) for l in page.get("links", []) if isinstance(l, dict) and l.get("title")]
    return title, address, str(page["extract"]), others, links


RELATED = 20  # linked articles to suggest


def related(title: str, text: str, links: list) -> list:
    """The linked articles most worth following: the article's own "See also" list first, then the links its text
    mentions most often (an article links to hundreds of pages, most of them in passing)."""
    linked = {l.lower(): l for l in links if l != title}
    see_also = []
    m = re.search(r"(?ms)^==\s*See also\s*==\s*$(.*?)(?=^==[^=]|\Z)", text)
    for line in (m.group(1).splitlines() if m else []):
        found = linked.get(line.strip().lower())
        if found and found not in see_also:
            see_also.append(found)
    body = _without_back_matter(text).lower()
    counted = sorted(((body.count(l.lower()), l) for l in linked.values() if l not in see_also), key=lambda c: -c[0])
    return (see_also + [l for n, l in counted if n])[:RELATED]


def shorten(title: str, text: str, topic: str) -> str:
    """The opening, the section headings, then the passages most about the topic (or simply what follows the
    opening, if the topic is just the article's own subject)."""
    lead, _, body = _without_back_matter(text).partition("\n==")
    body = "==" + body if body else ""
    headings = [h for h in re.findall(r"^==+\s*(.+?)\s*==+\s*$", body, re.M) if h.lower() not in _BACK_MATTER]
    title_words = set(re.findall(r"[a-z0-9']+", title.lower()))
    topic = " ".join(w for w in re.findall(r"[A-Za-z0-9']+", topic) if w.lower() not in title_words)
    passages, _ = find_passages(body, topic, PASSAGE_CHARS, where=False) if topic else ("", 0)
    if not passages:
        passages = body[:PASSAGE_CHARS]
    parts = [lead.strip()[:LEAD_CHARS]]
    if headings:
        parts.append("Sections: " + ", ".join(headings[:40]))
    if passages.strip():
        parts.append(passages.strip())
    return "\n\n".join(parts)


def lookup(query: str):
    """Looks a query up. Returns (title, address, text, whole article?, other matching titles, related linked
    articles) or None if nothing matched. Anything is searched as it is and gives the whole article; 'X about Y' gives the parts of X about Y;
    'X in full' is the same as plain X (kept because she may well say it)."""
    search, about, _ = _split(query)
    found = fetch(search)
    if not found:
        return None
    title, address, text, others, links = found
    nearby = related(title, text, links)
    if about:
        return title, address, shorten(title, text, about), False, others, nearby
    return title, address, whole(text), True, others, nearby


if __name__ == "__main__":
    try:
        result = lookup(" ".join(sys.argv[1:]) or "Pilea peperomioides")
    except WikiError as e:
        sys.exit(f"Not working: {e}")
    if not result:
        sys.exit("Wikipedia was reached, but has no article matching that.")
    print(f"It works. {result[0]} ({result[1]}), {len(result[2]):,} characters{' (the whole article)' if result[3] else ''}:\n")
    print(result[2][:1500])
    if result[4]:
        print("\nOther articles that matched: " + "; ".join(result[4]))
    if result[5]:
        print("\nArticles it links to: " + "; ".join(result[5]))
