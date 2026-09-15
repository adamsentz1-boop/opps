"""Convert arbitrary source payloads into the internal NormalizedOpportunity schema and persist them."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.audit import log_event
from app.enums import OpportunityStatus
from app.models import Opportunity
from app.sanitize import clean_text, detect_injection
from app.schemas import NormalizedOpportunity

_BUDGET_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s*(?:-|to|–)\s*\$?\s?(\d[\d,]*(?:\.\d+)?))?\s*(/\s?hr|per hour|/hour|an hour|hourly)?", re.I)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%d %b %Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def parse_budget(text: str | None) -> tuple[float | None, float | None, str]:
    """Best-effort budget extraction from free text. Returns (min, max, type)."""
    if not text:
        return None, None, "unknown"
    match = _BUDGET_RE.search(text)
    if not match:
        return None, None, "unknown"
    low = float(match.group(1).replace(",", ""))
    high = float(match.group(2).replace(",", "")) if match.group(2) else low
    budget_type = "hourly" if match.group(3) else "fixed"
    return min(low, high), max(low, high), budget_type


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[,;\n]", value) if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def normalize(source: str, payload: dict[str, Any], field_map: dict[str, str] | None = None) -> NormalizedOpportunity:
    """Map a raw dict into the normalised schema. `field_map` maps internal field -> payload key."""
    field_map = field_map or {}

    def pick(field: str, *fallbacks: str, default: Any = None) -> Any:
        keys = [field_map.get(field, field), *fallbacks]
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        return default

    title = clean_text(pick("title", "name", "subject", default="Untitled"), 500)
    description = clean_text(pick("description", "summary", "body", "content", "text", default=""))
    raw_text = clean_text(pick("raw_text", default="") or f"{title}\n\n{description}")
    budget_min = pick("budget_min", "min_budget", "budget")
    budget_max = pick("budget_max", "max_budget", "budget")
    budget_type = pick("budget_type", "type", default=None)
    if budget_min is None and budget_max is None:
        b_min, b_max, b_type = parse_budget(f"{title}\n{description}")
        budget_min, budget_max = b_min, b_max
        budget_type = budget_type or b_type
    try:
        budget_min = float(str(budget_min).replace(",", "").replace("$", "")) if budget_min is not None else None
        budget_max = float(str(budget_max).replace(",", "").replace("$", "")) if budget_max is not None else None
    except ValueError:
        budget_min, budget_max = None, None
    if budget_type not in ("fixed", "hourly", "unknown"):
        budget_type = "fixed" if (budget_min or budget_max) else "unknown"
    external_id = str(pick("external_id", "id", "guid", "url", "link", default="") or "")
    if not external_id:
        import hashlib
        external_id = hashlib.sha1(f"{title}|{description[:200]}".encode()).hexdigest()[:20]
    return NormalizedOpportunity(
        external_id=external_id, source=source, title=title, description=description,
        buyer_name=clean_text(pick("buyer_name", "client", "company", "author", default=None), 255) or None,
        buyer_type=str(pick("buyer_type", default="unknown") or "unknown"),
        location=clean_text(pick("location", default=None), 255) or None,
        budget_min=budget_min, budget_max=budget_max, currency=str(pick("currency", default="USD") or "USD"),
        budget_type=budget_type, posted_at=parse_datetime(pick("posted_at", "published", "pubDate", "date")),
        deadline=parse_datetime(pick("deadline", "due", "closes_at")),
        source_url=pick("source_url", "url", "link"), required_skills=_as_list(pick("required_skills", "skills", "tags")),
        deliverables=_as_list(pick("deliverables")), raw_text=raw_text,
        raw_payload={k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool, list, dict)) or v is None},
        opportunity_type="bid" if pick("opportunity_type", default="freelance") == "bid" else "freelance",
        agency=clean_text(pick("agency", default=None), 255) or None,
        solicitation_number=clean_text(pick("solicitation_number", default=None), 128) or None,
        notice_type=clean_text(pick("notice_type", default=None), 64) or None,
        set_aside=clean_text(pick("set_aside", default=None), 128) or None,
        naics_code=clean_text(pick("naics_code", default=None), 16) or None,
        estimated_value=_to_float(pick("estimated_value", default=None)),
        place_of_performance=clean_text(pick("place_of_performance", default=None), 255) or None,
    )


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def upsert_opportunity(db: Session, item: NormalizedOpportunity) -> tuple[Opportunity, bool]:
    """Insert a normalised opportunity, or return the existing one. Returns (opportunity, created)."""
    existing = db.query(Opportunity).filter_by(source=item.source, external_id=item.external_id).one_or_none()
    if existing:
        return existing, False
    flags = sorted(set(detect_injection(item.title) + detect_injection(item.description) + detect_injection(item.raw_text)))
    opp = Opportunity(**item.model_dump(exclude={"raw_payload"}), raw_payload=item.raw_payload,
                      status=OpportunityStatus.NEW.value, injection_flags=flags)
    db.add(opp)
    db.flush()
    log_event(db, agent="ScoutAgent", action="opportunity.discovered", object_type="opportunity", object_id=opp.id,
              new_state=opp.status, details={"source": opp.source, "title": opp.title, "injection_flags": flags})
    urls = item.raw_payload.get("attachment_urls") or []
    if urls:
        from app.documents import add_attachment
        for url in urls[:10]:
            add_attachment(db, opp, filename="", source_url=url)
    return opp, True
