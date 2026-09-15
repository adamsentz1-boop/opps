"""GenericRSSSource: consume any RSS 2.0 / Atom feed of opportunity listings you are permitted to read.

Uses only the standard library. Each entry becomes an opportunity; budgets are parsed from text when present.
"""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
import re

from app.normalizer import normalize
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource, SourceError

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str:
    return unescape(_TAG_RE.sub(" ", text or "")).strip()


def parse_feed(xml_text: str, source_name: str) -> list[NormalizedOpportunity]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"Invalid feed XML: {exc}") from exc
    items: list[NormalizedOpportunity] = []
    # RSS 2.0
    for item in root.iter("item"):
        payload = {
            "title": _strip_html(item.findtext("title")),
            "description": _strip_html(item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")),
            "link": (item.findtext("link") or "").strip(),
            "guid": (item.findtext("guid") or item.findtext("link") or "").strip(),
            "pubDate": item.findtext("pubDate"),
            "author": _strip_html(item.findtext("author") or item.findtext("{http://purl.org/dc/elements/1.1/}creator")),
            "tags": [c.text for c in item.findall("category") if c.text],
        }
        items.append(normalize(source_name, payload))
    # Atom
    for entry in root.iter(f"{_ATOM}entry"):
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href") if link_el is not None else ""
        author_el = entry.find(f"{_ATOM}author/{_ATOM}name")
        payload = {
            "title": _strip_html(entry.findtext(f"{_ATOM}title")),
            "description": _strip_html(entry.findtext(f"{_ATOM}content") or entry.findtext(f"{_ATOM}summary")),
            "link": link, "guid": entry.findtext(f"{_ATOM}id") or link,
            "published": entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated"),
            "author": author_el.text if author_el is not None else None,
            "tags": [c.get("term") for c in entry.findall(f"{_ATOM}category") if c.get("term")],
        }
        items.append(normalize(source_name, payload))
    return items


class GenericRSSSource(OpportunitySource):
    display_name = "Generic RSS/Atom feed"

    def __init__(self, url: str, source_name: str | None = None, timeout: int = 20):
        self.url = url
        self.timeout = timeout
        self.source_name = source_name or f"rss:{_slug(url)}"

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        req = urllib.request.Request(self.url, headers={"User-Agent": "OpportunityEngine/0.1 (+local)"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - owner-configured URL
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Could not fetch feed {self.url}: {exc}") from exc
        return parse_feed(text, self.source_name)

    def status_note(self) -> str | None:
        return self.url


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower().split("//", 1)[-1])[:40].strip("-")
