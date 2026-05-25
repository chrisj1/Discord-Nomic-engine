"""
SQLite-backed game state via aiosqlite.
"""

import datetime
import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

DB_PATH = Path("/app/data/game.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proposer_id     TEXT    NOT NULL,
    proposer_name   TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    patch_text      TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    poll_message_id TEXT,
    poll_channel_id TEXT,
    yes_votes       INTEGER NOT NULL DEFAULT 0,
    no_votes        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    closes_at       TEXT    NOT NULL,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS players (
    discord_id   TEXT PRIMARY KEY,
    discord_name TEXT NOT NULL,
    points       INTEGER NOT NULL DEFAULT 0,
    joined_at    TEXT NOT NULL
);
"""


class Database:
    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        log.info("Database ready at %s", DB_PATH)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Proposals ─────────────────────────────────────────────────────────────

    async def create_proposal(
        self,
        proposer_id: str,
        proposer_name: str,
        description: str,
        patch_text: str,
        duration_hours: float,
    ) -> int:
        now = datetime.datetime.utcnow()
        closes = now + datetime.timedelta(hours=duration_hours)
        async with self._db.execute(
            """
            INSERT INTO proposals
                (proposer_id, proposer_name, description, patch_text, created_at, closes_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (proposer_id, proposer_name, description, patch_text,
             now.isoformat(), closes.isoformat()),
        ) as cur:
            proposal_id = cur.lastrowid
        await self._db.commit()
        return proposal_id

    async def set_proposal_poll(
        self, proposal_id: int, message_id: str, channel_id: str
    ) -> None:
        await self._db.execute(
            "UPDATE proposals SET poll_message_id=?, poll_channel_id=? WHERE id=?",
            (message_id, channel_id, proposal_id),
        )
        await self._db.commit()

    async def get_proposal(self, proposal_id: int) -> aiosqlite.Row | None:
        async with self._db.execute(
            "SELECT * FROM proposals WHERE id=?", (proposal_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_pending_proposals(self) -> list[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM proposals WHERE status='pending' AND poll_message_id IS NOT NULL"
        ) as cur:
            return await cur.fetchall()

    async def get_expired_proposals(self) -> list[aiosqlite.Row]:
        now = datetime.datetime.utcnow().isoformat()
        async with self._db.execute(
            "SELECT * FROM proposals WHERE status='pending' AND poll_message_id IS NOT NULL AND closes_at <= ?",
            (now,),
        ) as cur:
            return await cur.fetchall()

    async def resolve_proposal(
        self,
        proposal_id: int,
        status: str,   # 'passed' | 'failed'
        yes_votes: int,
        no_votes: int,
    ) -> None:
        now = datetime.datetime.utcnow().isoformat()
        await self._db.execute(
            """
            UPDATE proposals
               SET status=?, yes_votes=?, no_votes=?, resolved_at=?
             WHERE id=?
            """,
            (status, yes_votes, no_votes, now, proposal_id),
        )
        await self._db.commit()

    async def list_proposals(self, status: str | None = None) -> list[aiosqlite.Row]:
        if status:
            async with self._db.execute(
                "SELECT * FROM proposals WHERE status=? ORDER BY id DESC LIMIT 20",
                (status,),
            ) as cur:
                return await cur.fetchall()
        async with self._db.execute(
            "SELECT * FROM proposals ORDER BY id DESC LIMIT 20"
        ) as cur:
            return await cur.fetchall()

    # ── Players ───────────────────────────────────────────────────────────────

    async def ensure_player(self, discord_id: str, discord_name: str) -> bool:
        """Insert player if not present. Returns True if newly created."""
        async with self._db.execute(
            "SELECT discord_id FROM players WHERE discord_id=?", (discord_id,)
        ) as cur:
            if await cur.fetchone():
                return False
        await self._db.execute(
            "INSERT INTO players (discord_id, discord_name, joined_at) VALUES (?, ?, ?)",
            (discord_id, discord_name, datetime.datetime.utcnow().isoformat()),
        )
        await self._db.commit()
        return True

    async def update_name(self, discord_id: str, discord_name: str) -> None:
        await self._db.execute(
            "UPDATE players SET discord_name=? WHERE discord_id=?",
            (discord_name, discord_id),
        )
        await self._db.commit()

    async def award_points(self, discord_id: str, discord_name: str, points: int) -> None:
        await self.ensure_player(discord_id, discord_name)
        await self._db.execute(
            "UPDATE players SET points = points + ?, discord_name=? WHERE discord_id=?",
            (points, discord_name, discord_id),
        )
        await self._db.commit()

    async def get_leaderboard(self, limit: int = 10) -> list[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM players ORDER BY points DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()

    async def get_player(self, discord_id: str) -> aiosqlite.Row | None:
        async with self._db.execute(
            "SELECT * FROM players WHERE discord_id=?", (discord_id,)
        ) as cur:
            return await cur.fetchone()
