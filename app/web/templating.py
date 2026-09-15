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
        "AWAITING_OWNER_APPROVAL": "warn", "APPROVED": "good", "EXECUTED": "info", "EXPIRED": "muted",
        "CANCELLED": "muted", "PROPOSED": "warn", "ACTIVE": "good", "PAUSED": "muted", "COMPLETED": "info",
    }.get(status, "muted")


def money2(value) -> str:
    """Money with cents (the Market Challenge works in dollars and cents)."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def signed_money(value) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    return ("+" if value >= 0 else "-") + f"${abs(value):,.2f}"


def qty(value) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    text = f"{value:,.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def pnl_class(value) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "muted"
    return "good" if value > 0 else "bad" if value < 0 else "muted"


def score_class(score) -> str:
    try:
        score = float(score or 0)
    except (TypeError, ValueError):
        return "muted"
    return "good" if score >= 80 else "warn" if score >= 60 else "bad"


templates.env.filters.update({"money": money, "hours": hours, "pct": pct, "dt": dt,
                              "status_class": status_class, "score_class": score_class, "money2": money2,
                              "signed_money": signed_money, "qty": qty, "pnl_class": pnl_class})
