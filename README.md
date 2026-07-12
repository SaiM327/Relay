# Relay

An AI-powered idea-to-production pipeline that turns popular Slack messages into merged GitHub pull requests using a chain of specialized agents.

Team members can:

- Post feature ideas or bug reports in any Slack channel
- Support ideas with reactions or positive thread replies
- Expand on their idea in a DM conversation with the bot (including screenshots and PDFs)
- Get the resulting issue, PR, and merge notifications without leaving Slack

Popular ideas are automatically classified, planned against the real codebase, implemented by a coding agent, tested, pre-reviewed by AI, and merged once a human approves.

## Demo Workflow

Team member posts:

> we should add a /health endpoint that returns 200

A few 👍 reactions later, the system flow kicks in:

```
Slack reaction threshold
    ↓
Intent Classification Agent
    ↓
Requirement Gathering Agent (DM)
    ↓
GitHub Issue
    ↓
Repository Indexer (SHA-cached)
    ↓
Planning Agent → plan.md
    ↓
Coding Agent (Gemini CLI in a git sandbox)
    ↓
Independent test run + repair loop
    ↓
Pull Request (Closes #N, own branch)
    ↓
AI Pre-Review Comment
    ↓
Human Approval + Green CI
    ↓
Auto-merge + Slack announcements
```

Response in the poster's DM:

> Done! Filed issue #3 and drafted an implementation plan. The coding agent is on it — I'll send you the PR when it's ready. 🚀
>
> PR is up: #4 🎉 It'll merge automatically once a human approves it.

## Features

### Slack Features

- Support tracking via reactions and positive thread replies (distinct supporters, author excluded)
- Multimodal DM intake — images and PDFs are fed directly to the model
- Cancellation mid-conversation ("nevermind, drop it")
- Follow-up status queries after an issue is filed
- Approval and merge announcements in the original channel

### AI Features

- Intent classification (feature request / bug report / not actionable)
- Positive-reply sentiment classification
- Conversational requirement gathering with structured JSON turns
- Repo-grounded plan generation from a cached repository index
- Autonomous code implementation with a test-failure repair loop (max 2 rounds)
- Advisory AI pre-review comment on every PR

### Backend Features

- Webhook-based architecture (Slack Events API + HMAC-verified GitHub webhooks)
- Relational persistence for messages, conversations, work items, and approvals
- Sandboxed git operations — every issue gets its own `vector/issue-<n>` branch
- Human-gated auto-merge: at least one approving review and green CI, never a timer
- Graceful degradation — model fallbacks, keyword heuristics, non-fatal AI failures

## Architecture

```
Slack (reactions + replies)
    ↓
Flask Webhook (slack-bolt)
    ↓
Intent Classification (Gemini Flash-Lite)
    ↓
Requirement Gathering Agent (Gemini Flash, multimodal)
    ↓
GitHub Issue (PyGithub)
    ↓
Repository Indexer (tree + AI file summaries, SHA-keyed cache)
    ↓
Planning Agent (Gemini Pro) → plan.md
    ↓
Coding Agent (Gemini CLI, temp git clone)
    ↓
Test Runner + Repair Loop
    ↓
Pull Request + AI Pre-Review
    ↓
GitHub Webhook (reviews + check suites)
    ↓
Merge Gate (approval + green CI) → squash merge
    ↓
Slack Notifications (poster DM + channel)
```

## Tech Stack

### Backend

- Python
- Flask
- slack-bolt

### AI

- Google Gemini API (Flash, Flash-Lite, Pro)
- Gemini CLI (headless coding agent)

### Database

- SQLite
- SQLAlchemy ORM

### Integrations

- Slack Events API
- GitHub REST API (PyGithub) + webhooks
- ngrok / cloudflared tunneling for local dev

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Slack, GitHub, and Gemini credentials
npm install -g @google/gemini-cli   # coding agent
python run.py                       # serves on :3000
ngrok http 3000                     # expose for Slack + GitHub webhooks
```

Run the test suite with `pytest`.
