import os
import discord
from discord.ext import commands
from cogs.chat import ChatCog
from cogs.alerts import AlertsCog


class JARVISBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.add_cog(ChatCog(self))
        await self.add_cog(AlertsCog(self))

    async def on_ready(self) -> None:
        print(f"[jarvis] connected as {self.user}", flush=True)


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("[jarvis] ERROR: DISCORD_BOT_TOKEN not set")

    intents = discord.Intents.default()
    intents.message_content = True

    bot = JARVISBot(command_prefix="!", intents=intents)
    bot.run(token)


if __name__ == "__main__":
    main()
