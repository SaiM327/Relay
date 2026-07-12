"""Threshold logic and dedupe state for support tracking (Phase 1).

A message's support count is the number of distinct users who either reacted
to it or replied positively in its thread. The original author is excluded,
so you can't boost your own idea.

Pure-ish core: takes a DB session and a Slack Web API client, so it can be
unit-tested with a fake client and an in-memory SQLite session.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import PositiveReply, TrackedMessage
from app.slack.classifier import is_positive_reply
from app.slack.client import fetch_message, get_distinct_reactors

logger = logging.getLogger(__name__)


def _upsert_tracked_message(session: Session, client, channel: str, ts: str) -> TrackedMessage:
    row = session.execute(
        select(TrackedMessage).where(
            TrackedMessage.slack_channel_id == channel,
            TrackedMessage.slack_message_ts == ts,
        )
    ).scalar_one_or_none()

    if row is None:
        message = fetch_message(client, channel, ts)
        row = TrackedMessage(
            slack_channel_id=channel,
            slack_message_ts=ts,
            text=message.get("text", ""),
            author_slack_id=message.get("user", ""),
        )
        session.add(row)
        session.flush()
    return row


def _distinct_supporters(session: Session, client, row: TrackedMessage) -> set[str]:
    reactors = get_distinct_reactors(client, row.slack_channel_id, row.slack_message_ts)
    repliers = set(
        session.execute(
            select(PositiveReply.replier_slack_id).where(
                PositiveReply.tracked_message_id == row.id
            )
        ).scalars()
    )
    return (reactors | repliers) - {row.author_slack_id}


def _evaluate_threshold(session: Session, client, row: TrackedMessage) -> bool:
    """Recompute support count and fire the pipeline once when it crosses.

    Returns True if the threshold was crossed for the first time.
    """
    row.reaction_count = len(_distinct_supporters(session, client, row))

    fired = False
    if row.reaction_count >= settings.reaction_threshold and not row.triggered:
        row.triggered = True
        fired = True
        logger.info(
            "Threshold crossed (%d/%d) for message %s in %s — triggering pipeline",
            row.reaction_count,
            settings.reaction_threshold,
            row.slack_message_ts,
            row.slack_channel_id,
        )
        _fire_pipeline(session, client, row)

    session.commit()
    return fired


def handle_reaction_change(session: Session, client, channel: str, ts: str) -> bool:
    """Process a reaction_added/reaction_removed event for a message."""
    row = _upsert_tracked_message(session, client, channel, ts)
    return _evaluate_threshold(session, client, row)


def handle_thread_reply(
    session: Session,
    client,
    channel: str,
    thread_ts: str,
    reply_ts: str,
    replier: str,
    text: str,
) -> bool:
    """Process a thread reply: if positive, count the replier as a supporter.

    Returns True if this reply pushed the parent message over the threshold.
    """
    if not is_positive_reply(text):
        return False

    row = _upsert_tracked_message(session, client, channel, thread_ts)
    if replier == row.author_slack_id:
        session.commit()
        return False

    already_recorded = session.execute(
        select(PositiveReply.id).where(
            PositiveReply.tracked_message_id == row.id,
            PositiveReply.slack_reply_ts == reply_ts,
        )
    ).scalar_one_or_none()
    if already_recorded is None:
        session.add(
            PositiveReply(
                tracked_message_id=row.id,
                replier_slack_id=replier,
                slack_reply_ts=reply_ts,
            )
        )
        session.flush()

    return _evaluate_threshold(session, client, row)


def _fire_pipeline(session: Session, client, row: TrackedMessage) -> None:
    """Intent gate, then Phase 2: DM the original poster to gather context."""
    from app.slack.dm_agent import start_conversation
    from app.slack.intent import INTENT_NOT_ACTIONABLE, classify_intent

    try:
        row.intent = classify_intent(row.text)
        if row.intent == INTENT_NOT_ACTIONABLE:
            logger.info(
                "Message %s crossed threshold but is not actionable (%r) — skipping DM",
                row.id,
                row.text[:80],
            )
            return
        start_conversation(session, client, row)
    except Exception:
        # Never let a DM failure roll back the triggered flag — we must not
        # re-fire for this message on the next reaction.
        logger.exception("Failed to start DM conversation for message %s", row.id)
