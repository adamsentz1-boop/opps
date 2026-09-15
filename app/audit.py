"""Audit log helper. Every important state change goes through here."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def log_event(db: Session, *, agent: str, action: str, object_type: str, object_id: str | None,
              previous_state: str | None = None, new_state: str | None = None,
              human_approval_required: bool = False, approval_id: str | None = None,
              details: dict[str, Any] | None = None) -> AuditLog:
    entry = AuditLog(agent=agent, action=action, object_type=object_type, object_id=object_id,
                     previous_state=previous_state, new_state=new_state,
                     human_approval_required=human_approval_required, approval_id=approval_id,
                     details=_safe(details or {}))
    db.add(entry)
    db.flush()
    return entry


_SECRET_MARKERS = ("api_key", "apikey", "secret", "token", "password", "authorization")


def _safe(details: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that looks like a credential before persisting."""
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if any(marker in key.lower() for marker in _SECRET_MARKERS):
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = _safe(value)
        else:
            clean[key] = value
    return clean
