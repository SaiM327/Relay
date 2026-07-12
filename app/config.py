"""Environment variable loading. Import `settings` everywhere else."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# override=True so .env edits take effect on Flask auto-reloads (the reloader's
# child process inherits stale values from the parent's environment otherwise).
# Tests set DEVFLOW_TEST and their own env vars, which must win over .env.
load_dotenv(override="DEVFLOW_TEST" not in os.environ)


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str = field(default_factory=lambda: os.environ.get("SLACK_BOT_TOKEN", ""))
    slack_signing_secret: str = field(default_factory=lambda: os.environ.get("SLACK_SIGNING_SECRET", ""))
    reaction_threshold: int = field(default_factory=lambda: int(os.environ.get("REACTION_THRESHOLD", "3")))
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///devflow.db"))
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "3000")))

    # Later phases
    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    github_repo: str = field(default_factory=lambda: os.environ.get("GITHUB_REPO", ""))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    plans_dir: str = field(default_factory=lambda: os.environ.get("PLANS_DIR", "plans"))
    cache_dir: str = field(default_factory=lambda: os.environ.get("CACHE_DIR", ".cache"))
    # Command run inside the sandbox to verify the coding agent's work,
    # e.g. "pytest -q" or "npm test". Empty = skip verification.
    target_repo_test_cmd: str = field(default_factory=lambda: os.environ.get("TARGET_REPO_TEST_CMD", ""))
    # Shared secret for GitHub webhook signature verification (repo Settings -> Webhooks)
    github_webhook_secret: str = field(default_factory=lambda: os.environ.get("GITHUB_WEBHOOK_SECRET", ""))
    # Slack channel for shipped-feature announcements (optional)
    team_channel_id: str = field(default_factory=lambda: os.environ.get("TEAM_CHANNEL_ID", ""))


settings = Settings()
