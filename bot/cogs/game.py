"""
/join    — join the game
/scores  — leaderboard
/rules   — display current rules.py
/score   — check your own score
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import engine

log = logging.getLogger(__name__)


class GameCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /join ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="join", description="Join the Nomic game.")
    async def join(self, interaction: discord.Interaction) -> None:
        created = await self.bot.db.ensure_player(
            str(interaction.user.id), interaction.user.display_name
        )
        if created:
            await interaction.response.send_message(
                f"Welcome to Nomic, **{interaction.user.display_name}**! "
                f"You start with 0 points. Propose rule changes to earn more.",
                ephemeral=False,
            )
        else:
            await self.bot.db.update_name(str(interaction.user.id), interaction.user.display_name)
            await interaction.response.send_message("You're already in the game.", ephemeral=True)

    # ── /scores ────────────────────────────────────────────────────────────────

    @app_commands.command(name="scores", description="Show the leaderboard.")
    async def scores(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.get_leaderboard(limit=10)
        if not rows:
            await interaction.response.send_message("No players yet. Use `/join` to start.", ephemeral=True)
            return

        MEDALS = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            medal = MEDALS[i] if i < len(MEDALS) else f"**{i+1}.**"
            lines.append(f"{medal} {row['discord_name']} — **{row['points']} pts**")

        embed = discord.Embed(
            title="Nomic Leaderboard",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ── /score ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="score", description="Check your score (or another player's).")
    @app_commands.describe(user="Player to look up (defaults to you).")
    async def score(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        target = user or interaction.user
        row = await self.bot.db.get_player(str(target.id))
        if row is None:
            msg = (
                "You haven't joined yet. Use `/join`."
                if target == interaction.user
                else f"{target.display_name} hasn't joined yet."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

        await interaction.response.send_message(
            f"**{row['discord_name']}** has **{row['points']} points**.",
            ephemeral=(target == interaction.user),
        )

    # ── /rules ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="rules", description="Display the current rules.py.")
    async def rules(self, interaction: discord.Interaction) -> None:
        try:
            content = self.bot.rules_path.read_text(encoding="utf-8")
        except Exception as exc:
            await interaction.response.send_message(f"❌ Could not read rules.py: {exc}", ephemeral=True)
            return

        # Discord message limit is 2000; code block overhead is ~8 chars
        max_content = 1990
        if len(content) > max_content:
            content = content[:max_content] + "\n…(truncated)"

        await interaction.response.send_message(f"```python\n{content}\n```")

    # ── /ruleinfo ──────────────────────────────────────────────────────────────

    @app_commands.command(name="ruleinfo", description="Show key rule constants.")
    async def ruleinfo(self, interaction: discord.Interaction) -> None:
        rules = self.bot.rules
        quorum       = engine.get_rule(rules, "QUORUM", 3)
        threshold    = engine.get_rule(rules, "PASSING_THRESHOLD", 0.5)
        duration     = engine.get_rule(rules, "PROPOSAL_DURATION_HOURS", 48)
        pts_pass     = engine.get_rule(rules, "POINTS_PASSED", 10)
        pts_fail     = engine.get_rule(rules, "POINTS_FAILED", 0)

        embed = discord.Embed(title="Current Rule Constants", colour=discord.Colour.blurple())
        embed.add_field(name="Quorum",              value=str(quorum),             inline=True)
        embed.add_field(name="Passing threshold",   value=f"{threshold*100:.0f}%", inline=True)
        embed.add_field(name="Poll duration",       value=f"{duration}h",          inline=True)
        embed.add_field(name="Points (pass)",       value=str(pts_pass),           inline=True)
        embed.add_field(name="Points (fail)",       value=str(pts_fail),           inline=True)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GameCog(bot))
