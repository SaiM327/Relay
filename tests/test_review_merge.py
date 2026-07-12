import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.db.models import ApprovalEvent, Conversation, TrackedMessage, WorkItem
from app.review import merge, webhook
from app.review.webhook import handle_check_suite_event, handle_review_event


# --- Fakes -----------------------------------------------------------------

class FakeCheckRun:
    def __init__(self, name, status="completed", conclusion="success"):
        self.name = name
        self.status = status
        self.conclusion = conclusion


class FakeCombinedStatus:
    def __init__(self, total_count=0, state="success"):
        self.total_count = total_count
        self.state = state


class FakeCommit:
    def __init__(self, check_runs, combined=None):
        self._check_runs = check_runs
        self._combined = combined or FakeCombinedStatus()

    def get_check_runs(self):
        return self._check_runs

    def get_combined_status(self):
        return self._combined


class FakeHead:
    sha = "abc123def456"


class FakePull:
    def __init__(self):
        self.merged = False
        self.head = FakeHead()
        self.html_url = "https://github.com/acme/robot/pull/42"
        self.merge_calls = []

    def merge(self, merge_method=None):
        self.merge_calls.append(merge_method)
        self.merged = True


class FakeMergeRepo:
    def __init__(self, check_runs=None, combined=None):
        self.pull = FakePull()
        self.commit = FakeCommit(check_runs or [], combined)

    def get_pull(self, number):
        return self.pull

    def get_commit(self, sha):
        return self.commit


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture()
def work_item(session):
    tracked = TrackedMessage(
        slack_channel_id="C1", slack_message_ts="1.0", text="robot idea", author_slack_id="U_A"
    )
    session.add(tracked)
    session.flush()
    convo = Conversation(
        tracked_message_id=tracked.id, slack_dm_channel_id="D1",
        status="issue_filed", gathered_context="{}",
    )
    session.add(convo)
    session.flush()
    item = WorkItem(
        conversation_id=convo.id, github_issue_number=7, plan_md_path="",
        pr_number=42, pr_status="in_review",
    )
    session.add(item)
    session.commit()
    return item


def _approved_review_payload(pr_number=42, reviewer="alice"):
    return {
        "action": "submitted",
        "review": {"state": "approved", "user": {"login": reviewer}},
        "pull_request": {"number": pr_number},
    }


def _use_repo(monkeypatch, repo):
    monkeypatch.setattr(merge, "get_repo", lambda: repo)
    return repo


# --- Merge gate -------------------------------------------------------------

def test_approval_with_green_ci_merges(session, work_item, fake_client, monkeypatch):
    repo = _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci")]))

    handle_review_event(session, _approved_review_payload(), fake_client)

    approval = session.execute(select(ApprovalEvent)).scalar_one()
    assert approval.approved_by == "alice"
    assert repo.pull.merged is True
    assert repo.pull.merge_calls == ["squash"]
    assert work_item.pr_status == "merged"
    # Poster got the shipped DM.
    assert any("shipped" in p["text"] for p in fake_client.posted)


def test_approval_announced_in_idea_channel(session, work_item, fake_client, monkeypatch):
    _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci")]))

    handle_review_event(session, _approved_review_payload(reviewer="alice"), fake_client)

    announcements = [p for p in fake_client.posted if p["text"].startswith(":white_check_mark:")]
    assert len(announcements) == 1
    assert announcements[0]["channel"] == "C1"  # the channel the idea came from
    assert "alice" in announcements[0]["text"]
    assert "robot idea" in announcements[0]["text"]


def test_second_approval_not_reannounced(session, work_item, fake_client, monkeypatch):
    # Red CI keeps the PR unmerged so a second review can arrive.
    _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci", conclusion="failure")]))

    handle_review_event(session, _approved_review_payload(reviewer="alice"), fake_client)
    handle_review_event(session, _approved_review_payload(reviewer="bob"), fake_client)

    announcements = [p for p in fake_client.posted if p["text"].startswith(":white_check_mark:")]
    assert len(announcements) == 1  # only the first approval is announced


def test_approval_with_red_ci_does_not_merge(session, work_item, fake_client, monkeypatch):
    repo = _use_repo(
        monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci", conclusion="failure")])
    )

    handle_review_event(session, _approved_review_payload(), fake_client)

    # Approval recorded, but no merge.
    assert session.execute(select(ApprovalEvent)).scalar_one().approved_by == "alice"
    assert repo.pull.merged is False
    assert work_item.pr_status == "approved"


def test_pending_ci_does_not_merge(session, work_item, fake_client, monkeypatch):
    repo = _use_repo(
        monkeypatch,
        FakeMergeRepo(check_runs=[FakeCheckRun("ci", status="in_progress", conclusion=None)]),
    )
    handle_review_event(session, _approved_review_payload(), fake_client)
    assert repo.pull.merged is False
    assert work_item.pr_status == "approved"


def test_check_suite_completion_merges_after_ci_fixed(session, work_item, fake_client, monkeypatch):
    # First: approved while CI red.
    red = _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci", conclusion="failure")]))
    handle_review_event(session, _approved_review_payload(), fake_client)
    assert red.pull.merged is False

    # Later: CI fixed, check_suite completed arrives.
    green = _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci")]))
    handle_check_suite_event(session, {"action": "completed"}, fake_client)

    assert green.pull.merged is True
    assert work_item.pr_status == "merged"


def test_no_approval_never_merges_via_check_suite(session, work_item, fake_client, monkeypatch):
    repo = _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci")]))
    # CI green but nobody approved: check_suite events must not merge.
    handle_check_suite_event(session, {"action": "completed"}, fake_client)
    assert repo.pull.merged is False
    assert work_item.pr_status == "in_review"


def test_review_on_foreign_pr_ignored(session, work_item, fake_client, monkeypatch):
    repo = _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci")]))
    handle_review_event(session, _approved_review_payload(pr_number=999), fake_client)
    assert session.execute(select(ApprovalEvent)).scalar_one_or_none() is None
    assert repo.pull.merged is False


def test_non_approval_review_ignored(session, work_item, fake_client, monkeypatch):
    repo = _use_repo(monkeypatch, FakeMergeRepo(check_runs=[FakeCheckRun("ci")]))
    payload = _approved_review_payload()
    payload["review"]["state"] = "changes_requested"
    handle_review_event(session, payload, fake_client)
    assert session.execute(select(ApprovalEvent)).scalar_one_or_none() is None
    assert repo.pull.merged is False


def test_legacy_status_red_blocks(session, work_item, fake_client, monkeypatch):
    repo = _use_repo(
        monkeypatch,
        FakeMergeRepo(check_runs=[], combined=FakeCombinedStatus(total_count=2, state="failure")),
    )
    handle_review_event(session, _approved_review_payload(), fake_client)
    assert repo.pull.merged is False


# --- Webhook signature ------------------------------------------------------

def _signed(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature_enforced(monkeypatch):
    import dataclasses

    from flask import Flask

    monkeypatch.setattr(
        webhook, "settings", dataclasses.replace(webhook.settings, github_webhook_secret="whsec")
    )
    app = Flask(__name__)
    webhook.register_github_webhook(app)
    client = app.test_client()
    body = json.dumps({"action": "ping"}).encode()

    bad = client.post(
        "/github/webhook",
        data=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=deadbeef"},
        content_type="application/json",
    )
    assert bad.status_code == 401

    good = client.post(
        "/github/webhook",
        data=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _signed(body, "whsec")},
        content_type="application/json",
    )
    assert good.status_code == 200
