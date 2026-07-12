"""Phase 3: GitHub issue creation from a ready Conversation, plus plan.md."""

import json
import logging
import re

from sqlalchemy.orm import Session

from app.db.models import Conversation, WorkItem
from app.github.client import get_repo
from app.planning.plan_generator import generate_plan, save_plan

logger = logging.getLogger(__name__)

TITLE_MAX_LEN = 80


def make_title(summary: str) -> str:
    """Short issue title: first sentence/line of the summary, capped."""
    first = re.split(r"(?<=[.!?])\s|\n", summary.strip(), maxsplit=1)[0].strip().rstrip(".")
    if len(first) > TITLE_MAX_LEN:
        first = first[: TITLE_MAX_LEN - 1].rsplit(" ", 1)[0] + "…"
    return first or "Feature request from Slack"


def _build_body(context: dict) -> str:
    parts = ["## Summary", "", context.get("summary") or "", ""]
    if context.get("permalink"):
        parts += [f"_Originally proposed in [this Slack message]({context['permalink']})._", ""]
    parts += ["<details>", "<summary>Gathering conversation transcript</summary>", ""]
    for turn in context.get("history", []):
        parts.append(f"- **{turn['role']}**: {turn['text']}")
    parts += ["", "</details>"]
    return "\n".join(parts)


def process_ready_conversation(session: Session, convo: Conversation):
    """Create the GitHub issue, generate plan.md, and record the WorkItem.

    Returns (work_item, issue). Raises if the issue itself can't be created;
    a plan-generation failure is logged but not fatal (plan_md_path stays "").
    """
    context = json.loads(convo.gathered_context)
    title = make_title(context.get("summary") or context.get("idea") or "")
    body = _build_body(context)

    repo = get_repo()
    issue = repo.create_issue(title=title, body=body)
    logger.info("Filed issue #%s for conversation %s", issue.number, convo.id)

    plan_path = ""
    try:
        plan_md = generate_plan(title, body, repo)
        plan_path = save_plan(issue.number, plan_md)
        logger.info("Plan for issue #%s written to %s", issue.number, plan_path)
    except Exception:
        logger.exception("Plan generation failed for issue #%s", issue.number)

    work_item = WorkItem(
        conversation_id=convo.id,
        github_issue_number=issue.number,
        plan_md_path=plan_path,
        pr_status="pending",
    )
    session.add(work_item)
    convo.status = "issue_filed"
    session.commit()
    return work_item, issue
