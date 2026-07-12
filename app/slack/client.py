"""slack_bolt app instance and thin helper wrappers around the Web API."""

import logging

from slack_bolt import App

from app.config import settings

logger = logging.getLogger(__name__)

bolt_app = App(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
    # Skip the auth.test call at startup so the app can be imported in tests
    # and local dev without real credentials.
    token_verification_enabled=False,
)


def get_distinct_reactors(client, channel: str, ts: str) -> set[str]:
    """Distinct users who reacted to a message, across all emoji."""
    resp = client.reactions_get(channel=channel, timestamp=ts)
    message = resp.get("message", {}) if hasattr(resp, "get") else resp["message"]
    reactors: set[str] = set()
    for reaction in message.get("reactions", []) or []:
        reactors.update(reaction.get("users", []))
    return reactors


def fetch_message(client, channel: str, ts: str) -> dict:
    """Fetch a single message (text + author) via conversations.history."""
    resp = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
    messages = resp.get("messages", [])
    return messages[0] if messages else {}
