"""Flask routes for the Slack Events API, wired to slack_bolt handlers."""

import logging

from flask import Flask, request
from slack_bolt.adapter.flask import SlackRequestHandler

from app.db.models import SessionLocal, init_db
from app.slack.client import bolt_app
from app.slack.dm_agent import handle_dm_message
from app.slack.reaction_tracker import handle_reaction_change, handle_thread_reply

logger = logging.getLogger(__name__)


def _on_reaction_event(event: dict, client) -> None:
    item = event.get("item", {})
    if item.get("type") != "message":
        return
    channel = item["channel"]
    ts = item["ts"]
    with SessionLocal() as session:
        handle_reaction_change(session, client, channel, ts)


@bolt_app.event("reaction_added")
def on_reaction_added(event, client):
    _on_reaction_event(event, client)


@bolt_app.event("reaction_removed")
def on_reaction_removed(event, client):
    # Keep counts accurate so a message can drop back below threshold before
    # it has ever fired. Once triggered, it stays triggered.
    _on_reaction_event(event, client)


@bolt_app.event("message")
def on_message(event, client):
    # Skip bot messages and most subtypes (edits, joins, etc.). file_share is
    # allowed through: DM replies with attachments arrive with that subtype.
    if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
        return

    # DMs go to the info-gathering agent (Phase 2).
    if event.get("channel_type") == "im":
        with SessionLocal() as session:
            handle_dm_message(session, client, event)
        return

    # Channel thread replies count as potential support (Phase 1).
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts == event.get("ts"):
        return
    with SessionLocal() as session:
        handle_thread_reply(
            session,
            client,
            channel=event["channel"],
            thread_ts=thread_ts,
            reply_ts=event["ts"],
            replier=event.get("user", ""),
            text=event.get("text", ""),
        )


def create_app() -> Flask:
    from app.review.webhook import register_github_webhook

    init_db()
    flask_app = Flask(__name__)
    handler = SlackRequestHandler(bolt_app)
    register_github_webhook(flask_app, slack_client=bolt_app.client)

    @flask_app.route("/slack/events", methods=["POST"])
    def slack_events():
        return handler.handle(request)

    @flask_app.route("/health", methods=["GET"])
    def health():
        return {"ok": True}

    return flask_app
