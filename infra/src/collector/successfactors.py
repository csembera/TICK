"""
SAP SuccessFactors career sites, collected through the job feed the site
publishes for search engines: GET https://<careers host>/sitemap.xml.

One file run two ways, like dayforce.py: the collector imports it, and so
does the evaluator's gate (through tick_common), so a config that verifies
collects the same postings every night. Standard library only. The caller
passes the HTTP call.

A classic career site (jobs.<company>.com/go/..., /job/<slug>/<id>/) renders
its search as HTML, 25 postings a page, and its JSON search
(/services/recruiting/v1/jobs) answers only a browser session, so the
collector uses neither. The feed lists every posting the site shows in one
RSS document: title, link, location, job function and description. Its
`g:` fields (Google's job-feed namespace) are read under their local names,
so a config maps `location`, not `g:location`.

The feed carries no requisition number. Each item's `id` (also the number in
its /job/ URL) is the posting's id on the career site; it is the closest
stable id the feed has, and posting_id maps to it.

Titles arrive as "<title> (<location>)". The suffix is dropped when it is
exactly the item's location, so a title reads as it does on the posting.
Descriptions arrive HTML-escaped inside the item; unescaped once, they are
the posting's HTML.
"""

import html
import xml.etree.ElementTree as ET

DESCRIPTION_KEY = "description"


def items(xml_text: str) -> list[dict]:
    """Every <item> in the feed as a dict of its fields, in feed order."""
    # A job feed declares no entities. Refusing any keeps an entity-expansion
    # document from ever reaching the parser.
    if "<!ENTITY" in xml_text:
        raise RuntimeError("successfactors: feed declares XML entities; refusing to parse it")
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise RuntimeError(f"successfactors: feed is <{root.tag}>, not an RSS document")
    postings = []
    for item in channel.findall("item"):
        fields = {}
        # namespaced fields first, so a plain RSS field wins a shared name
        for child in sorted(item, key=lambda c: not c.tag.startswith("{")):
            fields[child.tag.rpartition("}")[2]] = (child.text or "").strip()
        title, place = fields.get("title", ""), fields.get("location", "")
        if place and title.endswith(f" ({place})"):
            fields["title"] = title[: -len(place) - 3]
        if fields.get(DESCRIPTION_KEY):
            fields[DESCRIPTION_KEY] = html.unescape(fields[DESCRIPTION_KEY])
        postings.append(fields)
    return postings


def fetch_all(config: dict, get_text) -> tuple[list, int]:
    """Every posting on the feed, and how many there are.

    get_text(url) GETs url and returns the body. The feed reports no total of
    its own, so the count is what it lists. A body that is not an RSS feed
    raises, which fails the run and sends the company back to the evaluator."""
    postings = items(get_text(config["endpoint"]))
    return postings, len(postings)
