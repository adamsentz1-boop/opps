"""Notification abstraction. Adapters never send anything externally unless explicitly configured."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class NotificationMessage:
    title: str
    body: str
    level: str = "info"
    opportunity_id: str | None = None
    meta: dict = field(default_factory=dict)


class NotificationAdapter(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def send(self, db, message: NotificationMessage) -> bool:
        """Deliver the message. Return True if delivered."""

    @property
    def configured(self) -> bool:
        return True
