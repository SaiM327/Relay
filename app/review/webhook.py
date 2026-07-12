"""Phase 5: GitHub webhook handler (PR reviews + check suites)."""

import hashlib
import hmac
import logging

from flask import Flask, request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import ApprovalEvent, SessionLocal, WorkItem
from app.review.merge import try_merge

logger = logging.getLogger(__name__)


def _signature_valid(body: bytes, header: str | None) -> bool:
    if not settings.github_webhook_secret:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — accepting webhook unverified (dev only)")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", header)


def register_github_webhook(flask_app: Flask, slack_client=None) -> None:
    @flask_app.route("/github/webhook", methods=["POST"])
    def github_webhook():
        if not _signature_valid(request.get_data(), request.headers.get("X-Hub-Signature-256")):
            return {"error": "invalid signature"}, 401

        event = request.headers.get("X-GitHub-Event", "")
        payload = request.get_json(silent=True) or {}
        with SessionLocal() as session:
            if event == "pull_request_review":
                handle_review_event(session, payload, slack_client)
            elif event == "check_suite":
                handle_check_suite_event(session, payload, slack_client)
        return {"ok": True}


def _find_work_item(session: Session, pr_number: int) -> WorkItem | None:
    return session.execute(
        select(WorkItem).where(
            WorkItem.pr_number == pr_number,
            WorkItem.pr_status.in_(("in_review", "approved")),
        )
    ).scalar_one_or_none()


def handle_review_event(session: Session, payload: dict, slack_client=None) -> None:
    if payload.get("action") != "submitted":
        return
    review = payload.get("review") or {}
    if (review.get("state") or "").lower() != "approved":
        return

    pr_number = (payload.get("pull_request") or {}).get("number")
    work_item = _find_work_item(session, pr_number)
    if work_item is None:
        logger.info("Approved review on PR #%s — not one of ours, ignoring", pr_number)
        return

    approver = (review.get("user") or {}).get("login", "unknown")
    first_approval = work_item.pr_status == "in_review"
    session.add(ApprovalEvent(work_item_id=work_item.id, approved_by=approver))
    work_item.pr_status = "approved"
    session.commit()
    logger.info("Recorded approval of PR #%s by %s", pr_number, approver)

    if first_approval and slack_client is not None:
        from app.notify.slack_notify import announce_approval

        announce_approval(slack_client, session, work_item, approver)

    outcome = try_merge(session, work_item, slack_client)
    logger.info("PR #%s merge attempt: %s", pr_number, outcome)


def handle_check_suite_event(session: Session, payload: dict, slack_client=None) -> None:
    """CI finished somewhere — re-evaluate PRs that are approved but unmerged."""
    if payload.get("action") != "completed":
        return
    approved_items = session.execute(
        select(WorkItem).where(
            WorkItem.pr_status == "approved",
            WorkItem.pr_number.is_not(None),
        )
    ).scalars().all()
    for work_item in approved_items:
        outcome = try_merge(session, work_item, slack_client)
        logger.info("PR #%s re-check after check_suite: %s", work_item.pr_number, outcome)
