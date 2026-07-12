import json
import os

import pytest
from sqlalchemy import select

from app.db.models import Conversation, TrackedMessage, WorkItem
from app.github import issues
from app.github.issues import make_title, process_ready_conversation
from app.planning import plan_generator


class FakeIssue:
    def __init__(self, number, title, body):
        self.number = number
        self.title = title
        self.body = body
        self.html_url = f"https://github.com/acme/robot/issues/{number}"


class FakeTreeEntry:
    def __init__(self, path, type="blob"):
        self.path = path
        self.type = type


class FakeTree:
    def __init__(self, entries):
        self.tree = entries


class FakeReadme:
    decoded_content = b"# Robot\nA robot control repo."


class FakeBranch:
    class commit:
        sha = "abc123def456"


class FakeRepo:
    full_name = "acme/robot"
    default_branch = "main"

    def __init__(self):
        self.issues = []

    def get_branch(self, name):
        return FakeBranch()

    def create_issue(self, title, body):
        issue = FakeIssue(len(self.issues) + 1, title, body)
        self.issues.append(issue)
        return issue

    def get_git_tree(self, ref, recursive):
        return FakeTree(
            [
                FakeTreeEntry("src/calibrate.py"),
                FakeTreeEntry("src/vision/aruco.py"),
                FakeTreeEntry("assets/logo.png"),  # excluded by filter
                FakeTreeEntry("src", type="tree"),  # not a blob
            ]
        )

    def get_readme(self):
        return FakeReadme()


@pytest.fixture()
def ready_convo(session):
    tracked = TrackedMessage(slack_channel_id="C1", slack_message_ts="1.0", text="idea", author_slack_id="U_A")
    session.add(tracked)
    session.flush()
    convo = Conversation(
        tracked_message_id=tracked.id,
        slack_dm_channel_id="D1",
        status="ready",
        gathered_context=json.dumps(
            {
                "idea": "calibrate the robot",
                "permalink": "https://slack.example/p1",
                "history": [
                    {"role": "agent", "text": "tell me more"},
                    {"role": "user", "text": "use aruco markers with opencv"},
                ],
                "summary": "Calibrate robot camera using ArUco markers. Use OpenCV to find relative pose.",
            }
        ),
    )
    session.add(convo)
    session.commit()
    return convo


@pytest.fixture()
def fake_repo(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(issues, "get_repo", lambda: repo)
    monkeypatch.setattr(plan_generator, "_call_gemini", lambda model, prompt: "# Plan\nEdit src/calibrate.py")
    return repo


def test_issue_filed_with_full_context(session, ready_convo, fake_repo):
    work_item, issue = process_ready_conversation(session, ready_convo)

    assert issue.title == "Calibrate robot camera using ArUco markers"
    assert "## Summary" in issue.body
    assert "https://slack.example/p1" in issue.body
    assert "use aruco markers with opencv" in issue.body  # transcript included

    assert work_item.github_issue_number == issue.number
    assert work_item.pr_status == "pending"
    assert ready_convo.status == "issue_filed"

    assert os.path.exists(work_item.plan_md_path)
    with open(work_item.plan_md_path) as f:
        assert "src/calibrate.py" in f.read()


def test_plan_failure_still_files_issue(session, ready_convo, fake_repo, monkeypatch):
    def boom(model, prompt):
        raise RuntimeError("quota exhausted")

    monkeypatch.setattr(plan_generator, "_call_gemini", boom)
    work_item, issue = process_ready_conversation(session, ready_convo)

    assert issue.number == 1
    assert work_item.plan_md_path == ""
    assert ready_convo.status == "issue_filed"


def test_repo_index_filters_non_source(fake_repo):
    from app.planning.repo_index import format_index_for_prompt, get_repo_index

    index_text = format_index_for_prompt(get_repo_index(fake_repo))
    assert "src/calibrate.py" in index_text
    assert "src/vision/aruco.py" in index_text
    assert "logo.png" not in index_text


def test_make_title():
    assert make_title("Add dark mode. Users want it.") == "Add dark mode"
    assert make_title("Fix the login bug\nIt crashes on submit") == "Fix the login bug"
    long = "word " * 40
    assert len(make_title(long)) <= 81
    assert make_title("") == "Feature request from Slack"


def test_dm_flow_posts_issue_link(session, fake_client, fake_repo, monkeypatch):
    """End-to-end: done reply -> issue filed -> DM contains the issue URL."""
    import dataclasses

    from app.slack import dm_agent
    from app.slack.dm_agent import AgentReply, handle_dm_message
    from app.slack.reaction_tracker import handle_reaction_change

    patched = dataclasses.replace(dm_agent.settings, gemini_api_key="test-key")
    monkeypatch.setattr(dm_agent, "settings", patched)
    monkeypatch.setattr(
        dm_agent, "_agent_step", lambda *a, **kw: AgentReply(done=True, text="Calibrate the robot camera.")
    )
    spawned = []
    monkeypatch.setattr(dm_agent, "_run_coding_agent_async", lambda *a, **kw: spawned.append(a))

    for user in ("U1", "U2", "U3"):
        fake_client.add_reaction("C1", "1.0", "thumbsup", user)
        handle_reaction_change(session, fake_client, "C1", "1.0")

    handle_dm_message(
        session,
        fake_client,
        {"channel": "D_U_AUTHOR", "channel_type": "im", "user": "U_AUTHOR", "text": "details", "ts": "2"},
    )

    convo = session.execute(select(Conversation)).scalar_one()
    assert convo.status == "issue_filed"
    assert session.execute(select(WorkItem)).scalar_one().github_issue_number == 1
    assert "issues/1" in fake_client.posted[-1]["text"]
    assert len(spawned) == 1  # coding agent kicked off in background
