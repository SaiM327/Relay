"""Phase 4: git sandbox — clone, branch creation, commit/push, cleanup."""

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GIT_USER_NAME = "Relay Bot"
GIT_USER_EMAIL = "relay-bot@users.noreply.github.com"


def _git(cwd: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@dataclass
class Sandbox:
    path: str  # the repo checkout
    branch: str
    _tmpdir: str

    def commit_all(self, message: str) -> bool:
        """Stage and commit everything. Returns False if there were no changes."""
        _git(self.path, "add", "-A")
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=self.path
        )
        if diff.returncode == 0:
            return False
        _git(self.path, "commit", "-m", message)
        return True

    def push(self) -> None:
        _git(self.path, "push", "origin", self.branch)

    def cleanup(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)


def create_sandbox(clone_url: str, branch: str) -> Sandbox:
    """Shallow-clone the repo into a temp dir and check out a new branch."""
    tmpdir = tempfile.mkdtemp(prefix="relay-sandbox-")
    path = os.path.join(tmpdir, "repo")
    try:
        _git(tmpdir, "clone", "--depth", "1", clone_url, path)
        _git(path, "checkout", "-b", branch)
        _git(path, "config", "user.name", GIT_USER_NAME)
        _git(path, "config", "user.email", GIT_USER_EMAIL)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    logger.info("Sandbox ready at %s on branch %s", path, branch)
    return Sandbox(path=path, branch=branch, _tmpdir=tmpdir)
