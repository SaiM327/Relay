"""Phase 4: invokes Gemini CLI headlessly against a repo sandbox, opens a PR.

Requires the Gemini CLI to be installed (`npm install -g @google/gemini-cli`
or `brew install gemini-cli`); it authenticates via GEMINI_API_KEY. Headless
mode exits code 1 on daily-quota exhaustion without model fallback, so that
surfaces as a normal failure (pr_status="failed") to retry later.
"""

import logging
import os
import shutil
import subprocess

from sqlalchemy.orm import Session

from app.config import settings
from app.coding_agent.sandbox import create_sandbox
from app.db.models import WorkItem
from app.github.client import get_repo

logger = logging.getLogger(__name__)

GEMINI_CLI = "gemini"
GEMINI_CODING_MODEL = "gemini-flash-latest"
AGENT_TIMEOUT_S = 900
BRANCH_PREFIX = "vector/issue-"
MAX_REPAIR_ROUNDS = 2

AGENT_PROMPT = (
    "Implement the plan in PLAN.md. Make the necessary code changes, run existing "
    "tests if the repo has them, and stop once they pass. Do not modify PLAN.md "
    "or anything under .git. Keep the changes minimal and focused on the plan."
)

REPAIR_PROMPT = (
    "You previously made changes in this repo to implement PLAN.md, but the test "
    "command failed with the output below. Fix the code so the tests pass. Fix "
    "the code, not the tests. Do not modify PLAN.md or anything under .git.\n\n"
    "Test output:\n{output}"
)


def default_clone_url() -> str:
    return f"https://x-access-token:{settings.github_token}@github.com/{settings.github_repo}.git"


def process_work_item(session: Session, work_item: WorkItem, clone_url: str | None = None) -> str:
    """Run the whole coding step for a WorkItem. Returns the PR URL.

    On any failure the WorkItem is marked failed and the exception re-raised.
    """
    issue_number = work_item.github_issue_number
    branch = f"{BRANCH_PREFIX}{issue_number}"
    sandbox = create_sandbox(clone_url or default_clone_url(), branch)
    try:
        # Drop the plan into the checkout for the agent, but keep it out of git.
        shutil.copy(work_item.plan_md_path, os.path.join(sandbox.path, "PLAN.md"))
        with open(os.path.join(sandbox.path, ".git", "info", "exclude"), "a") as f:
            f.write("PLAN.md\n")

        _run_gemini(sandbox.path, AGENT_PROMPT)
        _run_tests_with_repair_loop(sandbox.path)

        if not sandbox.commit_all(f"Implement plan for issue #{issue_number}"):
            raise RuntimeError("Coding agent finished but made no changes")
        sandbox.push()

        repo = get_repo()
        with open(work_item.plan_md_path) as f:
            plan_md = f.read()
        pr = repo.create_pull(
            base=repo.default_branch,
            head=branch,
            title=f"Implement issue #{issue_number}",
            body=(
                f"Automated implementation of #{issue_number}.\n\n"
                f"Closes #{issue_number}\n\n"
                f"<details>\n<summary>plan.md</summary>\n\n{plan_md}\n\n</details>"
            ),
        )
        work_item.pr_number = pr.number
        work_item.pr_status = "in_review"
        session.commit()
        logger.info("Opened PR #%s for issue #%s", pr.number, issue_number)

        try:
            from app.review.ai_reviewer import review_pr

            review_pr(pr, plan_md)
        except Exception:
            # Advisory only — a reviewer failure must never fail the work item.
            logger.exception("AI pre-review failed for PR #%s (non-fatal)", pr.number)
        return pr.html_url
    except Exception:
        work_item.pr_status = "failed"
        session.commit()
        raise
    finally:
        sandbox.cleanup()


def _run_gemini(sandbox_path: str, prompt: str) -> None:
    env = {
        **os.environ,
        "GEMINI_API_KEY": settings.gemini_api_key,
        # The sandbox is a fresh temp dir, never in the CLI's trusted-folders
        # list; without this the CLI refuses to run headlessly with --yolo.
        "GEMINI_CLI_TRUST_WORKSPACE": "true",
    }
    result = subprocess.run(
        [GEMINI_CLI, "--yolo", "-m", GEMINI_CODING_MODEL, "-p", prompt],
        cwd=sandbox_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=AGENT_TIMEOUT_S,
    )
    logger.info("Gemini CLI finished (exit %s). Output tail: %s", result.returncode, result.stdout[-2000:])
    if result.returncode != 0:
        raise RuntimeError(
            f"Gemini CLI exited with {result.returncode}: {result.stderr.strip()[-2000:]}"
        )


def _run_tests_with_repair_loop(sandbox_path: str) -> None:
    """Run repo tests; on failure feed the output back to Gemini for a repair
    round (max MAX_REPAIR_ROUNDS), then re-verify. Tests are always run by us —
    never trusted from the agent's self-report."""
    failure = _repo_test_failure(sandbox_path)
    rounds = 0
    while failure and rounds < MAX_REPAIR_ROUNDS:
        rounds += 1
        logger.info("Repo tests failing; repair round %s/%s", rounds, MAX_REPAIR_ROUNDS)
        _run_gemini(sandbox_path, REPAIR_PROMPT.format(output=failure))
        failure = _repo_test_failure(sandbox_path)
    if failure:
        raise RuntimeError(
            f"Repo tests still failing after {MAX_REPAIR_ROUNDS} repair rounds:\n{failure}"
        )


def _repo_test_failure(sandbox_path: str) -> str | None:
    """Independent sanity check. Returns the failure output, or None on pass/skip."""
    cmd = settings.target_repo_test_cmd
    if not cmd:
        logger.info("TARGET_REPO_TEST_CMD not set; skipping independent test run")
        return None
    result = subprocess.run(
        cmd, shell=True, cwd=sandbox_path, capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        return f"({cmd!r} exited {result.returncode})\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    logger.info("Repo tests passed (%r)", cmd)
    return None
