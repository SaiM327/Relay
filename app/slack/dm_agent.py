"""Phase 2: conversational info-gathering agent (DMs the original poster).

On trigger, opens a DM referencing the original message and asks the poster to
expand. Each DM reply (text and/or attached images/PDFs) is fed to Gemini,
which either asks ONE clarifying question or declares it has enough info via
{"done": true, "summary": "..."}. The full transcript accumulates in
Conversation.gathered_context as JSON.
"""

import json
import logging
import threading
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Conversation, TrackedMessage, WorkItem

logger = logging.getLogger(__name__)

# Rolling alias: always points at the current Flash generation. Pinned
# versions (e.g. gemini-2.5-flash) get retired for new accounts.
GEMINI_DM_MODEL = "gemini-flash-latest"
# Tried when the primary is overloaded (503s are common on the free tier).
GEMINI_DM_FALLBACK_MODELS = ("gemini-2.0-flash", "gemini-flash-lite-latest")
# After this many agent questions, the model is told to wrap up with a summary.
MAX_AGENT_QUESTIONS = 5
SUPPORTED_MIMETYPES = ("image/", "application/pdf")

SYSTEM_PROMPT = """\
You are gathering a feature/bug report from a Slack user whose idea got enough
team support to be turned into a GitHub issue. Given the conversation so far
and any attached images/documents, either ask ONE short clarifying question or
declare you have enough info.

Respond ONLY with a JSON object, no other text:
- To ask a question: {"done": false, "question": "..."}
- To finish:         {"done": true, "summary": "..."}
- If the user wants to cancel, withdraw the idea, or stop the process
  (e.g. "nevermind", "cancel this", "forget it"): {"cancelled": true}

Never treat a cancellation as having enough info — cancelling must produce
{"cancelled": true}, not a summary.

If the current message includes attachments, also add an "attachment_summary"
field describing what they show (it is your only chance to record them — the
raw files will not be available later).

The final summary must be a complete, self-contained description of the
feature/bug suitable for a GitHub issue: what, why, expected behavior, and any
details from screenshots. Finish as soon as you genuinely have enough — do not
drag the conversation out.\
"""


@dataclass
class AgentReply:
    done: bool
    text: str  # question if not done, summary if done
    attachment_summary: str = ""
    cancelled: bool = False


def start_conversation(session: Session, client, tracked: TrackedMessage) -> None:
    """Open a DM with the original poster and create the Conversation row."""
    if not tracked.author_slack_id:
        logger.warning("Tracked message %s has no author; skipping DM", tracked.id)
        return

    dm = client.conversations_open(users=[tracked.author_slack_id])
    dm_channel = dm["channel"]["id"]

    permalink = None
    try:
        permalink = client.chat_getPermalink(
            channel=tracked.slack_channel_id, message_ts=tracked.slack_message_ts
        )["permalink"]
        reference = f"<{permalink}|your idea>"
    except Exception:
        reference = f"your idea (“{tracked.text[:80]}”)"

    intro = (
        f"Hey! :wave: Looks like {reference} got {tracked.reaction_count} "
        "supporters, so I'd like to turn it into a GitHub issue.\n"
        "Could you expand on it a bit — what problem does it solve, and how should "
        "it behave? Feel free to attach screenshots."
    )
    client.chat_postMessage(channel=dm_channel, text=intro)

    convo = Conversation(
        tracked_message_id=tracked.id,
        slack_dm_channel_id=dm_channel,
        status="gathering",
        gathered_context=json.dumps(
            {
                "idea": tracked.text,
                "permalink": permalink,
                "history": [{"role": "agent", "text": intro}],
                "summary": None,
            }
        ),
    )
    session.add(convo)
    session.flush()
    logger.info("Opened DM %s with %s for tracked message %s", dm_channel, tracked.author_slack_id, tracked.id)


