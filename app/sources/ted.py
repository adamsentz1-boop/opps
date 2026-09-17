"""TED (Tenders Electronic Daily) - EU public procurement notices, via the official Search API v3.

Verified contract (TED developer docs / public API reference):
  POST https://api.ted.europa.eu/v3/notices/search      (no API key required)
  body: {"query": "<expert search>", "fields": [...], "limit": <=100, "scope": "ACTIVE",
         "paginationMode": "ITERATION"}
  expert search: field=value, FT~"phrase", PD>=YYYYMMDD, AND/OR, SORT BY <field> DESC
  response: {"notices": [ { "publication-number": ..., "notice-title": {"eng": [...]}, ... } ], ...}
Values are frequently multilingual objects keyed by ISO-639-3 language code, so every field is flattened.

If TED rejects the requested field list (HTTP 400), the adapter retries once with a minimal, known-good field
set and records TED's own message on the source run, rather than failing silently or guessing.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.normalizer import normalize
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource, SourceError

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{}"

#: Requested on every call. Confirmed against the public API reference.
MINIMAL_FIELDS = ["publication-number", "notice-title", "buyer-name", "buyer-country", "deadline"]
PREFERRED_FIELDS = MINIMAL_FIELDS + ["publication-date", "classification-cpv", "total-value",
                                     "total-value-cur", "notice-type", "contract-nature", "links"]

PREFERRED_LANGS = ("eng", "en")


def flat(value: Any, langs: tuple[str, ...] = PREFERRED_LANGS) -> str:
    """Flatten a TED value that may be a string, a list, or a dict keyed by language code."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join(x for x in (flat(v, langs) for v in value) if x).strip()
    if isinstance(value, dict):
        for lang in langs:
            if value.get(lang):
                return flat(value[lang], langs)
        for key in ("value", "text", "label"):
            if value.get(key):
                return flat(value[key], langs)
        for v in value.values():
            found = flat(v, langs)
            if found:
                return found
    return ""


def flat_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [x for x in (flat(v) for v in value) if x]
    text = flat(value)
    return [text] if text else []


def notice_to_payload(notice: dict[str, Any]) -> dict[str, Any]:
    number = flat(notice.get("publication-number")) or flat(notice.get("ND"))
    title = flat(notice.get("notice-title")) or "Untitled TED notice"
    buyer = flat(notice.get("buyer-name"))
    country = flat(notice.get("buyer-country"))
    cpv = flat_list(notice.get("classification-cpv"))
    value = flat(notice.get("total-value"))
    currency = flat(notice.get("total-value-cur")) or "EUR"
    notice_type = flat(notice.get("notice-type"))
    description = "\n".join(x for x in [
        f"Buyer: {buyer}" if buyer else "", f"Country: {country}" if country else "",
        f"Notice type: {notice_type}" if notice_type else "",
        f"Nature: {flat(notice.get('contract-nature'))}" if notice.get("contract-nature") else "",
        f"CPV: {', '.join(cpv)}" if cpv else "",
        f"Estimated value: {value} {currency}" if value else "",
        f"Deadline: {flat(notice.get('deadline'))}" if notice.get("deadline") else "",
        "", title,
    ] if x)
    try:
        amount = float(str(value).replace(",", "")) if value else None
    except ValueError:
        amount = None
    return {
        "external_id": number or title[:60],
        "title": title,
        "description": description,
        "buyer_name": buyer or None,
        "buyer_type": "government",
        "location": country or None,
        "country": country or None,
        "budget_type": "unknown",
        "currency": currency,
        "estimated_value": amount,
        "posted_at": flat(notice.get("publication-date")) or None,
        "deadline": flat(notice.get("deadline")) or None,
        "source_url": NOTICE_URL.format(number) if number else None,
        "opportunity_type": "bid",
        "agency": buyer or None,
        "solicitation_number": number or None,
        "notice_type": notice_type or None,
        "naics_code": None,
        "cpv_codes": cpv,
        "place_of_performance": country or None,
        "required_skills": cpv,
    }


