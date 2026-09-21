"""SAM.gov federal contract opportunities via the official public API.

This is the sanctioned route: https://open.gsa.gov/api/get-opportunities-public-api/ . It needs a free
api.data.gov key (`SAM_GOV_API_KEY`). Nothing here scrapes sam.gov - every request goes to the documented
JSON endpoint, and without a key the adapter reports what is required and is skipped, exactly like the
other unconfigured sources.

Bidding on anything found here still requires an active SAM.gov entity registration (UEI). That is an owner
task, surfaced as a ComplianceRequirement by the RequirementsAgent, and it blocks approval until verified.

One request is issued per configured NAICS code because the API filters on a single code per call. Results
are mapped into the normalised opportunity schema and then, in the registry, passed through the contract-AI
relevance filter - a NAICS search alone returns far too much unrelated work.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any

from app.config import get_settings
from app.normalizer import normalize
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource, SourceError
from app.sources.rss import _strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
REQUIREMENTS = (
    "Official public API: https://open.gsa.gov/api/get-opportunities-public-api/ . Set SAM_GOV_API_KEY to a free "
    "api.data.gov key. Tune SAM_GOV_NAICS, SAM_GOV_PTYPES and SAM_GOV_POSTED_DAYS. Submitting a bid additionally "
    "requires an active SAM.gov entity registration (UEI) - an owner step, never an automated one."
)

#: Notice-type codes the API accepts for `ptype`, for documentation and validation.
NOTICE_TYPES = {"o": "Solicitation", "p": "Presolicitation", "k": "Combined Synopsis/Solicitation",
                "r": "Sources Sought", "s": "Special Notice", "i": "Intent to Bundle",
                "a": "Award Notice", "u": "Justification", "g": "Sale of Surplus"}


def _scrub(text: str, secret: str) -> str:
    """Never let the API key reach a log line, a SourceRun row or the dashboard."""
    if secret:
        text = text.replace(secret, "[redacted]")
    return text


class SamGovSource(OpportunitySource):
    source_name = "sam_gov"
    display_name = "SAM.gov (federal contract opportunities)"

    def __init__(self, api_key: str | None = None, naics: list[str] | None = None,
                 notice_types: list[str] | None = None, posted_days: int | None = None,
                 limit: int | None = None, fetch_descriptions: bool | None = None, timeout: int = 30):
        settings = get_settings()
        self.api_key = (api_key if api_key is not None else settings.sam_gov_api_key).strip()
        self.naics = naics if naics is not None else settings.sam_gov_naics_codes
        self.notice_types = notice_types if notice_types is not None else settings.sam_gov_notice_types
        self.posted_days = posted_days if posted_days is not None else settings.sam_gov_posted_days
        self.limit = limit if limit is not None else settings.sam_gov_limit
        self.fetch_descriptions = (settings.sam_gov_fetch_descriptions if fetch_descriptions is None
                                   else fetch_descriptions)
        self.timeout = timeout
        self._note: str | None = None

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return bool(self.api_key)

    @property
    def requirements(self) -> str:
        return REQUIREMENTS

    def source_url(self, external_id: str) -> str | None:
        return f"https://sam.gov/opp/{external_id}/view"

    def status_note(self) -> str | None:
        return self._note

    # ------------------------------------------------------------------ http
    def _get_json(self, url: str, params: dict[str, Any]) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        full = f"{url}?{query}"
        req = urllib.request.Request(full, headers={"User-Agent": "OpportunityEngine/0.1 (+local)",
                                                    "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed official endpoint
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            raise SourceError(_scrub(f"SAM.gov request failed: {type(exc).__name__}: {exc}", self.api_key)) from exc

    def _description_text(self, notice: dict) -> str:
        """`description` is a URL to the notice body in v2; fetch it when enabled, else fall back to fields."""
        raw = notice.get("description")
        if isinstance(raw, str) and raw.startswith(("http://", "https://")):
            if not self.fetch_descriptions:
                return ""
            try:
                payload = self._get_json(raw, {"api_key": self.api_key})
            except SourceError as exc:
                log.info("SAM.gov description unavailable for %s: %s", notice.get("noticeId"), exc)
                return ""
            body = payload.get("description") if isinstance(payload, dict) else payload
            return _strip_html(body if isinstance(body, str) else "")
        if isinstance(raw, str):
            return _strip_html(raw)
        return ""

    # ------------------------------------------------------------------ mapping
    def _to_payload(self, notice: dict) -> dict[str, Any]:
        notice_id = str(notice.get("noticeId") or "").strip()
        office = notice.get("fullParentPathName") or notice.get("department") or notice.get("subTier")
        place = notice.get("placeOfPerformance") or {}
        if isinstance(place, dict):
            city = (place.get("city") or {}).get("name") if isinstance(place.get("city"), dict) else place.get("city")
            state = (place.get("state") or {}).get("name") if isinstance(place.get("state"), dict) else place.get("state")
            location = ", ".join(str(p) for p in (city, state) if p) or None
        else:
            location = None
        contacts = notice.get("pointOfContact") or []
        contact = contacts[0] if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict) else {}
        description = self._description_text(notice)
        header = " | ".join(str(p) for p in [
            f"Solicitation {notice.get('solicitationNumber')}" if notice.get("solicitationNumber") else None,
            f"Type: {notice.get('type')}" if notice.get("type") else None,
            f"NAICS {notice.get('naicsCode')}" if notice.get("naicsCode") else None,
            f"PSC {notice.get('classificationCode')}" if notice.get("classificationCode") else None,
            f"Set-aside: {notice.get('typeOfSetAsideDescription')}" if notice.get("typeOfSetAsideDescription") else None,
        ] if p)
        return {
            "external_id": notice_id or str(notice.get("solicitationNumber") or ""),
            "title": notice.get("title") or "Untitled SAM.gov notice",
            "description": "\n\n".join(p for p in [header, description] if p),
            "buyer_name": office,
            "buyer_type": "government",
            "location": location,
            "posted_at": notice.get("postedDate"),
            "deadline": notice.get("responseDeadLine"),
            "source_url": notice.get("uiLink") or self.source_url(notice_id),
            "tags": [str(t) for t in [notice.get("naicsCode"), notice.get("classificationCode")] if t],
            # kept for the RequirementsAgent and later compliance work
            "solicitation_number": notice.get("solicitationNumber"),
            "notice_type": notice.get("type"),
            "set_aside": notice.get("typeOfSetAsideDescription"),
            "set_aside_code": notice.get("typeOfSetAside"),
            "naics_code": notice.get("naicsCode"),
            "classification_code": notice.get("classificationCode"),
            "archive_date": notice.get("archiveDate"),
            "contact_email": contact.get("email"),
            "organization_type": notice.get("organizationType"),
        }

    # ------------------------------------------------------------------ fetch
    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        if not self.api_key:
            raise NotImplementedError(f"{self.display_name} is not configured. Requirements: {REQUIREMENTS}")
        from app.models import utcnow

        now = utcnow()
        posted_from = (now - timedelta(days=max(1, self.posted_days))).strftime("%m/%d/%Y")
        posted_to = now.strftime("%m/%d/%Y")
        ptype = ",".join(self.notice_types) if self.notice_types else None

        items: list[NormalizedOpportunity] = []
        seen: set[str] = set()
        total_seen = 0
        for code in self.naics or [None]:
            params = {"api_key": self.api_key, "postedFrom": posted_from, "postedTo": posted_to,
                      "limit": min(int(self.limit), 1000), "offset": 0, "ptype": ptype, "ncode": code}
            payload = self._get_json(SEARCH_URL, params)
            notices = payload.get("opportunitiesData") or payload.get("opportunities") or []
            if not isinstance(notices, list):
                raise SourceError(f"Unexpected SAM.gov payload for NAICS {code}: no opportunitiesData list")
            total_seen += len(notices)
            for notice in notices:
                if not isinstance(notice, dict):
                    continue
                mapped = self._to_payload(notice)
                key = mapped["external_id"]
                if not key or key in seen:
                    continue
                seen.add(key)
                items.append(normalize(self.source_name, mapped))
        self._note = (f"{len(items)} notices from {len(self.naics)} NAICS code(s), "
                      f"posted {posted_from}-{posted_to}, types {ptype or 'all'}")
        return items
