from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def money(value) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    return f"${value:,.0f}"


def hours(value) -> str:
    try:
        return f"{float(value or 0):.1f}h"
    except (TypeError, ValueError):
        return "-"


def pct(value) -> str:
    try:
        return f"{float(value or 0):.0f}%"
    except (TypeError, ValueError):
        return "-"


def dt(value) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def status_class(status: str) -> str:
    return {
        "AWAITING_APPROVAL": "warn", "READY_TO_SUBMIT": "good", "SUBMITTED": "info", "INTERVIEWING": "info",
        "WON": "good", "LOST": "muted", "REJECTED": "muted", "DECLINED": "muted", "ERROR": "bad",
        "QUALIFIED": "info", "NEW": "muted", "QUALIFYING": "muted", "PAID": "good", "DELIVERED": "good",
        "AWAITING_OWNER_APPROVAL": "warn",
    }.get(status, "muted")


def score_class(score) -> str:
    try:
        score = float(score or 0)
    except (TypeError, ValueError):
        return "muted"
    return "good" if score >= 80 else "warn" if score >= 60 else "bad"


templates.env.filters.update({"money": money, "hours": hours, "pct": pct, "dt": dt,
                              "status_class": status_class, "score_class": score_class})
