"""Positive-sentiment classification for thread replies.

Uses Gemini Flash (free tier) when GEMINI_API_KEY is set; otherwise falls back
to a keyword heuristic so reply tracking works with zero AI setup.
"""

import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_CLASSIFIER_MODEL = "gemini-flash-lite-latest"

_POSITIVE = re.compile(
    r"\+1|👍|🔥|💯|❤️"
    r"|\b(ye+s+|yeah|yep|love (it|this)|great|awesome|amazing|agreed?"
    r"|need(ed)? this|want this|good idea|great idea|ship it|do it|yes please"
    r"|would be (great|awesome|amazing|nice|huge)|sounds good|count me in"
    r"|i'?m in|seconded|second this|upvote)\b",
    re.IGNORECASE,
)

# Meme-style one-word endorsements ("this", "THIS.") only count as the whole message.
_STANDALONE_POSITIVE = {"this", "same", "same!", "this!", "this."}
_NEGATIVE = re.compile(
    r"-1|👎"
    r"|\b(no+|nah|nope|don'?t|do not|disagree|bad idea|not (a )?(good|great)"
    r"|won'?t work|hate|against|downvote|already exists|duplicate|out of scope)\b",
    re.IGNORECASE,
)


def is_positive_reply(text: str) -> bool:
    """True if a thread reply expresses support for the parent message's idea."""
    text = (text or "").strip()
    if not text:
        return False
    if settings.gemini_api_key:
        try:
            return _classify_with_gemini(text)
        except Exception:
            logger.warning("Gemini classification failed; using heuristic", exc_info=True)
    return _heuristic(text)


def _heuristic(text: str) -> bool:
    if text.lower() in _STANDALONE_POSITIVE:
        return True
    return bool(_POSITIVE.search(text)) and not _NEGATIVE.search(text)


def _classify_with_gemini(text: str) -> bool:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    prompt = (
        "You are classifying a Slack thread reply. The parent message proposed a "
        "feature idea or bug report. Does this reply express support, agreement, or "
        "enthusiasm for the idea? Answer with exactly one word: YES or NO.\n\n"
        f"Reply: {text!r}"
    )
    response = client.models.generate_content(model=GEMINI_CLASSIFIER_MODEL, contents=prompt)
    return (response.text or "").strip().upper().startswith("YES")
