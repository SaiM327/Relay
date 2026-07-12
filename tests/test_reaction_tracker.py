from sqlalchemy import select

from app.db.models import TrackedMessage
from app.slack.reaction_tracker import handle_reaction_change

CHANNEL = "C123"
TS = "1720000000.000100"


def _react(session, client, user, emoji="thumbsup"):
    client.add_reaction(CHANNEL, TS, emoji, user)
    return handle_reaction_change(session, client, CHANNEL, TS)


def _get_row(session):
    return session.execute(
        select(TrackedMessage).where(
            TrackedMessage.slack_channel_id == CHANNEL,
            TrackedMessage.slack_message_ts == TS,
        )
    ).scalar_one()


def test_upsert_creates_single_row(session, fake_client):
    _react(session, fake_client, "U1")
    _react(session, fake_client, "U2")
    rows = session.execute(select(TrackedMessage)).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "an idea"
    assert rows[0].author_slack_id == "U_AUTHOR"


def test_fires_exactly_once_at_threshold(session, fake_client):
    assert _react(session, fake_client, "U1") is False
    assert _react(session, fake_client, "U2") is False
    # Third distinct reactor crosses the threshold of 3.
    assert _react(session, fake_client, "U3") is True
    # Further reactions must not fire again.
    assert _react(session, fake_client, "U4") is False
    assert _get_row(session).triggered is True
    assert _get_row(session).reaction_count == 4


def test_counts_distinct_reactors_not_emoji(session, fake_client):
    # Same two users reacting with many emoji should not cross threshold=3.
    assert _react(session, fake_client, "U1", "thumbsup") is False
    assert _react(session, fake_client, "U1", "fire") is False
    assert _react(session, fake_client, "U2", "rocket") is False
    assert _get_row(session).reaction_count == 2
    assert _get_row(session).triggered is False


def test_removal_drops_count_before_trigger(session, fake_client):
    _react(session, fake_client, "U1")
    _react(session, fake_client, "U2")
    fake_client.remove_reaction(CHANNEL, TS, "thumbsup", "U2")
    handle_reaction_change(session, fake_client, CHANNEL, TS)
    row = _get_row(session)
    assert row.reaction_count == 1
    assert row.triggered is False
    # Climbing back up still fires exactly once at the threshold.
    assert _react(session, fake_client, "U2") is False
    assert _react(session, fake_client, "U3") is True


def test_removal_after_trigger_stays_triggered(session, fake_client):
    _react(session, fake_client, "U1")
    _react(session, fake_client, "U2")
    assert _react(session, fake_client, "U3") is True
    fake_client.remove_reaction(CHANNEL, TS, "thumbsup", "U3")
    assert handle_reaction_change(session, fake_client, CHANNEL, TS) is False
    assert _get_row(session).triggered is True
