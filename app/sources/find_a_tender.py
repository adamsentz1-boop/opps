"""UK Find a Tender Service (FTS) - OCDS release packages.

Verified contract (FTS API documentation):
  GET https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages
      ?updatedFrom=YYYY-MM-DDTHH:MM:SS&updatedTo=YYYY-MM-DDTHH:MM:SS&limit=100[&stages=tender][&cursor=...]
  No API key. Response is an OCDS 1.1 release package: {"releases": [...], "links": {"next": "..."}}.

Parsing is defensive: any missing branch of the OCDS tree degrades to an absent field, never an exception.
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
from app.normalizer import normalize
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource, SourceError

log = logging.getLogger(__name__)

BASE_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"


def _get(node: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
        if node is None:
            return default
    return node


def release_to_payload(release: dict[str, Any]) -> dict[str, Any]:
    tender = release.get("tender") or {}
    title = _get(tender, "title", default="") or "Untitled tender"
    description = _get(tender, "description", default="") or ""
    buyer = _get(release, "buyer", "name", default=None)
    amount = _get(tender, "value", "amount")
    currency = _get(tender, "value", "currency", default="GBP") or "GBP"
    if amount is None:
        amount = _get(tender, "minValue", "amount")
    deadline = _get(tender, "tenderPeriod", "endDate")
    classifications: list[str] = []
    main = _get(tender, "classification", "id")
    if main:
        classifications.append(str(main))
    for item in (tender.get("items") or []):
        code = _get(item, "classification", "id")
        if code:
            classifications.append(str(code))
        for extra in (item.get("additionalClassifications") or []):
            if isinstance(extra, dict) and extra.get("id"):
                classifications.append(str(extra["id"]))
    url = None
    for doc in (tender.get("documents") or []):
        if isinstance(doc, dict) and doc.get("url"):
            url = doc["url"]
            break
    region = None
    for site in (tender.get("deliveryAddresses") or []):
        if isinstance(site, dict) and (site.get("region") or site.get("locality")):
            region = site.get("region") or site.get("locality")
            break
    ocid = release.get("ocid") or ""
    body = "\n".join(x for x in [
        f"Buyer: {buyer}" if buyer else "",
        f"OCID: {ocid}" if ocid else "",
        f"Procurement method: {tender.get('procurementMethod') or ''}",
        f"Category: {tender.get('mainProcurementCategory') or ''}",
        f"CPV: {', '.join(sorted(set(classifications)))}" if classifications else "",
        f"Value: {amount} {currency}" if amount is not None else "Value: not stated",
        f"Deadline: {deadline}" if deadline else "",
        f"Delivery region: {region}" if region else "",
        "", description,
    ] if x.strip())
    return {
        "external_id": str(release.get("id") or ocid or title[:60]),
        "title": str(title),
        "description": body,
        "buyer_name": buyer,
        "buyer_type": "government",
        "location": region or "United Kingdom",
        "country": "GBR",
        "currency": currency,
        "budget_type": "unknown",
        "estimated_value": float(amount) if isinstance(amount, (int, float)) else None,
        "posted_at": release.get("date"),
        "deadline": deadline,
        "source_url": url,
        "opportunity_type": "bid",
        "agency": buyer,
        "solicitation_number": str(ocid) or None,
        "notice_type": tender.get("status") or None,
        "cpv_codes": sorted(set(classifications)),
        "required_skills": sorted(set(classifications)),
        "place_of_performance": region or "United Kingdom",
    }


class FindATenderSource(OpportunitySource):
    source_name = "find_a_tender"
    display_name = "UK Find a Tender Service (OCDS)"

    def __init__(self, days_back: int | None = None, max_results: int | None = None, timeout: int = 45,
                 base_url: str = BASE_URL):
        s = get_settings()
        self.days_back = days_back or s.fts_days_back
        self.max_results = max_results or s.fts_max_results
        self.timeout = timeout
        self.base_url = base_url
        self.enabled = s.fts_enabled
        self._note = ""

    @property
    def requirements(self) -> str:
        return ("Set FTS_ENABLED=true. The Find a Tender OCDS API is public and keyless. Results are filtered "
                "locally by the legal-technology classifier, so no query tuning is required.")

    def status_note(self) -> str | None:
        return self._note or None

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "OpportunityEngine/0.1 (+local)"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - documented public API
                return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise SourceError(f"Find a Tender HTTP {exc.code}: {detail}") from None
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"Find a Tender request failed: {type(exc).__name__}") from None

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        if not self.enabled:
            raise NotImplementedError("Find a Tender adapter disabled: " + self.requirements)
        now = datetime.now(timezone.utc)
        params = {"updatedFrom": (now - timedelta(days=self.days_back)).strftime("%Y-%m-%dT%H:%M:%S"),
                  "updatedTo": now.strftime("%Y-%m-%dT%H:%M:%S"),
                  "limit": str(min(self.max_results, 100)), "stages": "tender"}
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        releases: list[dict] = []
        pages = 0
        while url and len(releases) < self.max_results and pages < 5:
            data = self._get_json(url)
            batch = [r for r in (data.get("releases") or []) if isinstance(r, dict)]
            releases.extend(batch)
            pages += 1
            nxt = _get(data, "links", "next")
            url = nxt if isinstance(nxt, str) and nxt.startswith("https://") and batch else None
        self._note = f"{pages} page(s), {len(releases)} release(s)"
        seen: dict[str, dict] = {}
        for release in releases[: self.max_results]:
            seen.setdefault(str(release.get("id") or release.get("ocid") or len(seen)), release)
        return [normalize(self.source_name, release_to_payload(r)) for r in seen.values()]
