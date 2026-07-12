"""Repository Indexer: cached file tree + AI summaries of key files.

The index is keyed by the default branch head SHA and cached as JSON under
CACHE_DIR, so it's built once per repo state instead of on every plan. When
GEMINI_API_KEY is missing or the summarization call fails, the index degrades
gracefully to tree + README only.
"""

import json
import logging
import os
import re

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_INDEX_MODEL = "gemini-flash-lite-latest"
MAX_TREE_FILES = 400
MAX_SUMMARY_FILES = 30
MAX_FILE_CHARS = 6000
MAX_README_CHARS = 4000

_EXCLUDE = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|__pycache__|\.git)/"
    r"|\.(png|jpe?g|gif|svg|ico|pdf|lock|min\.js|map|woff2?|ttf)$"
    r"|(^|/)(package-lock\.json|yarn\.lock|poetry\.lock)$",
    re.IGNORECASE,
)
_SOURCE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h")
_ENTRY_HINTS = ("main", "app", "index", "server", "cli", "run", "setup", "core")

SUMMARY_PROMPT = """\
For each file below, write a one-line summary of what it does/contains.
Respond ONLY with a JSON object mapping each file path to its summary string.

{files_block}
"""


def get_repo_index(repo) -> dict:
    """Return the (possibly cached) index for a repo."""
    head_sha = repo.get_branch(repo.default_branch).commit.sha
    cached = _load_cache(repo.full_name)
    if cached and cached.get("sha") == head_sha:
        logger.info("Repo index cache hit for %s@%s", repo.full_name, head_sha[:8])
        return cached

    logger.info("Building repo index for %s@%s", repo.full_name, head_sha[:8])
    tree_entries = [
        e for e in repo.get_git_tree(head_sha, recursive=True).tree
        if e.type == "blob" and not _EXCLUDE.search(e.path)
    ]
    paths = [e.path for e in tree_entries][:MAX_TREE_FILES]

    index = {
        "sha": head_sha,
        "repo": repo.full_name,
        "tree": paths,
        "readme": _get_readme(repo),
        "summaries": _summarize_key_files(repo, tree_entries),
    }
    _save_cache(repo.full_name, index)
    return index


def format_index_for_prompt(index: dict) -> str:
    lines = [f"## Repo file listing ({index['repo']})", ""]
    lines += index["tree"]
    if index["summaries"]:
        lines += ["", "## Key files", ""]
        lines += [f"- `{path}`: {summary}" for path, summary in index["summaries"].items()]
    lines += ["", "## README (truncated)", "", index["readme"]]
    return "\n".join(lines)


def _pick_key_files(tree_entries) -> list:
    source = [e for e in tree_entries if e.path.endswith(_SOURCE_EXT)]

    def priority(entry):
        name = os.path.basename(entry.path).lower()
        is_entryish = any(hint in name for hint in _ENTRY_HINTS)
        size = getattr(entry, "size", 0) or 0
        return (0 if is_entryish else 1, -size)

    return sorted(source, key=priority)[:MAX_SUMMARY_FILES]


def _summarize_key_files(repo, tree_entries) -> dict:
    if not settings.gemini_api_key:
        return {}
    key_files = _pick_key_files(tree_entries)
    if not key_files:
        return {}

    blocks = []
    for entry in key_files:
        try:
            content = repo.get_contents(entry.path).decoded_content.decode(
                "utf-8", errors="replace"
            )[:MAX_FILE_CHARS]
        except Exception:
            continue
        blocks.append(f"### {entry.path}\n```\n{content}\n```")

    if not blocks:
        return {}
    try:
        return _call_gemini_json(SUMMARY_PROMPT.format(files_block="\n\n".join(blocks)))
    except Exception:
        logger.warning("File summarization failed; index will have no summaries", exc_info=True)
        return {}


def _call_gemini_json(prompt: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=GEMINI_INDEX_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    payload = json.loads(response.text)
    return {str(k): str(v) for k, v in payload.items()} if isinstance(payload, dict) else {}


def _get_readme(repo) -> str:
    try:
        return repo.get_readme().decoded_content.decode("utf-8", errors="replace")[:MAX_README_CHARS]
    except Exception:
        return "(no README)"


def _cache_path(repo_full_name: str) -> str:
    safe = repo_full_name.replace("/", "-")
    return os.path.join(settings.cache_dir, f"repo-index-{safe}.json")


def _load_cache(repo_full_name: str) -> dict | None:
    try:
        with open(_cache_path(repo_full_name)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(repo_full_name: str, index: dict) -> None:
    os.makedirs(settings.cache_dir, exist_ok=True)
    with open(_cache_path(repo_full_name), "w") as f:
        json.dump(index, f)
