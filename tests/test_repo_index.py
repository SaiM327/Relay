import dataclasses

import pytest

from app.planning import repo_index
from app.planning.repo_index import format_index_for_prompt, get_repo_index


class FakeTreeEntry:
    type = "blob"

    def __init__(self, path, size=100):
        self.path = path
        self.size = size


class FakeTree:
    def __init__(self, entries):
        self.tree = entries


class FakeReadme:
    decoded_content = b"# Robot\nControls the robot."


class FakeContents:
    def __init__(self, text):
        self.decoded_content = text.encode()


class FakeBranch:
    def __init__(self, sha):
        self.commit = type("C", (), {"sha": sha})()


class FakeRepo:
    full_name = "acme/robot"
    default_branch = "main"

    def __init__(self, sha="sha-one"):
        self.sha = sha
        self.tree_calls = 0

    def get_branch(self, name):
        return FakeBranch(self.sha)

    def get_git_tree(self, ref, recursive):
        self.tree_calls += 1
        return FakeTree(
            [
                FakeTreeEntry("main.py", size=500),
                FakeTreeEntry("src/vision.py", size=900),
                FakeTreeEntry("assets/logo.png"),
                FakeTreeEntry("package-lock.json"),
            ]
        )

    def get_readme(self):
        return FakeReadme()

    def get_contents(self, path):
        return FakeContents(f"# contents of {path}")


def test_index_contains_filtered_tree_and_readme():
    repo = FakeRepo()
    index = get_repo_index(repo)
    assert index["sha"] == "sha-one"
    assert index["tree"] == ["main.py", "src/vision.py"]
    assert "Controls the robot" in index["readme"]

    text = format_index_for_prompt(index)
    assert "main.py" in text and "logo.png" not in text


def test_index_is_cached_until_sha_changes(monkeypatch, tmp_path):
    patched = dataclasses.replace(repo_index.settings, cache_dir=str(tmp_path))
    monkeypatch.setattr(repo_index, "settings", patched)

    repo = FakeRepo(sha="sha-one")
    get_repo_index(repo)
    get_repo_index(repo)
    assert repo.tree_calls == 1  # second call served from cache

    repo.sha = "sha-two"  # repo moved -> cache invalid
    index = get_repo_index(repo)
    assert repo.tree_calls == 2
    assert index["sha"] == "sha-two"


def test_summaries_skipped_without_api_key():
    index = get_repo_index(FakeRepo(sha="sha-nokey"))
    assert index["summaries"] == {}


def test_summaries_included_with_api_key(monkeypatch):
    patched = dataclasses.replace(repo_index.settings, gemini_api_key="test-key")
    monkeypatch.setattr(repo_index, "settings", patched)
    monkeypatch.setattr(
        repo_index,
        "_call_gemini_json",
        lambda prompt: {"main.py": "entry point", "src/vision.py": "vision utils"},
    )

    index = get_repo_index(FakeRepo(sha="sha-withkey"))
    assert index["summaries"]["main.py"] == "entry point"
    text = format_index_for_prompt(index)
    assert "## Key files" in text and "entry point" in text


def test_summary_failure_degrades_gracefully(monkeypatch):
    patched = dataclasses.replace(repo_index.settings, gemini_api_key="test-key")
    monkeypatch.setattr(repo_index, "settings", patched)

    def boom(prompt):
        raise RuntimeError("gemini down")

    monkeypatch.setattr(repo_index, "_call_gemini_json", boom)

    index = get_repo_index(FakeRepo(sha="sha-fail"))
    assert index["summaries"] == {}
    assert index["tree"]  # tree still present
