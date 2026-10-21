import os, logging
import httpx
from . import settings

logger = logging.getLogger("notifications")

def notify_slack(title: str, message: str, url: str | None = None, status: str = "info"):
    """
    Sends a formatted Slack notification message via webhook.
    Status can be: 'success' (green), 'error' (red), 'info' (blue).
    """
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return

    color_map = {
        "success": "#2eb886",
        "error": "#e01e5a",
        "info": "#2196F3"
    }
    color = color_map.get(status, "#2196F3")

    payload = {
        "attachments": [
            {
                "color": color,
                "title": title,
                "title_link": url if url else None,
                "text": message,
                "footer": "Snipcast Pipeline",
                "ts": int(os.environ.get("TIMESTAMP", 0)) or None
            }
        ]
    }
    # Clean out None fields
    if not payload["attachments"][0]["title_link"]:
        del payload["attachments"][0]["title_link"]
    if not payload["attachments"][0]["ts"]:
        del payload["attachments"][0]["ts"]

    try:
        with httpx.Client(timeout=10) as client:
            client.post(webhook, json=payload)
    except Exception as e:
        logger.warning("Failed to send Slack alert: %s", e)

