"""
/propose   — submit a .patch file as a rule-change proposal
/proposals — list proposals
/tally     — manually tally an active proposal
"""

import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot import engine

log = logging.getLogger(__name__)

# Max size of uploaded patch files (bytes)
MAX_PATCH_BYTES = 64 * 1024  # 64 KB


async def _get_vote_counts(poll: discord.Poll) -> tuple[int, int]:
    """
    Return (yes_votes, no_votes).
    Convention: answers[0] = YES, answers[1] = NO.
    Uses vote_count when the poll is finalized, otherwise fetches voter lists.
    """
    if not poll.answers:
        return 0, 0

    if poll.results_finalized:
        yes = poll.answers[0].vote_count
        no = poll.answers[1].vote_count if len(poll.answers) > 1 else 0
        return yes, no

    yes = sum(1 async for _ in poll.answers[0].voters())
    no = sum(1 async for _ in poll.answers[1].voters()) if len(poll.answers) > 1 else 0
    return yes, no


class ProposalsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.poll_checker.start()

    async def cog_unload(self) -> None:
        self.poll_checker.cancel()

    # ── Background task ────────────────────────────────────────────────────────

    @tasks.loop(minutes=2)
    async def poll_checker(self) -> None:
        """Tally any proposals whose poll has expired."""
        try:
            expired = await self.bot.db.get_expired_proposals()
            for row in expired:
                await self._tally(row)
        except Exception:
            log.exception("Error in poll_checker")

    @poll_checker.before_loop
    async def before_poll_checker(self) -> None:
        await self.bot.wait_until_ready()

    # ── Core tally logic ───────────────────────────────────────────────────────

    async def _tally(self, row) -> str | None:
        """
        Tally a proposal row.  Returns a status string, or None on hard error.
        Also posts the result to the proposal channel.
        """
        channel = self.bot.get_channel(int(row["poll_channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(row["poll_channel_id"]))
            except Exception:
                log.warning("Cannot find channel %s for proposal #%d", row["poll_channel_id"], row["id"])
                return None

        try:
            message = await channel.fetch_message(int(row["poll_message_id"]))
        except discord.NotFound:
            await self.bot.db.resolve_proposal(row["id"], "failed", 0, 0)
            return "poll message deleted — marked as failed"

        if message.poll is None:
            await self.bot.db.resolve_proposal(row["id"], "failed", 0, 0)
            return "no poll on message — marked as failed"

        yes, no = await _get_vote_counts(message.poll)

        rules = self.bot.rules
        passed = engine.call_rule(
            rules, "tally_vote", yes, no,
            default=lambda: _default_tally(yes, no, rules),
        )
        points = engine.call_rule(rules, "award_points", passed, default=10 if passed else 0)

        status = "passed" if passed else "failed"
        await self.bot.db.resolve_proposal(row["id"], status, yes, no)
        await self.bot.db.award_points(row["proposer_id"], row["proposer_name"], points)

        if passed:
            ok, err = engine.apply_patch(row["patch_text"], self.bot.rules_path, row["id"])
            if ok:
                self.bot.rules = engine.load_rules(self.bot.rules_path)
                verdict = f"✅ **Passed** ({yes}✅ {no}❌) — patch applied. {row['proposer_name']} +{points} pts."
            else:
                verdict = f"✅ Passed ({yes}✅ {no}❌) but patch failed to apply: `{err}`"
        else:
            quorum = engine.get_rule(rules, "QUORUM", 3)
            total = yes + no
            if total < quorum:
                verdict = f"❌ **Failed** — quorum not reached ({total}/{quorum} votes)."
            else:
                verdict = f"❌ **Failed** ({yes}✅ {no}❌)."

        await channel.send(f"**Proposal #{row['id']}** — {verdict}")
        return status

    # ── /propose ───────────────────────────────────────────────────────────────

    @app_commands.command(name="propose", description="Submit a rule-change proposal as a .patch file.")
    @app_commands.describe(
        description="One-line summary of what this proposal changes.",
        patch="A unified diff (.patch) against rules.py only.",
    )
    async def propose(
        self,
        interaction: discord.Interaction,
        description: str,
        patch: discord.Attachment,
    ) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)

        # Validate attachment
        if not patch.filename.endswith(".patch"):
            await interaction.followup.send("❌ Attachment must be a `.patch` file.", ephemeral=True)
            return

        if patch.size > MAX_PATCH_BYTES:
            await interaction.followup.send(
                f"❌ Patch is too large ({patch.size} bytes, max {MAX_PATCH_BYTES}).", ephemeral=True
            )
            return

        patch_bytes = await patch.read()
        try:
            patch_text = patch_bytes.decode("utf-8")
        except UnicodeDecodeError:
            await interaction.followup.send("❌ Patch file must be valid UTF-8.", ephemeral=True)
            return

        # Validate patch
        valid, error, _ = engine.validate_patch(patch_text, self.bot.rules_path)
        if not valid:
            # Truncate long errors so they fit in a Discord message
            if len(error) > 1800:
                error = error[:1800] + "\n…(truncated)"
            await interaction.followup.send(f"❌ **Invalid patch:**\n```\n{error}\n```", ephemeral=True)
            return

        rules = self.bot.rules
        duration_hours = engine.get_rule(rules, "PROPOSAL_DURATION_HOURS", 48)

        proposer_name = interaction.user.display_name
        proposal_id = await self.bot.db.create_proposal(
            proposer_id=str(interaction.user.id),
            proposer_name=proposer_name,
            description=description,
            patch_text=patch_text,
            duration_hours=float(duration_hours),
        )

        # Build Discord poll
        question_text = engine.call_rule(
            rules, "get_poll_question", proposer_name, proposal_id,
            default=f"Proposal #{proposal_id} by {proposer_name} — accept this rule change?",
        )
        answers = engine.call_rule(rules, "get_poll_answers", default=["✅ Yes", "❌ No"])
        if not isinstance(answers, list) or len(answers) < 2:
            answers = ["✅ Yes", "❌ No"]

        # Discord poll duration must be 1–168 hours (integer)
        poll_hours = max(1, min(168, int(duration_hours)))

        poll = discord.Poll(question=str(question_text)[:300], duration=datetime.timedelta(hours=poll_hours))
        for label in answers[:10]:  # Discord max 10 answers
            poll.add_answer(text=str(label)[:55])

        message = await interaction.followup.send(
            f"📋 **Proposal #{proposal_id}:** {description}\n"
            f"*Submitted by {proposer_name} · poll open for {poll_hours}h*",
            poll=poll,
        )

        await self.bot.db.set_proposal_poll(
            proposal_id,
            str(message.id),
            str(interaction.channel_id),
        )

    # ── /tally ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="tally", description="Manually tally a proposal (closes the poll now).")
    @app_commands.describe(proposal_id="The proposal number to tally.")
    async def tally(self, interaction: discord.Interaction, proposal_id: int) -> None:
        await interaction.response.defer(thinking=True)

        row = await self.bot.db.get_proposal(proposal_id)
        if row is None:
            await interaction.followup.send(f"❌ Proposal #{proposal_id} not found.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.followup.send(
                f"❌ Proposal #{proposal_id} is already **{row['status']}**.", ephemeral=True
            )
            return
        if row["poll_message_id"] is None:
            await interaction.followup.send("❌ Proposal has no poll yet.", ephemeral=True)
            return

        result = await self._tally(row)
        if result:
            await interaction.followup.send(f"Tallied proposal #{proposal_id}: **{result}**.")
        else:
            await interaction.followup.send("❌ Could not tally proposal — check logs.", ephemeral=True)

    # ── /proposals ─────────────────────────────────────────────────────────────

    @app_commands.command(name="proposals", description="List recent proposals.")
    @app_commands.describe(status="Filter by status: pending, passed, or failed.")
    @app_commands.choices(status=[
        app_commands.Choice(name="pending", value="pending"),
        app_commands.Choice(name="passed",  value="passed"),
        app_commands.Choice(name="failed",  value="failed"),
    ])
    async def proposals(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        filter_status = status.value if status else None
        rows = await self.bot.db.list_proposals(filter_status)

        if not rows:
            await interaction.response.send_message("No proposals found.", ephemeral=True)
            return

        STATUS_EMOJI = {"pending": "🗳️", "passed": "✅", "failed": "❌"}
        lines = []
        for r in rows:
            emoji = STATUS_EMOJI.get(r["status"], "❓")
            closes = r["closes_at"][:16].replace("T", " ") + " UTC"
            lines.append(
                f"{emoji} **#{r['id']}** {r['description'][:60]} "
                f"— *{r['proposer_name']}* · closes {closes}"
            )

        embed = discord.Embed(
            title="Nomic Proposals",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.response.send_message(embed=embed)


def _default_tally(yes: int, no: int, rules) -> bool:
    quorum = engine.get_rule(rules, "QUORUM", 3)
    threshold = engine.get_rule(rules, "PASSING_THRESHOLD", 0.5)
    total = yes + no
    if total < quorum:
        return False
    return (yes / total) >= threshold


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProposalsCog(bot))
