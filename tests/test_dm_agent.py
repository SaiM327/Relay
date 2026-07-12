import dataclasses
import json

import pytest
from sqlalchemy import select

from app.db.models import Conversation
from app.slack import dm_agent
from app.slack.dm_agent import AgentReply, handle_dm_message
from app.slack.reaction_tracker import handle_reaction_change

CHANNEL = "C123"
TS = "1720000000.000100"
DM_CHANNEL = "D_U_AUTHOR"  # FakeSlackClient derives DM ids from the user id


def _trigger(session, client):
    """Cross the threshold (3) so the pipeline opens a DM with U_AUTHOR."""
    for user in ("U1", "U2", "U3"):
        client.add_reaction(CHANNEL, TS, "thumbsup", user)
        handle_reaction_change(session, client, CHANNEL, TS)


def _dm_event(text, files=None):
    event = {"channel": DM_CHANNEL, "channel_type": "im", "user": "U_AUTHOR", "text": text, "ts": "1"}
    if files:
        event["files"] = files
    return event


@pytest.fixture(autouse=True)
def gemini_key(monkeypatch):
    # Settings is frozen; swap the module's reference for a modified copy.
    patched = dataclasses.replace(dm_agent.settings, gemini_api_key="test-key")
    monkeypatch.setattr(dm_agent, "settings", patched)


def test_trigger_opens_dm_and_creates_conversation(session, fake_client):
    _trigger(session, fake_client)

    convo = session.execute(select(Conversation)).scalar_one()
    assert convo.status == "gathering"
    assert convo.slack_dm_channel_id == DM_CHANNEL
    context = json.loads(convo.gathered_context)
    assert context["idea"] == "an idea"

    assert len(fake_client.posted) == 1
    intro = fake_client.posted[0]
    assert intro["channel"] == DM_CHANNEL
    assert "expand" in intro["text"]


def test_gathering_loop_until_done(session, fake_client, monkeypatch):
    _trigger(session, fake_client)
    monkeypatch.setattr(dm_agent, "_fire_issue_creation", lambda *a, **kw: None)

    replies = iter(
        [
            AgentReply(done=False, text="Which browser does this happen in?"),
            AgentReply(done=True, text="Feature: dark mode toggle in settings."),
        ]
    )
    monkeypatch.setattr(dm_agent, "_agent_step", lambda *a, **kw: next(replies))

    handle_dm_message(session, fake_client, _dm_event("the settings page needs dark mode"))
    convo = session.execute(select(Conversation)).scalar_one()
    assert convo.status == "gathering"
    assert fake_client.posted[-1]["text"] == "Which browser does this happen in?"

    handle_dm_message(session, fake_client, _dm_event("all browsers, it's a UI thing"))
    session.refresh(convo)
    assert convo.status == "ready"
    context = json.loads(convo.gathered_context)
    assert context["summary"] == "Feature: dark mode toggle in settings."
    # Full transcript retained: intro + 2 user turns + 1 agent question.
    roles = [t["role"] for t in context["history"]]
    assert roles == ["agent", "user", "agent", "user"]
    assert "GitHub issue" in fake_client.posted[-1]["text"]


def test_attachment_summary_recorded(session, fake_client, monkeypatch):
    _trigger(session, fake_client)

    monkeypatch.setattr(
        dm_agent,
        "_download_attachments",
        lambda files: [("shot.png", b"fakebytes", "image/png")],
    )
    monkeypatch.setattr(
        dm_agent,
        "_agent_step",
        lambda *a, **kw: AgentReply(
            done=False, text="Got it — what did you expect instead?",
            attachment_summary="screenshot of a broken settings page",
        ),
    )
    handle_dm_message(
        session,
        fake_client,
        _dm_event("see attached", files=[{"mimetype": "image/png", "name": "shot.png"}]),
    )

    convo = session.execute(select(Conversation)).scalar_one()
    context = json.loads(convo.gathered_context)
    assert any("broken settings page" in t["text"] for t in context["history"] if t["role"] == "system")
    assert context["history"][1]["attachments"] == ["shot.png"]


