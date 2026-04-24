# localai-bot

A lightweight, self-hosted AI triage bot for homelab and infrastructure alerts. Runs entirely on your own hardware using a local LLM — no data leaves your network.

When an automated alert posts to your chat channel, the bot responds immediately with a severity assessment, likely cause, and recommended next step. No essays. Just the facts.

---

## What it does

- **Auto-triage**: Watches a designated alerts channel. When a monitoring tool posts an alert (bot/webhook message), the bot analyses it and replies with:
  1. Severity — critical / high / medium / low
  2. Likely cause — one sentence
  3. Recommended next step — one sentence
- **Follow-up questions**: @mention the bot in the alerts channel to ask for more detail on a triage response
- **General chat**: @mention the bot in any channel for homelab Q&A

Everything runs locally. The LLM never calls home.

---

## Architecture

```
Monitoring tool (homelab-scripts, Watchtower, etc.)
        │
        ▼
  Chat platform (Discord / Slack / Teams)
  [alerts channel]
        │  new bot/webhook message
        ▼
   localai-bot (discord.py)
        │
        ▼
  Local LLM via LiteLLM proxy
  (e.g. Qwen2.5-14B on local GPU)
        │
        ▼
  Triage reply posted to alerts channel
```

### Components

| File | Role |
|---|---|
| `bot.py` | Entry point — loads cogs, connects to chat platform |
| `llm.py` | Async HTTP client to LiteLLM (`/v1/chat/completions`) |
| `cogs/chat.py` | ChatCog — @mention handler, per-channel conversation history |
| `cogs/alerts.py` | AlertsCog — auto-triage + @mention in alerts channel |

---

## Requirements

- Docker + Docker Compose
- A LiteLLM-compatible local LLM endpoint (e.g. [LiteLLM](https://github.com/BerriAI/litellm) proxying Ollama, llama.cpp, or vLLM)
- A Discord bot token (or equivalent for your platform)
- The bot and LiteLLM container on the same Docker network

---

## Setup

**1. Clone and configure**

```bash
git clone <this repo>
cd localai-bot
cp .env.example .env
```

Edit `.env`:

```
DISCORD_BOT_TOKEN=your_bot_token_here
ALERTS_CHANNEL_ID=your_channel_id_here
LITELLM_URL=http://your-litellm-host:4000/v1/chat/completions
```

**2. Discord bot setup**

In the [Discord Developer Portal](https://discord.com/developers/applications):
- Create an application, add a Bot
- Enable **Message Content Intent** under Bot → Privileged Gateway Intents
- Invite the bot to your server with `bot` scope and `Send Messages`, `Read Message History` permissions
- Copy the bot token into `.env`

**3. Start**

```bash
docker compose up -d
docker logs localai-bot --follow
```

You should see `[jarvis] connected as <BotName>#XXXX`.

---

## Usage

### Automatic alert triage

Any bot or webhook message posted to the configured alerts channel is automatically triaged. No configuration needed beyond pointing your monitoring tools at that channel.

Example alert → bot response:
```
[alert]  ERROR: disk usage on /dev/sda1 at 94% (threshold: 90%)

[bot]    Severity: high
         Likely cause: log accumulation or large file growth on /dev/sda1
         Next step: run `df -h` and `du -sh /* | sort -rh | head -20` to identify the source
```

### Follow-up questions

@mention the bot in the alerts channel to ask follow-up questions about a triage:

```
@JARVIS what logs should I check first?
```

### General chat

@mention the bot in any channel:

```
@JARVIS what's the difference between RAID 5 and RAID 6?
```

---

## Adapting to other platforms

The bot is written for Discord but the core logic (`llm.py`, `cogs/`) is platform-agnostic. To port to Slack or Microsoft Teams:

1. Replace `discord.py` with the relevant SDK (`slack_bolt`, `botframework`)
2. Rewrite `bot.py` and the `on_message` listeners in both cogs to use the new platform's event model
3. The triage logic, LLM client, and prompts require no changes

The `LITELLM_URL` and model config are fully portable — any OpenAI-compatible endpoint works.

---

## LLM compatibility

`llm.py` speaks the OpenAI chat completions API. Compatible backends include:

- [LiteLLM](https://github.com/BerriAI/litellm) (recommended — proxies most models)
- [Ollama](https://ollama.com) with `--port` flag
- [vLLM](https://github.com/vllm-project/vllm)
- [llama.cpp server](https://github.com/ggerganov/llama.cpp)
- Any hosted provider (OpenAI, Anthropic, Groq) — just update `LITELLM_URL` and set a real API key

---

## Running tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests/ -v
```

---

## Roadmap

Ideas for future development — contributions welcome.

### Multi-channel notification (email / webhook)

Route alerts through an email platform (SendGrid, Postmark, AWS SES) or webhook receiver so that:

- Monitoring tools send alerts to a shared mailbox or webhook endpoint
- A relay service posts them to the chat channel
- The bot triages as normal

This decouples the alerting pipeline from the chat platform and allows non-chat systems (ticketing tools, PagerDuty, etc.) to participate.

### Ticketing integration

After triaging an alert, automatically create a ticket in your system (Jira, Linear, Freshdesk, plain email to a shared inbox):

```
Alert received → triage response posted → ticket created with:
  - alert text
  - bot's severity + cause + next step
  - timestamp and source container/service
```

The ticket becomes the record of the incident. The chat response is the fast human-readable summary.

### Auto-resolution detection

When a follow-up alert arrives suggesting the issue was resolved (e.g. "disk usage back to 62%", "service restarted successfully"), the bot:

1. Matches it against open tickets using alert source and service name
2. Posts a resolution note to the chat thread
3. Closes the ticket automatically

This closes the loop without human intervention for self-healing systems.

### Alert deduplication and grouping

Suppress repeat alerts for the same issue within a configurable window. Group related alerts (same host, same service) into a single triage thread rather than flooding the channel.

---

## License

MIT
