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
    """Push notifications via a self-hosted or public ntfy server.

    Sends ONLY when (a) `ntfy` is listed in NOTIFY_ADAPTERS and (b) NTFY_URL and NTFY_TOPIC are set. The
    payload is the notification text (no secrets, no proposal bodies). Failures never break the pipeline.
    """
    name = "ntfy"
    required_env = ("ntfy_url", "ntfy_topic")

    def send(self, db, message: NotificationMessage) -> bool:
        if not self.configured:
            log.info("ntfy adapter not configured; skipping external send")
            return False
        import urllib.request
        settings = get_settings()
        url = settings.ntfy_url.rstrip("/") + "/" + settings.ntfy_topic.strip("/")
        headers = {"Title": message.title.encode("ascii", "ignore").decode(), "Content-Type": "text/plain; charset=utf-8",
                   "Priority": "high" if message.level == "opportunity" else "default"}
        req = urllib.request.Request(url, data=message.body.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - owner-configured URL
                return 200 <= resp.status < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy send failed: %s", exc)
            return False


class EmailAdapter(_ExternalStub):
    name = "email"


class SlackAdapter(_ExternalStub):
    name = "slack"


class SMSAdapter(_ExternalStub):
    name = "sms"


ADAPTERS: dict[str, type[NotificationAdapter]] = {
    "dashboard": DashboardAdapter, "ntfy": NtfyAdapter, "email": EmailAdapter, "slack": SlackAdapter, "sms": SMSAdapter,
}
