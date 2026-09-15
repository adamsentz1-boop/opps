from __future__ import annotations

import logging

from app.config import get_settings
from app.models import Notification
from app.notifications.base import NotificationAdapter, NotificationMessage

log = logging.getLogger(__name__)


class DashboardAdapter(NotificationAdapter):
    """Stores notifications in the database; shown in the dashboard bell."""
    name = "dashboard"

    def send(self, db, message: NotificationMessage) -> bool:
        db.add(Notification(level=message.level, title=message.title, body=message.body,
                            opportunity_id=message.opportunity_id, channel=self.name))
        db.flush()
        return True


class _ExternalStub(NotificationAdapter):
    """External channels are designed but disabled: they require explicit configuration AND the owner
    opting in via NOTIFY_ADAPTERS. Even then this stub only logs; implement the transport when you enable it."""
    name = "external"
    required_env: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        settings = get_settings()
        return all(getattr(settings, key, "") for key in self.required_env)

    def send(self, db, message: NotificationMessage) -> bool:
        if not self.configured:
            log.info("%s adapter not configured; skipping external send", self.name)
            return False
        log.info("%s adapter would send: %s (transport not implemented in Phase 1)", self.name, message.title)
        return False


class NtfyAdapter(_ExternalStub):
    name = "ntfy"
    required_env = ("ntfy_url", "ntfy_topic")


class EmailAdapter(_ExternalStub):
    name = "email"


class SlackAdapter(_ExternalStub):
    name = "slack"


class SMSAdapter(_ExternalStub):
    name = "sms"


ADAPTERS: dict[str, type[NotificationAdapter]] = {
    "dashboard": DashboardAdapter, "ntfy": NtfyAdapter, "email": EmailAdapter, "slack": SlackAdapter, "sms": SMSAdapter,
}
