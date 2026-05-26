"""
/propose   — submit a .patch file as a rule-change proposal
/amend     — replace the patch on your open proposal
/withdraw  — withdraw your open proposal
/proposals — list proposals in the current game
/proposal  — view a single proposal
/tally     — proposer (or admin) closes their own poll early
"""

import asyncio
import datetime
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot import engine

log = logging.getLogger(__name__)

MAX_PATCH_BYTES = 64 * 1024  # 64 KB


def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return perms is not None and perms.manage_guild


async def _get_vote_counts(
    poll: discord.Poll,
    proposer_id: str,
    rules,
    players: list[dict],
) -> tuple[int, int]:
    """
    Return (yes_votes, no_votes). Filters voters through rules.can_vote
    so the proposer (and non-roster votes) can be excluded per the rules.
    """
    if not poll.answers:
        return 0, 0

    yes_answer = poll.answers[0]
    no_answer = poll.answers[1] if len(poll.answers) > 1 else None

    async def count(answer) -> int:
        n = 0
        async for voter in answer.voters():
            allowed = engine.call_rule(
                rules, "can_vote",
                str(voter.id), proposer_id, players,
                default=(str(voter.id) != proposer_id
                         and any(p["discord_id"] == str(voter.id) for p in players)),
            )
            if allowed:
                n += 1
        return n

    yes = await count(yes_answer)
    no = await count(no_answer) if no_answer else 0
    return yes, no


class ProposalsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Serialize state-changing proposal ops to close create/tally races
        self._propose_lock = asyncio.Lock()
        self._tally_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.poll_checker.start()

    async def cog_unload(self) -> None:
        self.poll_checker.cancel()

    # ── Background task ────────────────────────────────────────────────────────

    @tasks.loop(minutes=2)
    async def poll_checker(self) -> None:
        try:
            expired = await self.bot.db.get_expired_proposals()
            for row in expired:
                await self._tally(row)
        except Exception:
            log.exception("Error in poll_checker")

    @poll_checker.before_loop
    async def before_poll_checker(self) -> None:
        await self.bot.wait_until_ready()

    # ── Core tally / turn advance ──────────────────────────────────────────────

    async def _tally(self, row) -> str | None:
        """Tally a proposal row. Posts result to its channel and advances state.

        Idempotent: re-fetches the row under a lock and bails if status has
        already moved off 'pending' (e.g. another tally won the race).
        """
        async with self._tally_lock:
            current = await self.bot.db.get_proposal(row["id"])
            if current is None or current["status"] != "pending":
                return None  # already resolved by /tally or a prior loop iteration
            row = current

        channel = self.bot.get_channel(int(row["poll_channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(row["poll_channel_id"]))
            except Exception:
                log.warning("Cannot find channel %s for proposal #%d", row["poll_channel_id"], row["id"])
                return None

        rules = self.bot.rules
        game_id = row["game_id"]
        players = await self.bot.db.get_game_players(game_id)

        try:
            message = await channel.fetch_message(int(row["poll_message_id"]))
            poll = message.poll
        except discord.NotFound:
            poll = None

        if poll is None:
            await self.bot.db.resolve_proposal(row["id"], "failed", 0, 0)
            await self._post_resolution(channel, row, "failed", 0, 0, [], "poll missing")
            await self._advance_after_resolution(game_id, row["proposer_id"], players)
            return "failed (no poll)"

        yes, no = await _get_vote_counts(poll, row["proposer_id"], rules, players)
        is_transmutation = bool(row["is_transmutation"])

        # safe_tally_vote enforces engine-side floors that rules.py cannot bypass
        passed = engine.safe_tally_vote(rules, yes, no, players, is_transmutation)

        status = "passed" if passed else "failed"
        await self.bot.db.resolve_proposal(row["id"], status, yes, no)

        # Award points per rules.award_points (returns dict of {id: delta})
        points_dict = engine.call_rule(
            rules, "award_points", passed, row["proposer_id"], players,
            default={row["proposer_id"]: 10 if passed else 0},
        )
        if isinstance(points_dict, dict):
            for pid, delta in points_dict.items():
                if isinstance(delta, int) and delta != 0:
                    await self.bot.db.award_points_in_game(game_id, str(pid), delta)

        # Apply patch if it passed
        patch_note = ""
        if passed:
            ok, err = engine.apply_patch(row["patch_text"], self.bot.rules_path, row["id"])
            if ok:
                self.bot.rules = engine.load_rules(self.bot.rules_path)
                patch_note = "patch applied"
            else:
                patch_note = f"patch failed to apply: {err}"

        await self._post_resolution(channel, row, status, yes, no, players, patch_note)

        # Check winner
        players_after = await self.bot.db.get_game_players(game_id)
        winner_id = engine.call_rule(self.bot.rules, "check_winner", players_after, default=None)
        if winner_id:
            await self.bot.db.finish_game(game_id, str(winner_id))
            winner_name = next(
                (p["name"] for p in players_after if p["discord_id"] == str(winner_id)),
                str(winner_id),
            )
            await channel.send(f"🏆 **Game Over!** <@{winner_id}> ({winner_name}) wins!")
            return status

        await self._advance_after_resolution(game_id, row["proposer_id"], players_after)
        return status

    async def _post_resolution(
        self, channel, row, status, yes, no, players, note: str
    ) -> None:
        rules = self.bot.rules
        kind = "⚡ Transmutation" if row["is_transmutation"] else "Proposal"
        if status == "passed":
            verdict = f"✅ **Passed** ({yes}✅ {no}❌)"
        elif status == "failed":
            if row["is_transmutation"]:
                required = max(1, len(players) - 1)
                total = yes + no
                if total < required:
                    verdict = (
                        f"❌ **Failed** — transmutation requires full participation "
                        f"({total}/{required} eligible voters)"
                    )
                elif no > 0:
                    verdict = f"❌ **Failed** — transmutation requires unanimous YES ({yes}✅ {no}❌)"
                else:
                    verdict = f"❌ **Failed** ({yes}✅ {no}❌)"
            else:
                quorum = engine.call_rule(
                    rules, "compute_quorum", players,
                    default=max(2, (len(players) + 1) // 2),
                )
                total = yes + no
                if total < quorum:
                    verdict = f"❌ **Failed** — quorum not reached ({total}/{quorum} votes)"
                else:
                    verdict = f"❌ **Failed** ({yes}✅ {no}❌)"
        else:
            verdict = f"↩️ **{status.title()}**"
        suffix = f" — {note}" if note else ""
        await channel.send(f"**{kind} #{row['id']}** — {verdict}{suffix}")

    async def _advance_after_resolution(
        self, game_id: int, proposer_id: str, players: list[dict]
    ) -> None:
        # safe_next_player validates the rule's return value against the roster
        next_id = engine.safe_next_player(self.bot.rules, proposer_id, players)
        if next_id:
            await self.bot.db.set_current_turn(game_id, str(next_id))

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

        game = await self.bot.db.get_active_game()
        if game is None or game["phase"] != "playing":
            await interaction.followup.send(
                "❌ No game is currently being played.", ephemeral=True
            )
            return

        rules = self.bot.rules
        players = await self.bot.db.get_game_players(game["id"])
        proposer_id = str(interaction.user.id)

        if not await self.bot.db.is_player_in_game(game["id"], proposer_id):
            await interaction.followup.send(
                "❌ You're not in this game. Wait for the next one to `/join`.",
                ephemeral=True,
            )
            return

        allowed = engine.call_rule(
            rules, "can_propose",
            proposer_id, game["current_turn_player_id"], players,
            default=(proposer_id == game["current_turn_player_id"]),
        )
        if not allowed:
            current = game["current_turn_player_id"]
            await interaction.followup.send(
                f"❌ It isn't your turn. Current turn: <@{current}>",
                ephemeral=True,
            )
            return

        # Validate attachment (can happen outside the lock — pure local checks)
        if not patch.filename.endswith(".patch"):
            await interaction.followup.send("❌ Attachment must be a `.patch` file.", ephemeral=True)
            return
        if patch.size > MAX_PATCH_BYTES:
            await interaction.followup.send(
                f"❌ Patch too large ({patch.size} bytes, max {MAX_PATCH_BYTES}).",
                ephemeral=True,
            )
            return

        patch_bytes = await patch.read()
        try:
            patch_text = patch_bytes.decode("utf-8")
        except UnicodeDecodeError:
            await interaction.followup.send("❌ Patch must be valid UTF-8.", ephemeral=True)
            return

        # Lock from open-check through DB insert so two concurrent /propose
        # calls can't both pass the "no open proposal" gate and double-insert.
        async with self._propose_lock:
            allow_concurrent = engine.get_rule(rules, "ALLOW_CONCURRENT_PROPOSALS", False)
            if not allow_concurrent:
                open_p = await self.bot.db.get_open_proposal(game["id"])
                if open_p is not None:
                    await interaction.followup.send(
                        f"❌ Proposal #{open_p['id']} is still open. "
                        f"Use `/withdraw` or wait for it to resolve.",
                        ephemeral=True,
                    )
                    return

            valid, error, new_content, transmutations = engine.validate_patch(patch_text, self.bot.rules_path)
            if not valid:
                if len(error) > 1800:
                    error = error[:1800] + "\n…(truncated)"
                await interaction.followup.send(f"❌ **Invalid patch:**\n```\n{error}\n```", ephemeral=True)
                return

            duration_hours = engine.get_rule(rules, "PROPOSAL_DURATION_HOURS", 48)
            proposer_name = interaction.user.display_name
            proposal_id = await self.bot.db.create_proposal(
                game_id=game["id"],
                proposer_id=proposer_id,
                proposer_name=proposer_name,
                description=description,
                patch_text=patch_text,
                duration_hours=float(duration_hours),
                transmuted_names=transmutations,
            )

        question_text = engine.call_rule(
            rules, "get_poll_question", proposer_name, proposal_id,
            default=f"Proposal #{proposal_id} by {proposer_name} — accept this rule change?",
        )
        answers = engine.call_rule(rules, "get_poll_answers", default=["✅ Yes", "❌ No"])
        if not isinstance(answers, list) or len(answers) < 2:
            answers = ["✅ Yes", "❌ No"]

        poll_hours = max(1, min(168, int(duration_hours)))
        poll = discord.Poll(
            question=str(question_text)[:300],
            duration=datetime.timedelta(hours=poll_hours),
        )
        for label in answers[:10]:
            poll.add_answer(text=str(label)[:55])

        kind = "⚡ Transmutation" if transmutations else "📋 Proposal"
        transmute_note = ""
        if transmutations:
            transmute_note = (
                f"\n*Transmuting:* `{', '.join(transmutations)}` — "
                f"requires unanimous YES from all non-proposer players."
            )

        # Attach raw patch + would-be rules.py so voters can review/apply locally
        attachments = _proposal_files(proposal_id, patch_text, new_content)

        message = await interaction.followup.send(
            f"{kind} **#{proposal_id}:** {description}\n"
            f"*By {proposer_name} · poll open for {poll_hours}h*{transmute_note}",
            poll=poll,
            files=attachments,
        )
        await self.bot.db.set_proposal_poll(
            proposal_id, str(message.id), str(interaction.channel_id)
        )

    # ── /amend ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="amend", description="Replace the patch on your open proposal (resets votes).")
    @app_commands.describe(
        proposal_id="Your open proposal to amend.",
        description="New summary.",
        patch="New .patch file.",
    )
    async def amend(
        self,
        interaction: discord.Interaction,
        proposal_id: int,
        description: str,
        patch: discord.Attachment,
    ) -> None:
        await interaction.response.defer(thinking=True)
        row = await self.bot.db.get_proposal(proposal_id)
        if row is None or row["status"] != "pending":
            await interaction.followup.send(
                f"❌ Proposal #{proposal_id} not found or not open.", ephemeral=True
            )
            return

        rules = self.bot.rules
        allowed = engine.call_rule(
            rules, "can_amend",
            str(interaction.user.id), row["proposer_id"],
            default=(str(interaction.user.id) == row["proposer_id"]),
        )
        if not allowed:
            await interaction.followup.send("❌ You cannot amend this proposal.", ephemeral=True)
            return

        if not patch.filename.endswith(".patch") or patch.size > MAX_PATCH_BYTES:
            await interaction.followup.send("❌ Invalid patch file.", ephemeral=True)
            return
        try:
            patch_text = (await patch.read()).decode("utf-8")
        except UnicodeDecodeError:
            await interaction.followup.send("❌ Patch must be valid UTF-8.", ephemeral=True)
            return

        valid, error, new_content, transmutations = engine.validate_patch(patch_text, self.bot.rules_path)
        if not valid:
            if len(error) > 1800:
                error = error[:1800] + "\n…(truncated)"
            await interaction.followup.send(f"❌ **Invalid patch:**\n```\n{error}\n```", ephemeral=True)
            return

        # Hold the tally lock so the poll_checker can't tally this proposal
        # mid-swap (would resolve with phantom zero votes).
        async with self._tally_lock:
            # Re-fetch to confirm it's still pending after acquiring the lock
            row = await self.bot.db.get_proposal(proposal_id)
            if row is None or row["status"] != "pending":
                await interaction.followup.send(
                    f"❌ Proposal #{proposal_id} was resolved before amend completed.",
                    ephemeral=True,
                )
                return

            channel = self.bot.get_channel(int(row["poll_channel_id"]))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(row["poll_channel_id"]))
                except Exception:
                    await interaction.followup.send("❌ Cannot reach original poll channel.", ephemeral=True)
                    return

            # End the existing Discord poll so old votes don't carry over
            try:
                old_msg = await channel.fetch_message(int(row["poll_message_id"]))
                if old_msg.poll and not old_msg.poll.is_finalized():
                    await old_msg.poll.end()
            except discord.NotFound:
                pass
            except Exception:
                log.exception("Failed to end old poll for proposal #%d", proposal_id)

            # New poll duration = remaining time on the original (so amending
            # doesn't extend your voting window). Min 1 hour (Discord limit).
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            closes_at = datetime.datetime.fromisoformat(row["closes_at"])
            remaining_h = max(1, min(168, int((closes_at - now).total_seconds() // 3600) + 1))

            question_text = engine.call_rule(
                rules, "get_poll_question",
                row["proposer_name"], proposal_id,
                default=f"Proposal #{proposal_id} by {row['proposer_name']} — accept this rule change?",
            )
            answers = engine.call_rule(rules, "get_poll_answers", default=["✅ Yes", "❌ No"])
            if not isinstance(answers, list) or len(answers) < 2:
                answers = ["✅ Yes", "❌ No"]

            poll = discord.Poll(
                question=str(question_text)[:300],
                duration=datetime.timedelta(hours=remaining_h),
            )
            for label in answers[:10]:
                poll.add_answer(text=str(label)[:55])

            kind_change = ""
            was_transmute = bool(row["is_transmutation"])
            if transmutations and not was_transmute:
                kind_change = " ⚡ **now a transmutation**"
            elif was_transmute and not transmutations:
                kind_change = " (no longer a transmutation)"

            attachments = _proposal_files(proposal_id, patch_text, new_content)
            new_msg = await channel.send(
                f"✏️ **Proposal #{proposal_id} amended**{kind_change}\n"
                f"*New summary:* {description}\n"
                f"*Previous votes have been reset. Poll runs for {remaining_h}h.*",
                poll=poll,
                files=attachments,
            )

            await self.bot.db.update_proposal_patch(
                proposal_id, description, patch_text, transmuted_names=transmutations
            )
            await self.bot.db.set_proposal_poll(
                proposal_id, str(new_msg.id), str(new_msg.channel.id)
            )

        await interaction.followup.send(f"Proposal #{proposal_id} amended.", ephemeral=True)

    # ── /withdraw ──────────────────────────────────────────────────────────────

    @app_commands.command(name="withdraw", description="Withdraw your open proposal.")
    @app_commands.describe(proposal_id="Your open proposal to withdraw.")
    async def withdraw(self, interaction: discord.Interaction, proposal_id: int) -> None:
        await interaction.response.defer(thinking=True)
        row = await self.bot.db.get_proposal(proposal_id)
        if row is None or row["status"] != "pending":
            await interaction.followup.send(
                f"❌ Proposal #{proposal_id} not found or not open.", ephemeral=True
            )
            return

        rules = self.bot.rules
        is_proposer = str(interaction.user.id) == row["proposer_id"]
        allowed = is_proposer or _is_admin(interaction)
        if not allowed:
            # Defer to rules.can_withdraw for a chance to grant access more broadly
            allowed = engine.call_rule(
                rules, "can_withdraw",
                str(interaction.user.id), row["proposer_id"],
                default=False,
            )
        if not allowed:
            await interaction.followup.send("❌ Only the proposer or an admin can withdraw.", ephemeral=True)
            return

        await self.bot.db.resolve_proposal(proposal_id, "withdrawn", 0, 0)

        channel = self.bot.get_channel(int(row["poll_channel_id"]))
        if channel:
            try:
                msg = await channel.fetch_message(int(row["poll_message_id"]))
                if msg.poll:
                    await msg.poll.end()
            except discord.NotFound:
                pass
            await channel.send(f"↩️ **Proposal #{proposal_id}** withdrawn by <@{interaction.user.id}>.")

        advance = engine.call_rule(rules, "advance_turn_on_withdraw", default=False)
        if advance:
            players = await self.bot.db.get_game_players(row["game_id"])
            await self._advance_after_resolution(row["game_id"], row["proposer_id"], players)

        await interaction.followup.send(f"Proposal #{proposal_id} withdrawn.", ephemeral=True)

    # ── /tally ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="tally", description="Close your own poll early (proposer or admin only).")
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

        if str(interaction.user.id) != row["proposer_id"] and not _is_admin(interaction):
            await interaction.followup.send(
                "❌ Only the proposer or an admin can tally this proposal early.",
                ephemeral=True,
            )
            return

        result = await self._tally(row)
        if result:
            await interaction.followup.send(f"Tallied proposal #{proposal_id}: **{result}**.")
        else:
            await interaction.followup.send("❌ Could not tally — check logs.", ephemeral=True)

    # ── /proposals ─────────────────────────────────────────────────────────────

    @app_commands.command(name="proposals", description="List recent proposals.")
    @app_commands.describe(status="Filter by status: pending, passed, failed, withdrawn.")
    @app_commands.choices(status=[
        app_commands.Choice(name="pending",   value="pending"),
        app_commands.Choice(name="passed",    value="passed"),
        app_commands.Choice(name="failed",    value="failed"),
        app_commands.Choice(name="withdrawn", value="withdrawn"),
    ])
    async def proposals(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        game = await self.bot.db.get_active_game()
        filter_status = status.value if status else None
        rows = await self.bot.db.list_proposals(
            game_id=game["id"] if game else None,
            status=filter_status,
        )
        if not rows:
            await interaction.response.send_message("No proposals found.", ephemeral=True)
            return

        STATUS_EMOJI = {"pending": "🗳️", "passed": "✅", "failed": "❌", "withdrawn": "↩️"}
        lines = []
        for r in rows:
            emoji = STATUS_EMOJI.get(r["status"], "❓")
            transmute = " ⚡" if r["is_transmutation"] else ""
            closes = r["closes_at"][:16].replace("T", " ") + " UTC"
            lines.append(
                f"{emoji} **#{r['id']}**{transmute} {r['description'][:60]} "
                f"— *{r['proposer_name']}* · closes {closes}"
            )

        embed = discord.Embed(
            title=f"Game #{game['id']} Proposals" if game else "Proposals",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    # ── /proposal ──────────────────────────────────────────────────────────────

    @app_commands.command(name="proposal", description="View a single proposal's patch.")
    @app_commands.describe(proposal_id="The proposal number to view.")
    async def proposal(self, interaction: discord.Interaction, proposal_id: int) -> None:
        row = await self.bot.db.get_proposal(proposal_id)
        if row is None:
            await interaction.response.send_message(f"❌ Proposal #{proposal_id} not found.", ephemeral=True)
            return

        kind = "⚡ Transmutation" if row["is_transmutation"] else "Proposal"
        transmute_line = ""
        if row["is_transmutation"] and row["transmuted_names"]:
            transmute_line = f"*Transmuting:* `{row['transmuted_names']}`\n"
        header = (
            f"**{kind} #{row['id']}** ({row['status']})\n"
            f"*{row['proposer_name']}* — {row['description']}\n"
            f"{transmute_line}"
        )
        body = row["patch_text"]
        max_body = 1900 - len(header)
        if len(body) > max_body:
            body = body[:max_body] + "\n…(truncated — full patch attached)"

        patch_file = discord.File(
            io.BytesIO(row["patch_text"].encode("utf-8")),
            filename=f"proposal-{row['id']}.patch",
        )
        await interaction.response.send_message(
            f"{header}```diff\n{body}\n```",
            file=patch_file,
        )


def _proposal_files(proposal_id: int, patch_text: str, new_rules: str) -> list[discord.File]:
    """Build the two attachments shown alongside every poll: the raw .patch and
    the rules.py that would result if the proposal passes. Voters can download
    either to review locally before voting."""
    return [
        discord.File(
            io.BytesIO(patch_text.encode("utf-8")),
            filename=f"proposal-{proposal_id}.patch",
        ),
        discord.File(
            io.BytesIO(new_rules.encode("utf-8")),
            filename=f"rules-if-passed-{proposal_id}.py",
        ),
    ]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProposalsCog(bot))
