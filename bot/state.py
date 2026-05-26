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
CREATE TABLE IF NOT EXISTS games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phase           TEXT    NOT NULL DEFAULT 'join',  -- join | playing | finished
    current_turn_player_id TEXT,
    winner_id       TEXT,
    created_at      TEXT    NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS players (
    discord_id   TEXT PRIMARY KEY,
    discord_name TEXT NOT NULL,
    first_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS game_players (
    game_id      INTEGER NOT NULL,
    discord_id   TEXT    NOT NULL,
    points       INTEGER NOT NULL DEFAULT 0,
    joined_at    TEXT    NOT NULL,
    PRIMARY KEY (game_id, discord_id)
);

CREATE TABLE IF NOT EXISTS proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id          INTEGER NOT NULL,
    proposer_id      TEXT    NOT NULL,
    proposer_name    TEXT    NOT NULL,
    description      TEXT    NOT NULL,
    patch_text       TEXT    NOT NULL,
    is_transmutation INTEGER NOT NULL DEFAULT 0,         -- 0/1; if 1, requires unanimous YES
    transmuted_names TEXT    NOT NULL DEFAULT '',        -- comma-separated names being transmuted
    status           TEXT    NOT NULL DEFAULT 'pending', -- pending | passed | failed | withdrawn
    poll_message_id  TEXT,
    poll_channel_id  TEXT,
    yes_votes        INTEGER NOT NULL DEFAULT 0,
    no_votes         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    closes_at        TEXT    NOT NULL,
    resolved_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_game_status ON proposals(game_id, status);
"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


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

    # ── Games ─────────────────────────────────────────────────────────────────

    async def create_game(self) -> int:
        now = _utcnow().isoformat()
        async with self._db.execute(
            "INSERT INTO games (phase, created_at) VALUES ('join', ?)",
            (now,),
        ) as cur:
            game_id = cur.lastrowid
        await self._db.commit()
        return game_id

    async def get_active_game(self) -> aiosqlite.Row | None:
        """Return the game in 'join' or 'playing' phase, if any. At most one."""
        async with self._db.execute(
            "SELECT * FROM games WHERE phase IN ('join', 'playing') ORDER BY id DESC LIMIT 1"
        ) as cur:
            return await cur.fetchone()

    async def get_game(self, game_id: int) -> aiosqlite.Row | None:
        async with self._db.execute(
            "SELECT * FROM games WHERE id=?", (game_id,)
        ) as cur:
            return await cur.fetchone()

    async def start_game(self, game_id: int, first_player_id: str) -> None:
        now = _utcnow().isoformat()
        await self._db.execute(
            "UPDATE games SET phase='playing', started_at=?, current_turn_player_id=? WHERE id=?",
            (now, first_player_id, game_id),
        )
        await self._db.commit()

    async def set_current_turn(self, game_id: int, player_id: str | None) -> None:
        await self._db.execute(
            "UPDATE games SET current_turn_player_id=? WHERE id=?",
            (player_id, game_id),
        )
        await self._db.commit()

    async def finish_game(self, game_id: int, winner_id: str | None) -> None:
        now = _utcnow().isoformat()
        await self._db.execute(
            "UPDATE games SET phase='finished', winner_id=?, finished_at=? WHERE id=?",
            (winner_id, now, game_id),
        )
        await self._db.commit()

    # ── Players & game roster ─────────────────────────────────────────────────

    async def ensure_player(self, discord_id: str, discord_name: str) -> None:
        """Insert player identity row if not present (no game association)."""
        now = _utcnow().isoformat()
        await self._db.execute(
            """
            INSERT INTO players (discord_id, discord_name, first_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET discord_name=excluded.discord_name
            """,
            (discord_id, discord_name, now),
        )
        await self._db.commit()

    async def add_player_to_game(
        self, game_id: int, discord_id: str, discord_name: str
    ) -> bool:
        """Add player to a game's roster. Returns True if newly added."""
        await self.ensure_player(discord_id, discord_name)
        async with self._db.execute(
            "SELECT 1 FROM game_players WHERE game_id=? AND discord_id=?",
            (game_id, discord_id),
        ) as cur:
            if await cur.fetchone():
                return False
        now = _utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO game_players (game_id, discord_id, joined_at) VALUES (?, ?, ?)",
            (game_id, discord_id, now),
        )
        await self._db.commit()
        return True

    async def is_player_in_game(self, game_id: int, discord_id: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM game_players WHERE game_id=? AND discord_id=?",
            (game_id, discord_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_game_players(self, game_id: int) -> list[dict]:
        """Return roster ordered by join time (oldest first) as plain dicts —
        the shape passed to rules.py callbacks."""
        async with self._db.execute(
            """
            SELECT gp.discord_id, p.discord_name AS name, gp.points, gp.joined_at
              FROM game_players gp
              JOIN players p ON p.discord_id = gp.discord_id
             WHERE gp.game_id=?
             ORDER BY gp.joined_at ASC
            """,
            (game_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_game_player(self, game_id: int, discord_id: str) -> aiosqlite.Row | None:
        async with self._db.execute(
            """
            SELECT gp.discord_id, p.discord_name, gp.points, gp.joined_at
              FROM game_players gp
              JOIN players p ON p.discord_id = gp.discord_id
             WHERE gp.game_id=? AND gp.discord_id=?
            """,
            (game_id, discord_id),
        ) as cur:
            return await cur.fetchone()

    async def award_points_in_game(
        self, game_id: int, discord_id: str, points: int
    ) -> None:
        await self._db.execute(
            "UPDATE game_players SET points = points + ? WHERE game_id=? AND discord_id=?",
            (points, game_id, discord_id),
        )
        await self._db.commit()

    async def get_leaderboard(self, game_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        async with self._db.execute(
            """
            SELECT gp.discord_id, p.discord_name, gp.points
              FROM game_players gp
              JOIN players p ON p.discord_id = gp.discord_id
             WHERE gp.game_id=?
             ORDER BY gp.points DESC, gp.joined_at ASC
             LIMIT ?
            """,
            (game_id, limit),
        ) as cur:
            return await cur.fetchall()

    # ── Proposals ─────────────────────────────────────────────────────────────

    async def create_proposal(
        self,
        game_id: int,
        proposer_id: str,
        proposer_name: str,
        description: str,
        patch_text: str,
        duration_hours: float,
        transmuted_names: list[str] | None = None,
    ) -> int:
        now = _utcnow()
        closes = now + datetime.timedelta(hours=duration_hours)
        names = transmuted_names or []
        async with self._db.execute(
            """
            INSERT INTO proposals
                (game_id, proposer_id, proposer_name, description, patch_text,
                 is_transmutation, transmuted_names, created_at, closes_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (game_id, proposer_id, proposer_name, description, patch_text,
             1 if names else 0, ",".join(names),
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

    async def get_proposal_by_poll_message(self, message_id: str) -> aiosqlite.Row | None:
        async with self._db.execute(
            "SELECT * FROM proposals WHERE poll_message_id=?", (str(message_id),)
        ) as cur:
            return await cur.fetchone()

    async def get_pending_in_game(self, game_id: int) -> list[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM proposals WHERE game_id=? AND status='pending'",
            (game_id,),
        ) as cur:
            return await cur.fetchall()

    async def get_open_proposal(self, game_id: int) -> aiosqlite.Row | None:
        """Returns the currently active proposal (pending) for this game, if any."""
        async with self._db.execute(
            "SELECT * FROM proposals WHERE game_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (game_id,),
        ) as cur:
            return await cur.fetchone()

    async def get_expired_proposals(self) -> list[aiosqlite.Row]:
        """Return pending proposals from active games whose poll has closed."""
        now = _utcnow().isoformat()
        async with self._db.execute(
            """
            SELECT p.* FROM proposals p
              JOIN games g ON g.id = p.game_id
             WHERE p.status='pending'
               AND p.poll_message_id IS NOT NULL
               AND p.closes_at <= ?
               AND g.phase='playing'
            """,
            (now,),
        ) as cur:
            return await cur.fetchall()

    async def resolve_proposal(
        self,
        proposal_id: int,
        status: str,   # 'passed' | 'failed' | 'withdrawn'
        yes_votes: int,
        no_votes: int,
    ) -> None:
        now = _utcnow().isoformat()
        await self._db.execute(
            """
            UPDATE proposals
               SET status=?, yes_votes=?, no_votes=?, resolved_at=?
             WHERE id=?
            """,
            (status, yes_votes, no_votes, now, proposal_id),
        )
        await self._db.commit()

    async def update_proposal_patch(
        self,
        proposal_id: int,
        description: str,
        patch_text: str,
        transmuted_names: list[str] | None = None,
    ) -> None:
        names = transmuted_names or []
        await self._db.execute(
            """
            UPDATE proposals
               SET description=?, patch_text=?, is_transmutation=?, transmuted_names=?
             WHERE id=?
            """,
            (description, patch_text, 1 if names else 0, ",".join(names), proposal_id),
        )
        await self._db.commit()

    async def list_proposals(
        self, game_id: int | None = None, status: str | None = None
    ) -> list[aiosqlite.Row]:
        clauses, params = [], []
        if game_id is not None:
            clauses.append("game_id=?")
            params.append(game_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._db.execute(
            f"SELECT * FROM proposals {where} ORDER BY id DESC LIMIT 20",
            params,
        ) as cur:
            return await cur.fetchall()