def handle_dm_message(session: Session, client, event: dict) -> None:
    """Route a message.im event into its active gathering conversation."""
    convo = session.execute(
        select(Conversation)
        .where(
            Conversation.slack_dm_channel_id == event["channel"],
            Conversation.status == "gathering",
        )
        .order_by(Conversation.updated_at.desc())
    ).scalars().first()
    if convo is None:
        # Recovery path: a conversation stuck in "ready" means issue creation
        # failed earlier. Any DM retries it.
        stuck = session.execute(
            select(Conversation)
            .where(
                Conversation.slack_dm_channel_id == event["channel"],
                Conversation.status == "ready",
            )
            .order_by(Conversation.updated_at.desc())
        ).scalars().first()
        if stuck is not None:
            client.chat_postMessage(channel=event["channel"], text="Retrying the GitHub issue…")
            _fire_issue_creation(session, client, event["channel"], stuck)
            return
        # Follow-up mode: no active gathering, but the user has history here —
        # answer with the status of their most recent idea.
        status_text = _followup_status(session, event["channel"])
        if status_text:
            client.chat_postMessage(channel=event["channel"], text=status_text)
            return
        # No conversation at all — don't leave the user wondering.
        client.chat_postMessage(
            channel=event["channel"],
            text="I don't have an active conversation with you right now. Ideas "
            "kick off when a message gets enough reactions in a channel — I'll "
            "DM you when one of yours does!",
        )
        return

    if not settings.gemini_api_key:
        logger.error("GEMINI_API_KEY not set; cannot run the DM agent")
        client.chat_postMessage(
            channel=event["channel"],
            text="Sorry, I can't process this right now (AI backend not configured).",
        )
        return

    context = json.loads(convo.gathered_context)
    text = event.get("text", "")
    attachments = _download_attachments(event.get("files") or [])

    user_entry = {"role": "user", "text": text}
    if attachments:
        user_entry["attachments"] = [name for name, _, _ in attachments]
    context["history"].append(user_entry)

    questions_asked = sum(
        1 for turn in context["history"] if turn["role"] == "agent"
    )
    try:
        reply = _agent_step(context, attachments, force_finish=questions_asked > MAX_AGENT_QUESTIONS)
    except Exception:
        logger.exception("DM agent step failed")
        client.chat_postMessage(
            channel=event["channel"],
            text="Sorry, something went wrong on my end — could you say that again?",
        )
        context["history"].pop()
        convo.gathered_context = json.dumps(context)
        session.commit()
        return

    if reply.attachment_summary:
        context["history"].append({"role": "system", "text": f"[attachments: {reply.attachment_summary}]"})

    if reply.cancelled:
        convo.status = "cancelled"
        convo.gathered_context = json.dumps(context)
        session.commit()
        client.chat_postMessage(
            channel=event["channel"],
            text="No problem, consider it dropped. If it comes up again, just get "
            "people reacting to a new message. :+1:",
        )
        logger.info("Conversation %s cancelled by user", convo.id)
        return

    if reply.done:
        context["summary"] = reply.text
        convo.status = "ready"
        convo.gathered_context = json.dumps(context)
        session.commit()
        client.chat_postMessage(
            channel=event["channel"],
            text="Perfect, I've got everything I need. Filing a GitHub issue now — "
            "I'll keep you posted. :rocket:",
        )
        logger.info("Conversation %s ready; summary gathered — firing Phase 3", convo.id)
        _fire_issue_creation(session, client, event["channel"], convo)
    else:
        context["history"].append({"role": "agent", "text": reply.text})
        convo.gathered_context = json.dumps(context)
        session.commit()
        client.chat_postMessage(channel=event["channel"], text=reply.text)


def _followup_status(session: Session, dm_channel: str) -> str | None:
    """Status blurb for the user's most recent conversation in this DM, or
    None if they've never had one."""
    convo = session.execute(
        select(Conversation)
        .where(Conversation.slack_dm_channel_id == dm_channel)
        .order_by(Conversation.updated_at.desc())
    ).scalars().first()
    if convo is None:
        return None

    idea = (json.loads(convo.gathered_context).get("idea") or "your idea")[:80]
    footer = (
        "\n\nTo start something new, post it in a channel and get people reacting."
    )

    if convo.status == "cancelled":
        return f"Your last idea (“{idea}”) was cancelled at your request." + footer

    work_item = session.execute(
        select(WorkItem).where(WorkItem.conversation_id == convo.id)
    ).scalars().first()
    if work_item is None:
        return f"Your last idea (“{idea}”) never made it to a GitHub issue." + footer

    issue_url = f"https://github.com/{settings.github_repo}/issues/{work_item.github_issue_number}"
    issue_ref = f"<{issue_url}|issue #{work_item.github_issue_number}>"
    pr_url = f"https://github.com/{settings.github_repo}/pull/{work_item.pr_number}"

    if work_item.pr_status == "merged":
        status = "the PR was merged. All done! :tada:"
    elif work_item.pr_status == "in_review":
        status = f"<{pr_url}|PR #{work_item.pr_number}> is open and waiting for a human approval."
    elif work_item.pr_status == "approved":
        status = f"<{pr_url}|PR #{work_item.pr_number}> is approved and will merge once CI is green."
    elif work_item.pr_status == "failed":
        status = (
            "the coding agent hit a snag and couldn't open a PR. The issue and "
            "context are saved, so the team can retry it."
        )
    elif not work_item.plan_md_path:
        status = (
            "plan generation failed, so the coding agent is holding off. The "
            "team can retry it from the server."
        )
    else:
        status = "the coding agent is working on it — I'll DM you when the PR is up."

    return f"Here's where “{idea}” stands: I filed {issue_ref}, and {status}" + footer


