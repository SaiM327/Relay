"""Intent Classification Agent: is a popular message actually actionable?

Runs when a message crosses the support threshold, before the DM agent is
engaged. Keeps lunch polls and popular jokes out of the pipeline. Uses Gemini
Flash-Lite when GEMINI_API_KEY is set, keyword heuristic otherwise.
"""

import json
import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_INTENT_MODEL = "gemini-flash-lite-latest"

INTENT_FEATURE = "feature_request"
INTENT_BUG = "bug_report"
INTENT_NOT_ACTIONABLE = "not_actionable"
_VALID_INTENTS = (INTENT_FEATURE, INTENT_BUG, INTENT_NOT_ACTIONABLE)

INTENT_PROMPT = """\
Classify this Slack message that received strong team support. Is it something
an engineering team could act on?

Respond ONLY with JSON: {{"intent": "feature_request" | "bug_report" | "not_actionable"}}

- "feature_request": proposes new functionality, an improvement, or a task
- "bug_report": describes something broken or misbehaving
- "not_actionable": jokes, polls, announcements, questions, social chatter

Message: {text!r}
"""

_BUG_RE = re.compile(
    r"\b(bug|broken|crash(es|ing)?|error|fail(s|ing|ed)?|doesn'?t work|not working"
    r"|regression|glitch|wrong|incorrect)\b",
    re.IGNORECASE,
)
_FEATURE_RE = re.compile(
    r"\b(add|feature|idea|should|need|want|support|improve|allow|implement|build"
    r"|create|make|automate|integrate|upgrade|refactor|calibrate|would be|can we"
    r"|let'?s|we (could|have to|need))\b",
    re.IGNORECASE,
)


def classify_intent(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return INTENT_NOT_ACTIONABLE
    if settings.gemini_api_key:
        try:
            return _classify_with_gemini(text)
        except Exception:
            logger.warning("Gemini intent classification failed; using heuristic", exc_info=True)
    return _heuristic(text)


def _heuristic(text: str) -> str:
    if _BUG_RE.search(text):
        return INTENT_BUG
    if _FEATURE_RE.search(text):
        return INTENT_FEATURE
    return INTENT_NOT_ACTIONABLE


def _classify_with_gemini(text: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=GEMINI_INTENT_MODEL,
        contents=INTENT_PROMPT.format(text=text),
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    intent = (json.loads(response.text).get("intent") or "").strip()
    if intent not in _VALID_INTENTS:
        raise ValueError(f"unexpected intent {intent!r}")
    return intent