class TEDSource(OpportunitySource):
    source_name = "ted"
    display_name = "TED - EU Tenders Electronic Daily"

    def __init__(self, cpv_codes: list[str] | None = None, keywords: list[str] | None = None,
                 countries: list[str] | None = None, days_back: int | None = None,
                 max_results: int | None = None, timeout: int = 45):
        s = get_settings()
        self.cpv_codes = cpv_codes if cpv_codes is not None else s.ted_cpv_list
        self.keywords = keywords if keywords is not None else s.ted_keyword_list
        self.countries = countries if countries is not None else s.ted_country_list
        self.days_back = days_back or s.ted_days_back
        self.max_results = max_results or s.ted_max_results
        self.timeout = timeout
        self.enabled = s.ted_enabled
        self._note = ""

    @property
    def requirements(self) -> str:
        return ("Set TED_ENABLED=true. The TED Search API is public and keyless; tune TED_CPV_CODES, "
                "TED_KEYWORDS and TED_COUNTRIES (ISO-3 codes, blank = all EU) in .env or Settings.")

    def status_note(self) -> str | None:
        return self._note or None

    def source_url(self, external_id: str) -> str | None:
        return NOTICE_URL.format(external_id)

    # ------------------------------------------------------------------ query building
    def _date_clause(self) -> str:
        since = (datetime.now(timezone.utc) - timedelta(days=self.days_back)).strftime("%Y%m%d")
        return f"publication-date>={since}"

    def _country_clause(self) -> str:
        if not self.countries:
            return ""
        joined = " OR ".join(f"buyer-country={c.upper()}" for c in self.countries)
        return f"({joined})"

    def build_queries(self) -> list[str]:
        """One query per subject clause, ANDed with date (and country) filters."""
        subject_clauses: list[str] = []
        if self.cpv_codes:
            subject_clauses.append(" OR ".join(f"classification-cpv={code}" for code in self.cpv_codes))
        for i in range(0, len(self.keywords), 4):
            chunk = self.keywords[i:i + 4]
            subject_clauses.append(" OR ".join(f'FT~"{k}"' for k in chunk))
        if not subject_clauses:
            subject_clauses = ['FT~"legal technology"']
        queries = []
        for clause in subject_clauses:
            parts = [f"({clause})", self._date_clause()]
            country = self._country_clause()
            if country:
                parts.append(country)
            queries.append(" AND ".join(parts) + " SORT BY publication-date DESC")
        return queries

    # ------------------------------------------------------------------ transport
    def _post(self, body: dict) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(SEARCH_URL, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "OpportunityEngine/0.1 (+local)"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - documented public API
                return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            raise SourceError(f"TED API HTTP {exc.code}: {detail}") from None
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"TED API request failed: {type(exc).__name__}") from None

    def _search(self, query: str) -> list[dict]:
        body = {"query": query, "fields": PREFERRED_FIELDS, "limit": min(self.max_results, 100),
                "scope": "ACTIVE", "paginationMode": "ITERATION"}
        try:
            data = self._post(body)
        except SourceError as exc:
            if "400" not in str(exc):
                raise
            # TED rejects unknown field names and lists the supported ones; retry with the minimal set.
            log.info("TED rejected the preferred field list, retrying with minimal fields: %s", exc)
            self._note = (self._note + " | " if self._note else "") + f"field list rejected, used minimal fields ({str(exc)[:120]})"
            body["fields"] = MINIMAL_FIELDS
            data = self._post(body)
        notices = data.get("notices") or data.get("results") or []
        return [n for n in notices if isinstance(n, dict)]

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        if not self.enabled:
            raise NotImplementedError("TED adapter disabled: " + self.requirements)
        seen: dict[str, dict] = {}
        queries = self.build_queries()
        errors: list[str] = []
        self._note = ""
        for query in queries:
            try:
                for notice in self._search(query):
                    key = flat(notice.get("publication-number")) or json.dumps(notice, sort_keys=True)[:80]
                    seen.setdefault(key, notice)
            except SourceError as exc:
                errors.append(str(exc)[:200])
        if errors and not seen:
            raise SourceError("; ".join(errors[:2]))
        summary = f"{len(queries)} queries, {len(seen)} unique notices" + (
            f" ({len(errors)} query error(s): {errors[0][:120]})" if errors else "")
        self._note = f"{summary} | {self._note}" if self._note else summary
        return [normalize(self.source_name, notice_to_payload(n)) for n in list(seen.values())[: self.max_results]]
