"""AI Review Agent: advisory pre-review comment on freshly opened PRs.

Runs right after the coding agent opens a PR, before any human looks at it.
Strictly advisory — it never approves, requests changes, or blocks the merge
gate in app/review/merge.py. Any failure here is logged and swallowed.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_REVIEW_MODEL = "gemini-flash-latest"
MAX_DIFF_CHARS = 40_000
COMMENT_HEADER = "## Automated pre-review"

REVIEW_PROMPT = """\
You are reviewing an automated pull request before a human does. Given the
implementation plan and the diff, write a short review (under 300 words) in
markdown covering:

- Potential bugs or risky changes in the diff
- Plan items that appear to be missing or only partially implemented
- Anything else a human reviewer should double-check

Be specific (reference files/lines from the diff). If the change looks clean,
say so briefly. Do not restate the diff.

## Implementation plan

{plan}

## Diff

{diff}
"""


def review_pr(pr, plan_md: str = "") -> str | None:
    """Post an advisory review comment on a PR. Returns the comment body, or
    None if the review was skipped."""
    if not settings.gemini_api_key:
        logger.info("GEMINI_API_KEY not set; skipping AI pre-review of PR #%s", pr.number)
        return None

    diff = _build_diff_text(pr)
    if not diff:
        logger.info("No diff content for PR #%s; skipping AI pre-review", pr.number)
        return None

    review = _call_gemini(
        REVIEW_PROMPT.format(plan=plan_md or "(no plan available)", diff=diff)
    ).strip()
    if not review:
        return None

    body = (
        f"{COMMENT_HEADER}\n\n{review}\n\n"
        "---\n_Advisory only — generated automatically; human approval is still required._"
    )
    pr.create_issue_comment(body)
    logger.info("Posted AI pre-review on PR #%s", pr.number)
    return body


def _build_diff_text(pr) -> str:
    chunks = []
    total = 0
    for f in pr.get_files():
        patch = getattr(f, "patch", None)
        if not patch:
            continue
        chunk = f"### {f.filename}\n```diff\n{patch}\n```"
        if total + len(chunk) > MAX_DIFF_CHARS:
            chunks.append("(diff truncated)")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n\n".join(chunks)


def _call_gemini(prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(model=GEMINI_REVIEW_MODEL, contents=prompt)
    return response.text or ""
