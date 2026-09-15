"""GenericJSONSource: consume a JSON endpoint (or local file) returning a list of opportunity objects.

Field mapping lets you adapt arbitrary payloads: {"title": "jobTitle", "description": "details", ...}.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from app.normalizer import normalize
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource, SourceError


class GenericJSONSource(OpportunitySource):
    display_name = "Generic JSON feed"

    def __init__(self, url_or_path: str, field_map: dict[str, str] | None = None, items_key: str | None = None,
                 source_name: str | None = None, timeout: int = 20):
        self.url_or_path = url_or_path
        self.field_map = field_map or {}
        self.items_key = items_key
        self.timeout = timeout
        self.source_name = source_name or f"json:{re.sub(r'[^a-z0-9]+', '-', url_or_path.lower())[-40:].strip('-')}"

    def _load(self) -> Any:
        if self.url_or_path.startswith(("http://", "https://")):
            req = urllib.request.Request(self.url_or_path, headers={"User-Agent": "OpportunityEngine/0.1 (+local)",
                                                                    "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001
                raise SourceError(f"Could not fetch JSON {self.url_or_path}: {exc}") from exc
        path = Path(self.url_or_path)
        if not path.exists():
            raise SourceError(f"JSON file not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceError(f"Invalid JSON in {path}: {exc}") from exc

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        data = self._load()
        if self.items_key and isinstance(data, dict):
            data = data.get(self.items_key, [])
        if isinstance(data, dict):
            for key in ("items", "results", "data", "jobs", "opportunities"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise SourceError("JSON payload is not a list of opportunities")
        return [normalize(self.source_name, item, self.field_map) for item in data if isinstance(item, dict)]

    def status_note(self) -> str | None:
        return self.url_or_path
