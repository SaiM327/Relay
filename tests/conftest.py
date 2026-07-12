import os
import tempfile

# Must be set before any `app.*` import: config and the DB engine are
# initialized at import time. DEVFLOW_TEST stops config from letting the real
# .env override these values.
os.environ["DEVFLOW_TEST"] = "1"
_db_path = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["REACTION_THRESHOLD"] = "3"
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["SLACK_SIGNING_SECRET"] = "test-secret"
os.environ["GEMINI_API_KEY"] = ""  # force the heuristic classifier in tests
os.environ["GITHUB_TOKEN"] = ""  # never hit real GitHub from tests
os.environ["GITHUB_REPO"] = ""
os.environ["PLANS_DIR"] = os.path.join(tempfile.mkdtemp(), "plans")
os.environ["CACHE_DIR"] = os.path.join(tempfile.mkdtemp(), "cache")

import pytest

from app.db.models import Base, SessionLocal, engine


@pytest.fixture()
def session():
    # Guard against ever creating/dropping tables on a real database: the
    # engine must point at this run's throwaway file.
    assert _db_path in str(engine.url), f"tests wired to non-test DB: {engine.url}"
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s
    Base.metadata.drop_all(engine)


class FakeSlackClient:
    """Minimal stand-in for the Slack Web API client used by the tracker."""

    def __init__(self):
        # (channel, ts) -> {emoji_name: [user_ids]}
        self.reactions: dict[tuple[str, str], dict[str, list[str]]] = {}
        self.messages: dict[tuple[str, str], dict] = {}
        self.posted: list[dict] = []  # chat_postMessage calls, in order

    def add_reaction(self, channel, ts, emoji, user):
        self.reactions.setdefault((channel, ts), {}).setdefault(emoji, [])
        if user not in self.reactions[(channel, ts)][emoji]:
            self.reactions[(channel, ts)][emoji].append(user)

    def remove_reaction(self, channel, ts, emoji, user):
        users = self.reactions.get((channel, ts), {}).get(emoji, [])
        if user in users:
            users.remove(user)

    def reactions_get(self, channel, timestamp):
        reactions = [
            {"name": name, "users": users, "count": len(users)}
            for name, users in self.reactions.get((channel, timestamp), {}).items()
            if users
        ]
        return {"message": {"reactions": reactions}}

    def conversations_history(self, channel, latest, inclusive, limit):
        msg = self.messages.get((channel, latest), {"text": "an idea", "user": "U_AUTHOR"})
        return {"messages": [msg]}

    def conversations_open(self, users):
        return {"channel": {"id": f"D_{users[0]}"}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.example/archives/{channel}/p{message_ts}"}

    def chat_postMessage(self, channel, text):
        self.posted.append({"channel": channel, "text": text})
        return {"ok": True}


@pytest.fixture()
def fake_client():
    return FakeSlackClient()
