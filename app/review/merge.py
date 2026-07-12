"""Phase 5: merge logic gated on human approval + green CI.

Policy: a PR merges only when BOTH hold —
  1. at least one ApprovalEvent recorded (from a pull_request_review webhook)
  2. CI on the PR head is green (Checks API + legacy combined status)
There is no timer and no "no objections" path. If either condition fails we
log why and do nothing; a later check_suite event re-evaluates.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ApprovalEvent, WorkItem
from app.github.client import get_repo

logger = logging.getLogger(__name__)

_GOOD_CONCLUSIONS = ("success", "neutral", "skipped")


def ci_is_green(repo, sha: str) -> tuple[bool, str]:
    """Check both check runs and legacy commit statuses for the head sha."""
    commit = repo.get_commit(sha)

    check_runs = list(commit.get_check_runs())
    for run in check_runs:
        if run.status != "completed":
            return False, f"check run {run.name!r} still {run.status}"
        if run.conclusion not in _GOOD_CONCLUSIONS:
            return False, f"check run {run.name!r} concluded {run.conclusion}"

    combined = commit.get_combined_status()
    if combined.total_count > 0 and combined.state != "success":
        return False, f"combined status is {combined.state!r}"

    if not check_runs and combined.total_count == 0:
        logger.info("No CI configured on %s; treating as green", sha[:8])
    return True, "green"


def try_merge(session: Session, work_item: WorkItem, slack_client=None) -> str:
    """Attempt to merge a WorkItem's PR. Returns a human-readable outcome."""
    repo = get_repo()
    pr = repo.get_pull(work_item.pr_number)

    if pr.merged:
        work_item.pr_status = "merged"
        session.commit()
        return "already merged"

    approvals = session.execute(
        select(ApprovalEvent).where(ApprovalEvent.work_item_id == work_item.id)
    ).scalars().all()
    if not approvals:
        logger.info("PR #%s: no approval recorded yet — not merging", work_item.pr_number)
        return "no approval recorded"

    green, reason = ci_is_green(repo, pr.head.sha)
    if not green:
        logger.info("PR #%s: approved but CI not green (%s) — not merging", work_item.pr_number, reason)
        return f"ci not green: {reason}"

    pr.merge(merge_method="squash")
    work_item.pr_status = "merged"
    session.commit()
    logger.info("PR #%s merged (approved by %s, CI green)", work_item.pr_number, approvals[0].approved_by)

    if slack_client is not None:
        try:
            from app.notify.slack_notify import announce_merge

            announce_merge(slack_client, session, work_item, pr.html_url)
        except Exception:
            logger.exception("Merge notification failed for PR #%s", work_item.pr_number)
    return "merged"
