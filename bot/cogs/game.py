"""
Game lifecycle and player commands.

  /newgame    — (admin) open a new game in 'join' phase
  /startgame  — (admin) close the roster and begin play
  /endgame    — (admin) end the active game without a winner
  /join       — add yourself to the active game's roster
  /turn       — show whose turn it is
  /players    — list the active game's roster
  /scores     — leaderboard for the active game
  /score      — check a single player's score
  /directions — how to play
  /rules      — show the current rules.py
  /ruleinfo   — show key rule constants
"""

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import engine

log = logging.getLogger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return perms is not None and perms.manage_guild


class GameCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    @app_commands.command(name="newgame", description="(admin) Open a new Nomic game in join phase.")
    @app_commands.default_permissions(manage_guild=True)
    async def newgame(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        active = await self.bot.db.get_active_game()
        if active is not None:
            await interaction.response.send_message(
                f"❌ Game #{active['id']} is already active (phase: **{active['phase']}**). "
                f"End it with `/endgame` before starting a new one.",
                ephemeral=True,
            )
            return

        game_id = await self.bot.db.create_game()
        await interaction.response.send_message(
            f"🎲 **Game #{game_id}** opened. Use `/join` to enter, then "
            f"an admin runs `/startgame` to begin."
        )

    @app_commands.command(name="startgame", description="(admin) Close the roster and begin play.")
    @app_commands.default_permissions(manage_guild=True)
    async def startgame(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        game = await self.bot.db.get_active_game()
        if game is None:
            await interaction.response.send_message("❌ No active game. Run `/newgame` first.", ephemeral=True)
            return
        if game["phase"] != "join":
            await interaction.response.send_message(
                f"❌ Game #{game['id']} is in phase **{game['phase']}**, not 'join'.",
                ephemeral=True,
            )
            return

        players = await self.bot.db.get_game_players(game["id"])
        rules = self.bot.rules
        ok = engine.call_rule(rules, "can_start_game", players, default=len(players) >= 2)
        if not ok:
            await interaction.response.send_message(
                f"❌ Not enough players to start ({len(players)} joined).",
                ephemeral=True,
            )
            return

        first_id = engine.safe_next_player(rules, None, players)
        if first_id is None:
            await interaction.response.send_message("❌ Could not determine first player.", ephemeral=True)
            return

        await self.bot.db.start_game(game["id"], first_id)
        first_name = next((p["name"] for p in players if p["discord_id"] == first_id), first_id)
        await interaction.response.send_message(
            f"🚀 **Game #{game['id']} started** with {len(players)} players.\n"
            f"First turn: <@{first_id}> ({first_name})"
        )

    @app_commands.command(name="endgame", description="(admin) End the active game without a winner.")
    @app_commands.default_permissions(manage_guild=True)
    async def endgame(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        game = await self.bot.db.get_active_game()
        if game is None:
            await interaction.response.send_message("❌ No active game.", ephemeral=True)
            return

        await self.bot.db.finish_game(game["id"], None)
        await interaction.response.send_message(f"🏁 **Game #{game['id']} ended** (no winner declared).")

    # ── Player ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="join", description="Join the active Nomic game.")
    async def join(self, interaction: discord.Interaction) -> None:
        game = await self.bot.db.get_active_game()
        if game is None:
            await interaction.response.send_message(
                "❌ No active game. Wait for an admin to run `/newgame`.",
                ephemeral=True,
            )
            return

        players = await self.bot.db.get_game_players(game["id"])
        rules = self.bot.rules
        allowed = engine.call_rule(
            rules, "can_join_game",
            str(interaction.user.id), players, game["phase"],
            default=(game["phase"] == "join"),
        )
        if not allowed:
            await interaction.response.send_message(
                f"❌ Cannot join: game is in phase **{game['phase']}**.",
                ephemeral=True,
            )
            return

        added = await self.bot.db.add_player_to_game(
            game["id"], str(interaction.user.id), interaction.user.display_name
        )
        if added:
            await interaction.response.send_message(
                f"Welcome to Game #{game['id']}, **{interaction.user.display_name}**!"
            )
        else:
            await interaction.response.send_message("You're already in this game.", ephemeral=True)

    @app_commands.command(name="turn", description="Show whose turn it is.")
    async def turn(self, interaction: discord.Interaction) -> None:
        game = await self.bot.db.get_active_game()
        if game is None or game["phase"] != "playing":
            await interaction.response.send_message("❌ No game is currently being played.", ephemeral=True)
            return
        turn_id = game["current_turn_player_id"]
        if not turn_id:
            await interaction.response.send_message("❌ No turn is set.", ephemeral=True)
            return
        await interaction.response.send_message(f"🎯 Current turn: <@{turn_id}>")

    @app_commands.command(name="players", description="List the active game's roster.")
    async def players(self, interaction: discord.Interaction) -> None:
        game = await self.bot.db.get_active_game()
        if game is None:
            await interaction.response.send_message("❌ No active game.", ephemeral=True)
            return

        roster = await self.bot.db.get_game_players(game["id"])
        if not roster:
            await interaction.response.send_message("No players have joined yet.", ephemeral=True)
            return

        lines = []
        for p in roster:
            marker = " 🎯" if p["discord_id"] == game["current_turn_player_id"] else ""
            lines.append(f"• **{p['name']}** — {p['points']} pts{marker}")

        embed = discord.Embed(
            title=f"Game #{game['id']} — {game['phase']}",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="scores", description="Show the leaderboard for the active game.")
    async def scores(self, interaction: discord.Interaction) -> None:
        game = await self.bot.db.get_active_game()
        if game is None:
            await interaction.response.send_message("❌ No active game.", ephemeral=True)
            return

        rows = await self.bot.db.get_leaderboard(game["id"], limit=10)
        if not rows:
            await interaction.response.send_message("No players yet. Use `/join`.", ephemeral=True)
            return

        MEDALS = ["🥇", "🥈", "🥉"]
        lines = [
            f"{MEDALS[i] if i < len(MEDALS) else f'**{i+1}.**'} {r['discord_name']} — **{r['points']} pts**"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(
            title=f"Game #{game['id']} — Leaderboard",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="score", description="Check your score (or another player's) in the active game.")
    @app_commands.describe(user="Player to look up (defaults to you).")
    async def score(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        game = await self.bot.db.get_active_game()
        if game is None:
            await interaction.response.send_message("❌ No active game.", ephemeral=True)
            return

        target = user or interaction.user
        row = await self.bot.db.get_game_player(game["id"], str(target.id))
        if row is None:
            msg = (
                "You haven't joined this game. Use `/join`."
                if target == interaction.user
                else f"{target.display_name} isn't in this game."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

        await interaction.response.send_message(
            f"**{row['discord_name']}** has **{row['points']} points** in Game #{game['id']}.",
            ephemeral=(target == interaction.user),
        )

    # ── Directions ─────────────────────────────────────────────────────────────

    @app_commands.command(name="directions", description="How to play Nomic.")
    async def directions(self, interaction: discord.Interaction) -> None:
        rules = self.bot.rules
        game = await self.bot.db.get_active_game()
        players = await self.bot.db.get_game_players(game["id"]) if game else []
        quorum = engine.call_rule(
            rules, "compute_quorum", players,
            default=max(2, (len(players) + 1) // 2),
        )
        threshold = engine.call_rule(rules, "compute_passing_threshold", players, default=0.5)
        duration = engine.get_rule(rules, "PROPOSAL_DURATION_HOURS", 48)
        target = engine.get_rule(rules, "TARGET_SCORE", 100)

        embed = discord.Embed(
            title="🎲 How to Play Nomic",
            description=(
                "Nomic is a game where you win by **changing the rules of the game itself**. "
                "The current rules live in `rules.py` — submit changes as unified diffs "
                "(`.patch` files) and your fellow players vote on them."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="1️⃣ Joining a game",
            value=(
                "An admin opens a game with `/newgame`. You `/join` to enter the roster, "
                "then an admin runs `/startgame` to close the roster and begin play."
            ),
            inline=False,
        )
        embed.add_field(
            name="2️⃣ Taking your turn",
            value=(
                "On your turn (check with `/turn`), run `/propose` with a short "
                "description and a `.patch` file — a unified diff against `rules.py`. "
                "You can only propose when it's your turn. Use `/rules` to view the "
                "current `rules.py` you're patching against."
            ),
            inline=False,
        )
        embed.add_field(
            name="📝 Making a patch",
            value=(
                "The bot hosts a live git server. Clone once, then "
                "`git pull` before each new patch to get the current rules:\n"
                "```sh\n"
                "git clone https://nomic.chrisjerrett.com/nomic-rules\n"
                "cd nomic-rules && git pull\n"
                "$EDITOR rules.py\n"
                "git diff > my.patch\n"
                "git checkout rules.py    # reset for next time\n"
                "```\n"
                "No git access? Use `/rules` to download the file, "
                "edit it, then `diff -u rules.py.orig rules.py > my.patch`.\n"
                "Test locally: `patch -p1 < my.patch` in a fresh copy."
            ),
            inline=False,
        )
        embed.add_field(
            name="3️⃣ Voting",
            value=(
                f"A Discord poll opens for **{duration}h**. Players vote ✅ Yes or ❌ No "
                f"using the native poll. By default the proposer cannot vote on their "
                f"own proposal. A proposal passes with **≥{int(threshold*100)}% YES** "
                f"and at least **{quorum} total votes** (scales with the roster). "
                f"Vote weights are configurable via `can_vote` — the game can vote in "
                f"weighted or random-weight voting."
            ),
            inline=False,
        )
        embed.add_field(
            name="4️⃣ Resolution",
            value=(
                "The poll closes when **every eligible voter has voted**, when the "
                f"**{duration}h** timer expires, or when an **admin** runs `/tally`. "
                "At that point the engine tallies votes, applies the "
                "patch if it passed, awards points, and advances the turn. The result "
                "message shows point changes (`<@you> +10 → 35`) and a separate message "
                "announces the next player. Failed proposals still cost your turn."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚡ Transmutation",
            value=(
                "Some rules are tagged `#immutable` (e.g. the core voting logic). To "
                "change them, first submit a **transmutation** — a patch that downgrades "
                "the tag to `#mutable`. Transmutations require **unanimous YES from "
                "every non-proposer player**. Then submit a follow-up amendment to "
                "change the body."
            ),
            inline=False,
        )
        embed.add_field(
            name="✏️ Mistakes",
            value=(
                "Spotted a bug in your patch? `/amend` replaces it (resets votes, "
                "uses remaining time). Want to bail entirely? `/withdraw`."
            ),
            inline=False,
        )
        embed.add_field(
            name="🧪 Custom validity rules",
            value=(
                "Beyond the engine's safety/immutability checks, the game can vote "
                "in arbitrary patch-validity rules via the mutable "
                "`is_valid_patch(patch, description, proposer, players)` callback in "
                "`rules.py`. Examples players have used: max line count, MD5 "
                "proof-of-work (\"hash must end in 0\"), required description tags. "
                "Rejected patches come back with the rule's error message."
            ),
            inline=False,
        )
        embed.add_field(
            name="🏆 Winning",
            value=(
                f"First player to **{target} points** wins. Points and the win condition "
                f"are both mutable — you can change how scoring works through proposals."
            ),
            inline=False,
        )
        embed.set_footer(
            text="See /proposals for the active list · /proposal <id> to view a patch · /ruleinfo for current constants"
        )
        await interaction.response.send_message(embed=embed)

    # ── Rules inspection ───────────────────────────────────────────────────────

    @app_commands.command(name="rules", description="Show the current rules.py and attach it as a file.")
    async def rules(self, interaction: discord.Interaction) -> None:
        try:
            content = self.bot.rules_path.read_text(encoding="utf-8")
        except Exception as exc:
            await interaction.response.send_message(f"❌ Could not read rules.py: {exc}", ephemeral=True)
            return

        # Always attach the full file so players can download and diff against it
        attachment = discord.File(
            io.BytesIO(content.encode("utf-8")),
            filename="rules.py",
        )

        max_preview = 1900
        if len(content) > max_preview:
            preview = content[:max_preview] + "\n…(truncated — full file attached)"
        else:
            preview = content

        await interaction.response.send_message(
            f"```python\n{preview}\n```", file=attachment,
        )

    @app_commands.command(name="ruleinfo", description="Show key rule constants.")
    async def ruleinfo(self, interaction: discord.Interaction) -> None:
        rules = self.bot.rules
        game = await self.bot.db.get_active_game()
        players = await self.bot.db.get_game_players(game["id"]) if game else []

        quorum = engine.call_rule(rules, "compute_quorum", players, default=max(2, (len(players) + 1) // 2))
        threshold = engine.call_rule(rules, "compute_passing_threshold", players, default=0.5)
        duration = engine.get_rule(rules, "PROPOSAL_DURATION_HOURS", 48)
        target = engine.get_rule(rules, "TARGET_SCORE", 100)
        min_players = engine.get_rule(rules, "MIN_PLAYERS_TO_START", 2)

        embed = discord.Embed(title="Current Rule Values", colour=discord.Colour.blurple())
        embed.add_field(name="Roster",             value=str(len(players)),       inline=True)
        embed.add_field(name="Quorum",             value=str(quorum),             inline=True)
        embed.add_field(name="Passing threshold",  value=f"{threshold*100:.0f}%", inline=True)
        embed.add_field(name="Poll duration",      value=f"{duration}h",          inline=True)
        embed.add_field(name="Target score",       value=str(target),             inline=True)
        embed.add_field(name="Min players",        value=str(min_players),        inline=True)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GameCog(bot))
