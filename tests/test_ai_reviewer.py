import dataclasses

import pytest

from app.review import ai_reviewer
from app.review.ai_reviewer import review_pr


class FakeFile:
    def __init__(self, filename, patch):
        self.filename = filename
        self.patch = patch


class FakePull:
    number = 42

    def __init__(self, files=None):
        self.files = files if files is not None else [
            FakeFile("health.py", "@@ -0,0 +1 @@\n+STATUS = 200")
        ]
        self.comments = []

    def get_files(self):
        return self.files

    def create_issue_comment(self, body):
        self.comments.append(body)


@pytest.fixture()
def with_key(monkeypatch):
    patched = dataclasses.replace(ai_reviewer.settings, gemini_api_key="test-key")
    monkeypatch.setattr(ai_reviewer, "settings", patched)


def test_review_posts_advisory_comment(with_key, monkeypatch):
    prompts = []

    def fake_gemini(prompt):
        prompts.append(prompt)
        return "Looks fine, but check the status code constant."

    monkeypatch.setattr(ai_reviewer, "_call_gemini", fake_gemini)

    pr = FakePull()
    body = review_pr(pr, plan_md="# Plan\nAdd health endpoint.")

    assert len(pr.comments) == 1
    assert pr.comments[0] == body
    assert body.startswith("## Automated pre-review")
    assert "check the status code constant" in body
    assert "Advisory only" in body
    # Prompt is grounded in both the plan and the diff.
    assert "Add health endpoint" in prompts[0]
    assert "STATUS = 200" in prompts[0]


def test_review_skipped_without_api_key(monkeypatch):
    patched = dataclasses.replace(ai_reviewer.settings, gemini_api_key="")
    monkeypatch.setattr(ai_reviewer, "settings", patched)

    pr = FakePull()
    assert review_pr(pr, "plan") is None
    assert pr.comments == []


def test_review_skipped_with_empty_diff(with_key, monkeypatch):
    monkeypatch.setattr(ai_reviewer, "_call_gemini", lambda prompt: "should not be called")

    pr = FakePull(files=[])
    assert review_pr(pr, "plan") is None
    assert pr.comments == []


def test_reviewer_failure_does_not_fail_work_item(session, monkeypatch, tmp_path):
    """End-to-end through the runner: gemini review blows up, PR still opens."""
    import json
    import os
    import subprocess

    from app.coding_agent import runner
    from app.db.models import Conversation, TrackedMessage, WorkItem

    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, capture_output=True)
    (origin / "README.md").write_text("# Target\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=origin, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "receive.denyCurrentBranch", "ignore"],
        cwd=origin, check=True, capture_output=True,
    )

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
    item = WorkItem(conversation_id=convo.id, github_issue_number=7, plan_md_path=str(plan), pr_status="pending")
    session.add(item)
    session.commit()

    class FakePR:
        number = 42
        html_url = "https://github.com/acme/robot/pull/42"

        def get_files(self):
            raise RuntimeError("github api down")

        def create_issue_comment(self, body):
            raise AssertionError("should not be reached")

    class FakePRRepo:
        default_branch = "main"

        def create_pull(self, base, head, title, body):
            return FakePR()

    monkeypatch.setattr(runner, "get_repo", lambda: FakePRRepo())

    def fake_gemini(sandbox_path, prompt):
        with open(os.path.join(sandbox_path, "health.py"), "w") as f:
            f.write("STATUS = 200\n")

    monkeypatch.setattr(runner, "_run_gemini", fake_gemini)

    patched = dataclasses.replace(ai_reviewer.settings, gemini_api_key="test-key")
    monkeypatch.setattr(ai_reviewer, "settings", patched)

    pr_url = runner.process_work_item(session, item, clone_url=f"file://{origin}")
    assert pr_url == FakePR.html_url
    assert item.pr_status == "in_review"  # reviewer failure did not mark it failed
