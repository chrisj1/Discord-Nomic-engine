"""
Nomic Discord Bot — entry point.
"""

import logging
import os
import types
from pathlib import Path

import discord
from discord.ext import commands

from bot import engine
from bot.state import Database

log = logging.getLogger(__name__)

RULES_PATH = Path(os.environ.get("RULES_PATH", "/app/rules/rules.py"))


class NomicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.db = Database()
        self.rules_path: Path = RULES_PATH
        self.rules: types.ModuleType | None = None

    async def setup_hook(self) -> None:
        await self.db.init()

        if not self.rules_path.exists():
            log.error(
                "rules.py not found at %s — is the nomic-rules volume mounted?",
                self.rules_path,
            )
        else:
            self.rules = engine.load_rules(self.rules_path)
            log.info("Loaded rules from %s", self.rules_path)

        await self.load_extension("bot.cogs.proposals")
        await self.load_extension("bot.cogs.game")

        guild_id = os.environ.get("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to 1 hour to propagate)")

    async def on_ready(self) -> None:
        log.info("Nomic bot ready: %s (id=%s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Game(name="Nomic — /propose"))

    async def close(self) -> None:
        await self.db.close()
        await super().close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

    bot = NomicBot()
    bot.run(token)


if __name__ == "__main__":
    main()
