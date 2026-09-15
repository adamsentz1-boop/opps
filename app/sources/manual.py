"""ManualSource: opportunities entered by the owner (dashboard form, API, seed script, JSON file).

Items are queued in memory (or loaded from `data/manual_inbox.json`) and drained on the next scan.
The dashboard "Add opportunity" form bypasses the queue and normalises immediately.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.normalizer import normalize
from app.schemas import NormalizedOpportunity
from app.sources.base import OpportunitySource

INBOX_PATH = BASE_DIR / "data" / "manual_inbox.json"


class ManualSource(OpportunitySource):
    source_name = "manual"
    display_name = "Manual entry"

    def __init__(self, inbox_path: Path | None = None):
        self._queue: list[dict[str, Any]] = []
        self.inbox_path = inbox_path or INBOX_PATH

    def add(self, payload: dict[str, Any]) -> NormalizedOpportunity:
        item = normalize(self.source_name, payload)
        self._queue.append(payload)
        return item

    def fetch_new_opportunities(self) -> list[NormalizedOpportunity]:
        payloads = list(self._queue)
        self._queue.clear()
        if self.inbox_path.exists():
            try:
                data = json.loads(self.inbox_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    payloads.extend(p for p in data if isinstance(p, dict))
                self.inbox_path.write_text("[]", encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass
        return [normalize(self.source_name, p) for p in payloads]

    def status_note(self) -> str | None:
        return f"inbox: {self.inbox_path.name}"


def normalize_manual(payload: dict[str, Any]) -> NormalizedOpportunity:
    return normalize("manual", payload)