def test_user_can_cancel(session, fake_client, monkeypatch):
    _trigger(session, fake_client)

    monkeypatch.setattr(
        dm_agent, "_agent_step", lambda *a, **kw: AgentReply(done=False, text="", cancelled=True)
    )
    fired = []
    monkeypatch.setattr(dm_agent, "_fire_issue_creation", lambda *a, **kw: fired.append(a))

    handle_dm_message(session, fake_client, _dm_event("nevermind, cancel this idea"))

    convo = session.execute(select(Conversation)).scalar_one()
    assert convo.status == "cancelled"
    assert fired == []
    assert "dropped" in fake_client.posted[-1]["text"]

    # Further DMs are ignored — the conversation is no longer gathering.
    handle_dm_message(session, fake_client, _dm_event("actually wait"))
    assert convo.status == "cancelled"


def test_dm_without_active_conversation_gets_help_text(session, fake_client):
    handle_dm_message(session, fake_client, _dm_event("hello?"))
    assert len(fake_client.posted) == 1
    assert "active conversation" in fake_client.posted[0]["text"]


def _file_issue(session, convo, plan_md_path="plans/issue-3-plan.md", pr_status="pending", pr_number=None):
    from app.db.models import WorkItem

    convo.status = "issue_filed"
    item = WorkItem(
        conversation_id=convo.id,
        github_issue_number=3,
        plan_md_path=plan_md_path,
        pr_status=pr_status,
        pr_number=pr_number,
    )
    session.add(item)
    session.commit()
    return item


def test_followup_dm_reports_plan_failure(session, fake_client):
    _trigger(session, fake_client)
    convo = session.execute(select(Conversation)).scalar_one()
    _file_issue(session, convo, plan_md_path="")  # plan generation failed

    handle_dm_message(session, fake_client, _dm_event("why is the coding agent holding off?"))

    text = fake_client.posted[-1]["text"]
    assert "issue #3" in text
    assert "plan generation failed" in text.lower()


def test_followup_dm_reports_pr_in_review(session, fake_client):
    _trigger(session, fake_client)
    convo = session.execute(select(Conversation)).scalar_one()
    _file_issue(session, convo, pr_status="in_review", pr_number=9)

    handle_dm_message(session, fake_client, _dm_event("any update?"))

    text = fake_client.posted[-1]["text"]
    assert "PR #9" in text
    assert "approval" in text


def test_followup_dm_reports_merged(session, fake_client):
    _trigger(session, fake_client)
    convo = session.execute(select(Conversation)).scalar_one()
    _file_issue(session, convo, pr_status="merged", pr_number=9)

    handle_dm_message(session, fake_client, _dm_event("status?"))
    assert "merged" in fake_client.posted[-1]["text"]


def test_followup_dm_after_cancel(session, fake_client, monkeypatch):
    _trigger(session, fake_client)
    monkeypatch.setattr(
        dm_agent, "_agent_step", lambda *a, **kw: AgentReply(done=False, text="", cancelled=True)
    )
    handle_dm_message(session, fake_client, _dm_event("nevermind"))

    handle_dm_message(session, fake_client, _dm_event("what happened to my idea?"))
    assert "cancelled" in fake_client.posted[-1]["text"]


def test_agent_failure_keeps_gathering(session, fake_client, monkeypatch):
    _trigger(session, fake_client)

    def boom(*a, **kw):
        raise RuntimeError("api down")

    monkeypatch.setattr(dm_agent, "_agent_step", boom)
    handle_dm_message(session, fake_client, _dm_event("some details"))

    convo = session.execute(select(Conversation)).scalar_one()
    assert convo.status == "gathering"
    assert "something went wrong" in fake_client.posted[-1]["text"]
    # The failed user turn was rolled back so a retry starts clean.
    context = json.loads(convo.gathered_context)
    assert len(context["history"]) == 1