def _download_attachments(files: list[dict]) -> list[tuple[str, bytes, str]]:
    """Fetch supported files with the bot token. Returns (name, bytes, mimetype)."""
    import requests

    out: list[tuple[str, bytes, str]] = []
    for f in files:
        mimetype = f.get("mimetype", "")
        if not mimetype.startswith(SUPPORTED_MIMETYPES):
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        out.append((f.get("name", "file"), resp.content, mimetype))
    return out


def _agent_step(
    context: dict, attachments: list[tuple[str, bytes, str]], force_finish: bool
) -> AgentReply:
    """One Gemini call: transcript + current attachments in, JSON decision out."""
    from google import genai
    from google.genai import types

    transcript_lines = [f"Original Slack idea: {context['idea']}", "", "Conversation so far:"]
    for turn in context["history"]:
        transcript_lines.append(f"{turn['role']}: {turn['text']}")
    if force_finish:
        transcript_lines.append(
            "\nYou have asked enough questions. You MUST respond with done=true now, "
            "summarizing everything you have."
        )

    parts: list = ["\n".join(transcript_lines)]
    for name, data, mimetype in attachments:
        parts.append(types.Part.from_bytes(data=data, mime_type=mimetype))

    client = genai.Client(api_key=settings.gemini_api_key)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
    )
    response = None
    last_error: Exception | None = None
    for model in (GEMINI_DM_MODEL, *GEMINI_DM_FALLBACK_MODELS):
        try:
            response = client.models.generate_content(model=model, contents=parts, config=config)
            break
        except genai.errors.APIError as e:
            # 429 (quota) and 5xx (overload) are worth retrying on another
            # model; anything else (bad request, auth) will fail there too.
            if e.code not in (429, 500, 502, 503, 504):
                raise
            logger.warning("Model %s unavailable (%s); trying next fallback", model, e.code)
            last_error = e
    if response is None:
        raise last_error  # type: ignore[misc]
    payload = json.loads(response.text)

    if payload.get("cancelled"):
        return AgentReply(done=False, text="", cancelled=True)
    if payload.get("done"):
        return AgentReply(done=True, text=payload.get("summary", ""), attachment_summary=payload.get("attachment_summary", ""))
    return AgentReply(done=False, text=payload.get("question", ""), attachment_summary=payload.get("attachment_summary", ""))


def _fire_issue_creation(session: Session, client, channel: str, convo: Conversation) -> None:
    """Phase 3: file a GitHub issue + plan.md, then tell the poster."""
    from app.github.issues import process_ready_conversation

    try:
        work_item, issue = process_ready_conversation(session, convo)
    except Exception:
        logger.exception("Issue creation failed for conversation %s", convo.id)
        client.chat_postMessage(
            channel=channel,
            text="Hmm, I couldn't file the GitHub issue (the team should check the "
            "server logs). Your write-up is saved, so nothing is lost.",
        )
        return

    if work_item.plan_md_path:
        client.chat_postMessage(
            channel=channel,
            text=f"Done! Filed <{issue.html_url}|issue #{issue.number}> and drafted an "
            "implementation plan. The coding agent is on it — I'll send you the PR "
            "when it's ready (usually a few minutes).",
        )
        _run_coding_agent_async(client, channel, work_item.id)
    else:
        client.chat_postMessage(
            channel=channel,
            text=f"Done! Filed <{issue.html_url}|issue #{issue.number}>. Plan "
            "generation failed though, so the coding agent is holding off — the "
            "team can retry it later.",
        )


def _run_coding_agent_async(client, channel: str, work_item_id: int) -> None:
    """Run Phase 4 on a worker thread; DM the outcome when it finishes."""
    from app.coding_agent.runner import process_work_item
    from app.db.models import SessionLocal

    def worker() -> None:
        with SessionLocal() as session:
            work_item = session.get(WorkItem, work_item_id)
            try:
                pr_url = process_work_item(session, work_item)
            except Exception:
                logger.exception("Coding agent failed for work item %s", work_item_id)
                client.chat_postMessage(
                    channel=channel,
                    text="The coding agent hit a snag and couldn't open a PR — the "
                    "team should check the server logs. The issue and plan are "
                    "still there, so this can be retried.",
                )
                return
            client.chat_postMessage(
                channel=channel,
                text=f"PR is up: <{pr_url}|#{work_item.pr_number}> :tada: "
                "It'll merge automatically once a human approves it.",
            )

    threading.Thread(target=worker, daemon=True, name=f"coding-agent-{work_item_id}").start()
