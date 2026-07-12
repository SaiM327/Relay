"""Phase 5/6: DM the original poster + team channel announcement on merge."""

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Conversation, TrackedMessage, WorkItem

logger = logging.getLogger(__name__)


def announce_approval(client, session: Session, work_item: WorkItem, approver: str) -> None:
    """Announce a human approval in the channel where the idea was posted
    (plus the team channel, if configured)."""
    convo = session.get(Conversation, work_item.conversation_id)
    tracked = session.get(TrackedMessage, convo.tracked_message_id)

    pr_url = f"https://github.com/{settings.github_repo}/pull/{work_item.pr_number}"
    text = (
        f":white_check_mark: <{pr_url}|PR #{work_item.pr_number}> for "
        f"<@{tracked.author_slack_id}>'s idea (“{tracked.text[:140]}”) was approved "
        f"by *{approver}* — it'll merge automatically once CI is green."
    )

    channels = [tracked.slack_channel_id]
    if settings.team_channel_id and settings.team_channel_id not in channels:
        channels.append(settings.team_channel_id)
    for channel in channels:
        try:
            client.chat_postMessage(channel=channel, text=text)
        except Exception:
            logger.exception("Failed to announce approval in %s", channel)


def announce_merge(client, session: Session, work_item: WorkItem, pr_url: str) -> None:
    convo = session.get(Conversation, work_item.conversation_id)
    tracked = session.get(TrackedMessage, convo.tracked_message_id)

    if convo.slack_dm_channel_id:
        client.chat_postMessage(
            channel=convo.slack_dm_channel_id,
            text=f"Your idea just shipped! :ship: The PR was approved and merged: {pr_url}",
        )

    if settings.team_channel_id:
        client.chat_postMessage(
            channel=settings.team_channel_id,
            text=(
                f":rocket: Shipped an idea from <@{tracked.author_slack_id}>: "
                f"“{tracked.text[:140]}” — merged in {pr_url}"
            ),
        )

    convo.status = "done"
    session.commit()
