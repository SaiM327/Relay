from sqlalchemy import select

from app.db.models import Conversation, TrackedMessage
from app.slack.intent import (
    INTENT_BUG,
    INTENT_FEATURE,
    INTENT_NOT_ACTIONABLE,
    _heuristic,
)
from app.slack.reaction_tracker import handle_reaction_change

CHANNEL = "C123"
TS = "1720000000.000100"


def _trigger_message(session, client, text):
    client.messages[(CHANNEL, TS)] = {"text": text, "user": "U_AUTHOR"}
    for user in ("U1", "U2", "U3"):
        client.add_reaction(CHANNEL, TS, "thumbsup", user)
        handle_reaction_change(session, client, CHANNEL, TS)


def test_heuristic_intents():
    assert _heuristic("the login page crashes on submit") == INTENT_BUG
    assert _heuristic("we should add dark mode to settings") == INTENT_FEATURE
    assert _heuristic("we need to calibrate the robot with the camera") == INTENT_FEATURE
    assert _heuristic("pizza friday???") == INTENT_NOT_ACTIONABLE
    assert _heuristic("") == INTENT_NOT_ACTIONABLE


def test_actionable_message_gets_dm_and_intent(session, fake_client):
    _trigger_message(session, fake_client, "we should add dark mode")
    row = session.execute(select(TrackedMessage)).scalar_one()
    assert row.intent == INTENT_FEATURE
    assert row.triggered is True
    assert session.execute(select(Conversation)).scalar_one_or_none() is not None
    assert len(fake_client.posted) == 1  # the DM intro


def test_not_actionable_message_skips_dm_but_stays_triggered(session, fake_client):
    _trigger_message(session, fake_client, "who wants pizza on friday???")
    row = session.execute(select(TrackedMessage)).scalar_one()
    assert row.intent == INTENT_NOT_ACTIONABLE
    assert row.triggered is True  # must never re-fire
    assert session.execute(select(Conversation)).scalar_one_or_none() is None
    assert fake_client.posted == []

    # Further reactions don't re-classify or fire anything.
    fake_client.add_reaction(CHANNEL, TS, "fire", "U4")
    handle_reaction_change(session, fake_client, CHANNEL, TS)
    assert fake_client.posted == []
