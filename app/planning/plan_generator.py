"""Planning Agent: turns issue + repo index into plan.md.

The plan is grounded in the cached repository index (file tree + AI summaries
of key files + README, see repo_index.py) so it references real file paths
rather than guessing.
"""

import logging
import os

from app.config import settings
from app.planning.repo_index import format_index_for_prompt, get_repo_index

logger = logging.getLogger(__name__)

# Pro for plan quality; falls back to Flash if the free-tier Pro quota is out.
GEMINI_PLAN_MODEL = "gemini-pro-latest"
GEMINI_PLAN_FALLBACK_MODEL = "gemini-flash-latest"

PROMPT_TEMPLATE = """\
Given this feature request and this repo index, write a concrete
implementation plan: which files to touch, what changes are needed, and how to
verify the change works. Reference real file paths from the index — do not
invent paths. If the request needs new files, say where they should live and
why. Output as markdown with sections: Overview, Changes (per file), New files
(if any), Verification.

## Feature request: {title}

{body}

{repo_index}
"""


def generate_plan(issue_title: str, issue_body: str, repo) -> str:
    index = get_repo_index(repo)
    prompt = PROMPT_TEMPLATE.format(
        title=issue_title,
        body=issue_body,
        repo_index=format_index_for_prompt(index),
    )
    try:
        return _call_gemini(GEMINI_PLAN_MODEL, prompt)
    except Exception:
        logger.warning("Plan model %s failed; retrying with %s", GEMINI_PLAN_MODEL, GEMINI_PLAN_FALLBACK_MODEL)
        return _call_gemini(GEMINI_PLAN_FALLBACK_MODEL, prompt)


def _call_gemini(model: str, prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text or ""


def save_plan(issue_number: int, plan_md: str) -> str:
    os.makedirs(settings.plans_dir, exist_ok=True)
    path = os.path.abspath(os.path.join(settings.plans_dir, f"issue-{issue_number}-plan.md"))
    with open(path, "w") as f:
        f.write(plan_md)
    return path
