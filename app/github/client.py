"""PyGithub wrapper."""

from github import Github

from app.config import settings


def get_repo():
    """The target repo (owner/name from GITHUB_REPO) issues and PRs go to."""
    if not settings.github_token or not settings.github_repo:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPO must be set in .env")
    return Github(settings.github_token).get_repo(settings.github_repo)
