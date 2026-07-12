import json
import os
import subprocess

import pytest
from sqlalchemy import select

from app.coding_agent import runner
from app.coding_agent.runner import process_work_item
from app.coding_agent.sandbox import create_sandbox
from app.db.models import Conversation, TrackedMessage, WorkItem


@pytest.fixture()
def origin_repo(tmp_path):
    """A real local git repo acting as the GitHub remote."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, capture_output=True)
    (origin / "README.md").write_text("# Target repo\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=origin,
        check=True,
        capture_output=True,
    )
    # Allow pushing new branches into this non-bare repo.
    subprocess.run(
        ["git", "config", "receive.denyCurrentBranch", "ignore"],
        cwd=origin,
        check=True,
        capture_output=True,
    )
    return origin


@pytest.fixture()
def work_item(session, tmp_path):
    tracked = TrackedMessage(slack_channel_id="C1", slack_message_ts="1.0", text="idea", author_slack_id="U_A")
    session.add(tracked)
    session.flush()
    convo = Conversation(
        tracked_message_id=tracked.id, slack_dm_channel_id="D1", status="issue_filed",
        gathered_context=json.dumps({"summary": "s", "history": []}),
    )
    session.add(convo)
    session.flush()
    plan = tmp_path / "issue-7-plan.md"
    plan.write_text("# Plan\nAdd a /health endpoint.")
    item = WorkItem(
        conversation_id=convo.id, github_issue_number=7,
        plan_md_path=str(plan), pr_status="pending",
    )
    session.add(item)
    session.commit()
    return item


class FakePR:
    number = 42
    html_url = "https://github.com/acme/robot/pull/42"


class FakePRRepo:
    default_branch = "main"

    def __init__(self):
        self.pulls = []

    def create_pull(self, base, head, title, body):
        self.pulls.append({"base": base, "head": head, "title": title, "body": body})
        return FakePR()


def _branch_exists(origin, branch):
    result = subprocess.run(
        ["git", "rev-parse", "--verify", branch], cwd=origin, capture_output=True
    )
    return result.returncode == 0


def test_sandbox_clone_branch_commit_push(origin_repo):
    sandbox = create_sandbox(f"file://{origin_repo}", "vector/issue-7")
    try:
        assert os.path.exists(os.path.join(sandbox.path, "README.md"))

        # No changes yet -> no commit.
        assert sandbox.commit_all("noop") is False

        with open(os.path.join(sandbox.path, "health.py"), "w") as f:
            f.write("STATUS = 200\n")
        assert sandbox.commit_all("add health") is True
        sandbox.push()
    finally:
        sandbox.cleanup()

    assert _branch_exists(origin_repo, "vector/issue-7")
    assert not os.path.exists(sandbox.path)


def test_process_work_item_opens_pr(session, work_item, origin_repo, monkeypatch):
    fake_repo = FakePRRepo()
    monkeypatch.setattr(runner, "get_repo", lambda: fake_repo)

    def fake_gemini(sandbox_path, prompt):
        assert os.path.exists(os.path.join(sandbox_path, "PLAN.md"))
        with open(os.path.join(sandbox_path, "health.py"), "w") as f:
            f.write("STATUS = 200\n")

    monkeypatch.setattr(runner, "_run_gemini", fake_gemini)

    pr_url = process_work_item(session, work_item, clone_url=f"file://{origin_repo}")

    assert pr_url == FakePR.html_url
    assert work_item.pr_number == 42
    assert work_item.pr_status == "in_review"
    assert _branch_exists(origin_repo, "vector/issue-7")

    pull = fake_repo.pulls[0]
    assert pull["head"] == "vector/issue-7"
    assert "Closes #7" in pull["body"]

    # PLAN.md must not have been committed.
    files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "vector/issue-7"],
        cwd=origin_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "health.py" in files
    assert "PLAN.md" not in files


def test_gemini_failure_marks_failed(session, work_item, origin_repo, monkeypatch):
    monkeypatch.setattr(runner, "get_repo", lambda: FakePRRepo())

    def boom(sandbox_path, prompt):
        raise RuntimeError("quota exhausted (exit 1)")

    monkeypatch.setattr(runner, "_run_gemini", boom)

    with pytest.raises(RuntimeError):
        process_work_item(session, work_item, clone_url=f"file://{origin_repo}")
    assert work_item.pr_status == "failed"
    assert not _branch_exists(origin_repo, "vector/issue-7")


def test_no_changes_marks_failed(session, work_item, origin_repo, monkeypatch):
    monkeypatch.setattr(runner, "get_repo", lambda: FakePRRepo())
    monkeypatch.setattr(runner, "_run_gemini", lambda sandbox_path, prompt: None)

    with pytest.raises(RuntimeError, match="no changes"):
        process_work_item(session, work_item, clone_url=f"file://{origin_repo}")
    assert work_item.pr_status == "failed"


def test_failing_repo_tests_block_pr_after_repair_rounds(session, work_item, origin_repo, monkeypatch):
    import dataclasses

    monkeypatch.setattr(runner, "get_repo", lambda: FakePRRepo())
    monkeypatch.setattr(
        runner, "settings", dataclasses.replace(runner.settings, target_repo_test_cmd="exit 1")
    )

    calls = []

    def fake_gemini(sandbox_path, prompt):
        calls.append(prompt)
        with open(os.path.join(sandbox_path, "broken.py"), "w") as f:
            f.write("x = 1\n")

    monkeypatch.setattr(runner, "_run_gemini", fake_gemini)

    with pytest.raises(RuntimeError, match="still failing"):
        process_work_item(session, work_item, clone_url=f"file://{origin_repo}")
    assert work_item.pr_status == "failed"
    assert not _branch_exists(origin_repo, "vector/issue-7")
    # 1 initial run + MAX_REPAIR_ROUNDS repair attempts, each fed the failure output.
    assert len(calls) == 1 + runner.MAX_REPAIR_ROUNDS
    assert all("failed with the output below" in p for p in calls[1:])


def test_repair_round_fixes_failing_tests(session, work_item, origin_repo, monkeypatch):
    """First test run fails; the repair round writes the file the test command
    needs, so the second run passes and the PR opens."""
    import dataclasses

    fake_repo = FakePRRepo()
    monkeypatch.setattr(runner, "get_repo", lambda: fake_repo)
    monkeypatch.setattr(
        runner,
        "settings",
        dataclasses.replace(runner.settings, target_repo_test_cmd="test -f fixed.txt"),
    )

    calls = []

    def fake_gemini(sandbox_path, prompt):
        calls.append(prompt)
        if len(calls) == 1:  # initial implementation, tests will fail
            with open(os.path.join(sandbox_path, "health.py"), "w") as f:
                f.write("STATUS = 200\n")
        else:  # repair round fixes it
            with open(os.path.join(sandbox_path, "fixed.txt"), "w") as f:
                f.write("ok\n")

    monkeypatch.setattr(runner, "_run_gemini", fake_gemini)

    pr_url = process_work_item(session, work_item, clone_url=f"file://{origin_repo}")

    assert pr_url == FakePR.html_url
    assert work_item.pr_status == "in_review"
    assert len(calls) == 2  # only one repair round was needed
    files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "vector/issue-7"],
        cwd=origin_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "fixed.txt" in files  # repair output was committed too
