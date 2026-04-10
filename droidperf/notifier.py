"""
droidperf/notifier.py
---------------------
Outbound notification delivery for alert events.

Two delivery channels are supported:

1. **Webhook / Slack** — HTTP POST to a configurable URL with a JSON
   payload compatible with Slack Incoming Webhooks *and* generic webhook
   consumers.  The URL is read from ``settings.webhook_url``.

2. **OS Desktop Notification** — Uses ``plyer.notification.notify()``
   (cross-platform: Windows toast, macOS, Linux libnotify).  Enabled when
   ``settings.os_notifications`` is ``True``.  If ``plyer`` is not
   installed the call is silently skipped with a warning.

Both functions are designed to be called from a background thread.  They
do not touch any Tkinter widgets.

Public API:
    notify(event)   — send all enabled notifications for an AlertEvent
    send_webhook(event, url) -> bool
    send_os_notification(event) -> bool
"""

import json
import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from droidperf.alert_engine import AlertEvent

from droidperf.settings_manager import settings

logger = logging.getLogger(__name__)

_APP_TITLE = "ADB Telemetry Monitor"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(event: "AlertEvent") -> None:
    """
    Deliver all enabled notifications for *event*.

    Reads ``webhook_url`` and ``os_notifications`` from ``settings`` at
    call time so live changes to settings take effect immediately without
    restarting a session.

    Args:
        event (AlertEvent): The alert that fired.
    """
    url: str = settings.get("webhook_url", "").strip()
    if url:
        send_webhook(event, url)

    if settings.get("os_notifications", False):
        send_os_notification(event)


def send_webhook(event: "AlertEvent", url: str) -> bool:
    """
    POST a JSON payload to *url* (Slack-compatible format).

    Slack Incoming Webhook format:  ``{"text": "..."}``
    Generic consumers also receive ``event_kind``, ``metric``, ``value``,
    ``threshold``, ``package``, and ``device_id`` fields.

    Args:
        event (AlertEvent): Alert to report.
        url (str):          Webhook URL.

    Returns:
        bool: ``True`` on HTTP 2xx, ``False`` otherwise.
    """
    payload = {
        "text": f"[{_APP_TITLE}] {event.message}",
        "event_kind": event.kind,
        "metric": event.metric,
        "value": event.value,
        "threshold": event.threshold,
        "package": event.package,
        "device_id": event.device_id,
    }
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ADBTelemetryMonitor/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            if 200 <= status < 300:
                logger.info("Webhook delivered (HTTP %d): %s", status, url)
                return True
            logger.warning("Webhook returned non-2xx status %d for URL: %s", status, url)
            return False

    except urllib.error.HTTPError as exc:
        logger.error("Webhook HTTP error %d for URL %s: %s", exc.code, url, exc.reason)
        return False
    except urllib.error.URLError as exc:
        logger.error("Webhook URL error for %s: %s", url, exc.reason)
        return False
    except OSError as exc:
        logger.error("Webhook network error: %s", exc)
        return False
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Unexpected error sending webhook: %s", exc)
        return False


def send_os_notification(event: "AlertEvent") -> bool:
    """
    Show an OS desktop notification via ``plyer``.

    Falls back gracefully when ``plyer`` is not installed.

    Args:
        event (AlertEvent): Alert to display.

    Returns:
        bool: ``True`` if the notification was dispatched, ``False`` if
              ``plyer`` is unavailable or the call failed.
    """
    try:
        from plyer import notification  # type: ignore  # optional dependency

        kind_label = "SPIKE DETECTED" if event.kind == "spike" else "ALERT"
        notification.notify(
            title=f"{_APP_TITLE} — {kind_label}",
            message=event.message,
            app_name=_APP_TITLE,
            timeout=6,
        )
        logger.debug("OS notification sent: %s", event.message)
        return True

    except ImportError:
        logger.warning(
            "plyer not installed — OS notifications disabled. "
            "Install with: pip install plyer"
        )
        return False
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("OS notification failed: %s", exc)
        return False
