# CLAUDE.md — localai-bot

Discord bot (JARVIS) backed by local qwen2.5-14b. Runs as Docker container on ai-agent-net.

## Architecture

- `bot.py` — entry point, JARVISBot subclass, loads two cogs
- `llm.py` — async HTTP client to localai-litellm:4000/v1
- `cogs/chat.py` — ChatCog: @mention anywhere -> LLM response
- `cogs/alerts.py` — AlertsCog: auto-triage bot messages in homelab-alerts + @mention in that channel

## Key constants

- `ALERTS_CHANNEL_ID` — env var, default 1488857934061633697 (homelab-alerts)
- `LITELLM_URL` — env var, default http://localai-litellm:4000/v1/chat/completions
- Bot token: `DISCORD_BOT_TOKEN` in `.env`

## Deploy

```bash
docker compose up -d
docker logs localai-jarvis --follow
```

## Tests

```bash
.venv/bin/pytest tests/ -v
```

## Notes

- Container must be on ai-agent-net to reach localai-litellm
- JARVIS token was freed from HermesAgent (archived 2026-04-24)
- Both HermesAgent and openclaw are archived at ~/projects/archived/
