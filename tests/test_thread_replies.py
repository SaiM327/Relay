from sqlalchemy import select

from app.db.models import PositiveReply, TrackedMessage
from app.slack.classifier import _heuristic
from app.slack.reaction_tracker import handle_reaction_change, handle_thread_reply

CHANNEL = "C123"
THREAD_TS = "1720000000.000100"


def _react(session, client, user, emoji="thumbsup"):
    client.add_reaction(CHANNEL, THREAD_TS, emoji, user)
    return handle_reaction_change(session, client, CHANNEL, THREAD_TS)


def _reply(session, client, user, text, reply_ts):
    return handle_thread_reply(
        session, client, CHANNEL, THREAD_TS, reply_ts, user, text
    )


def _get_row(session):
    return session.execute(
        select(TrackedMessage).where(
            TrackedMessage.slack_channel_id == CHANNEL,
            TrackedMessage.slack_message_ts == THREAD_TS,
        )
    ).scalar_one()


def test_positive_replies_count_toward_threshold(session, fake_client):
    assert _react(session, fake_client, "U1") is False
    assert _react(session, fake_client, "U2") is False
    # Third distinct supporter arrives via a positive thread reply.
    assert _reply(session, fake_client, "U3", "yes we need this!", "1720000001.1") is True
    assert _get_row(session).triggered is True


def test_negative_reply_does_not_count(session, fake_client):
    _react(session, fake_client, "U1")
    _react(session, fake_client, "U2")
    assert _reply(session, fake_client, "U3", "nah, bad idea imo", "1720000001.1") is False
    row = _get_row(session)
    assert row.reaction_count == 2
    assert row.triggered is False


def test_authors_own_reply_does_not_count(session, fake_client):
    _react(session, fake_client, "U1")
    _react(session, fake_client, "U2")
    # Parent message author is U_AUTHOR (see FakeSlackClient default).
    assert _reply(session, fake_client, "U_AUTHOR", "yes I really want this", "1720000001.1") is False
    assert _get_row(session).triggered is False


def test_same_replier_counts_once(session, fake_client):
    _react(session, fake_client, "U1")
    assert _reply(session, fake_client, "U2", "great idea", "1720000001.1") is False
    assert _reply(session, fake_client, "U2", "yes please do it", "1720000002.2") is False
    assert _get_row(session).reaction_count == 2


def test_duplicate_reply_event_deduped(session, fake_client):
    _reply(session, fake_client, "U1", "love this", "1720000001.1")
    _reply(session, fake_client, "U1", "love this", "1720000001.1")
    replies = session.execute(select(PositiveReply)).scalars().all()
    assert len(replies) == 1


def test_reactor_who_also_replies_counts_once(session, fake_client):
    _react(session, fake_client, "U1")
    assert _reply(session, fake_client, "U1", "+1 would be great", "1720000001.1") is False
    assert _get_row(session).reaction_count == 1


def test_heuristic_classifier():
    assert _heuristic("+1") is True
    assert _heuristic("yes we absolutely need this") is True
    assert _heuristic("great idea, ship it") is True
    assert _heuristic("nah I don't think so") is False
    assert _heuristic("this already exists, duplicate") is False
    assert _heuristic("what time is standup?") is False
    assert _heuristic("") is False
