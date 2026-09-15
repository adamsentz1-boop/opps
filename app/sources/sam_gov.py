"""SAM.gov federal contract opportunities via the official public API.

Docs: https://open.gsa.gov/api/get-opportunities-public-api/
Requires SAM_GOV_API_KEY (free from api.data.gov). No scraping; the key is sent as the documented `api_key`
query parameter and never logged.

Each notice becomes a `bid`-type opportunity. Set-asides and NAICS are carried through so the pipeline can create
compliance requirements the owner must verify before approval.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.normalizer import normalize, parse_datetime
from app.sanitize import clean_text
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource, SourceError

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
NOTICE_TYPE_NAMES = {"o": "Solicitation", "k": "Combined Synopsis/Solicitation", "p": "Presolicitation",
                     "r": "Sources Sought", "s": "Special Notice", "a": "Award Notice", "u": "Justification",
                     "g": "Sale of Surplus Property", "i": "Intent to Bundle"}


def _get(url: str, params: dict[str, Any], timeout: int = 30) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{url}?{query}", headers={"Accept": "application/json",
                                                             "User-Agent": "OpportunityEngine/0.1 (+local)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - documented public API
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # Never echo the URL (it carries the api_key).
        body = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else ""
        raise SourceError(f"SAM.gov API returned HTTP {exc.code}: {body}") from None
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"SAM.gov API request failed: {type(exc).__name__}") from None


def notice_to_payload(notice: dict[str, Any], description: str | None = None) -> dict[str, Any]:
    """Map a SAM.gov notice object to a normaliser payload."""
    pop = notice.get("placeOfPerformance") or {}
    city = (pop.get("city") or {}).get("name") if isinstance(pop.get("city"), dict) else pop.get("city")
    state = (pop.get("state") or {}).get("code") if isinstance(pop.get("state"), dict) else pop.get("state")
    place = ", ".join(x for x in [city, state] if x) or None
    contacts = notice.get("pointOfContact") or []
    contact_lines = [f"{c.get('fullName') or ''} {c.get('email') or ''}".strip() for c in contacts if isinstance(c, dict)]
    set_aside = notice.get("typeOfSetAsideDescription") or notice.get("typeOfSetAside") or None
    ntype = notice.get("type") or ""
    parts = [f"Agency: {notice.get('fullParentPathName') or ''}",
             f"Notice type: {ntype}", f"Solicitation #: {notice.get('solicitationNumber') or ''}",
             f"NAICS: {notice.get('naicsCode') or ''}  PSC: {notice.get('classificationCode') or ''}",
             f"Set-aside: {set_aside or 'none'}", f"Response deadline: {notice.get('responseDeadLine') or ''}",
             f"Place of performance: {place or ''}",
             f"Contacts: {'; '.join(contact_lines) or 'n/a'}", "", description or "(description not fetched)"]
    return {
        "external_id": notice.get("noticeId") or notice.get("solicitationNumber") or "",
        "title": notice.get("title") or "Untitled notice",
        "description": "\n".join(parts),
        "buyer_name": notice.get("fullParentPathName") or notice.get("department") or None,
        "buyer_type": "government",
        "location": place,
        "budget_type": "unknown",
        "posted_at": notice.get("postedDate"),
        "deadline": notice.get("responseDeadLine"),
        "source_url": notice.get("uiLink") or (f"https://sam.gov/opp/{notice.get('noticeId')}/view" if notice.get("noticeId") else None),
        "required_skills": [x for x in [notice.get("naicsCode"), notice.get("classificationCode")] if x],
        "opportunity_type": "bid",
        "agency": notice.get("fullParentPathName") or None,
        "solicitation_number": notice.get("solicitationNumber") or None,
        "notice_type": ntype or None,
        "set_aside": set_aside,
        "naics_code": str(notice.get("naicsCode")) if notice.get("naicsCode") else None,
        "place_of_performance": place,
        "raw_notice": {k: v for k, v in notice.items() if k not in ("description",)},
    }


class SamGovSource(OpportunitySource):
    source_name = "sam_gov"
    display_name = "SAM.gov (federal contract opportunities)"

    def __init__(self, api_key: str | None = None, naics: list[str] | None = None, keywords: list[str] | None = None,
                 notice_types: str | None = None, days_back: int | None = None, max_results: int | None = None,
                 fetch_descriptions: bool = True):
        s = get_settings()
        self.api_key = api_key if api_key is not None else s.sam_gov_api_key
        self.naics = naics if naics is not None else s.sam_gov_naics_list
        self.keywords = keywords if keywords is not None else s.sam_gov_keyword_list
        self.notice_types = notice_types or s.sam_gov_notice_types
        self.days_back = days_back or s.sam_gov_days_back
        self.max_results = max_results or s.sam_gov_max_results
        self.fetch_descriptions = fetch_descriptions
        self._last_note = ""

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return bool(self.api_key)

    @property
    def requirements(self) -> str:
        return ("Set SAM_GOV_API_KEY (free at api.data.gov) and optionally SAM_GOV_NAICS / SAM_GOV_KEYWORDS. Bidding also "
                "requires an active SAM.gov entity registration (UEI/CAGE), which the system never creates for you.")

    def source_url(self, external_id: str) -> str | None:
        return f"https://sam.gov/opp/{external_id}/view"

    def status_note(self) -> str | None:
        return self._last_note or None

    # ------------------------------------------------------------------ fetching
    def _search(self, extra: dict[str, Any]) -> list[dict]:
        now = datetime.now(timezone.utc)
        params = {"api_key": self.api_key, "limit": min(self.max_results, 1000), "offset": 0,
                  "postedFrom": (now - timedelta(days=self.days_back)).strftime("%m/%d/%Y"),
                  "postedTo": now.strftime("%m/%d/%Y"), "ptype": self.notice_types}
        params.update(extra)
        data = _get(SEARCH_URL, params)
        return [n for n in (data.get("opportunitiesData") or []) if isinstance(n, dict)]

    def _description(self, notice: dict) -> str | None:
        url = notice.get("description")
        if not (self.fetch_descriptions and isinstance(url, str) and url.startswith("https://")):
            return None
        try:
            data = _get(url, {"api_key": self.api_key})
        except SourceError as exc:
            log.info("description fetch failed for %s: %s", notice.get("noticeId"), exc)
            return None
        text = data.get("description") if isinstance(data, dict) else None
        return clean_text(_strip_html(text)) if text else None

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        if not self.api_key:
            raise NotImplementedError("SAM.gov adapter disabled: " + self.requirements)
        queries: list[dict[str, Any]] = []
        if self.naics:
            queries.append({"ncode": ",".join(self.naics)})
        for kw in self.keywords:
            queries.append({"title": kw})
        if not queries:
            queries.append({})
        seen: dict[str, dict] = {}
        for extra in queries:
            for notice in self._search(extra):
                nid = notice.get("noticeId")
                if nid and nid not in seen:
                    seen[nid] = notice
        items = []
        for notice in list(seen.values())[: self.max_results]:
            payload = notice_to_payload(notice, self._description(notice))
            items.append(normalize(self.source_name, payload))
        self._last_note = f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'}, {len(seen)} unique notices"
        return items

    def fetch_opportunity_details(self, external_id: str) -> NormalizedOpportunity | None:
        if not self.api_key:
            return None
        notices = self._search({"noticeid": external_id})
        if not notices:
            return None
        return normalize(self.source_name, notice_to_payload(notices[0], self._description(notices[0])))


def _strip_html(text: str) -> str:
    import re
    from html import unescape
    return unescape(re.sub(r"<[^>]+>", " ", text))
