"""
db.py — thin async database abstraction.

In production: uses asyncpg pool (Postgres). DATABASE_URL comes from
a Docker Swarm secret mounted by provision-service.sh.

In local dev (no DATABASE_URL, not in a container): falls back to SQLite
via aiosqlite. SQL files are written in Postgres dialect; a light adapter
converts them for SQLite automatically.
"""
import os
import re
import sqlite3
from pathlib import Path

import asyncpg
import aiosqlite

_IN_CONTAINER = os.path.exists("/.dockerenv")


def _pg_to_sqlite(sql: str) -> str:
    """Convert Postgres-dialect SQL to SQLite-compatible SQL."""
    sql = re.sub(r"\$\d+", "?", sql)
    sql = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bSERIAL\b", "INTEGER", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bnow\(\)", "datetime('now')", sql, flags=re.IGNORECASE)
    # SQLite requires parens around function-call column defaults
    sql = re.sub(
        r"DEFAULT\s+datetime\('now'\)",
        "DEFAULT (datetime('now'))",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _to_dict(row) -> dict:
    """Convert asyncpg Record or sqlite3.Row to a plain dict."""
    return dict(zip(row.keys(), row))


class DB:
    """Thin wrapper presenting a unified interface over asyncpg or aiosqlite."""

    def __init__(self):
        self._pg: asyncpg.Pool | None = None
        self._sq: aiosqlite.Connection | None = None

    @classmethod
    async def from_postgres(cls, url: str) -> "DB":
        db = cls()
        db._pg = await asyncpg.create_pool(url, min_size=2, max_size=10)
        return db

    @classmethod
    async def from_sqlite(cls, path: str = "dev.db") -> "DB":
        db = cls()
        db._sq = await aiosqlite.connect(path)
        db._sq.row_factory = sqlite3.Row
        return db

    @property
    def connected(self) -> bool:
        return self._pg is not None or self._sq is not None

    @property
    def backend(self) -> str:
        if self._pg:
            return "postgres"
        if self._sq:
            return "sqlite"
        return "none"

    async def close(self) -> None:
        if self._pg:
            await self._pg.close()
        if self._sq:
            await self._sq.close()

    async def fetch(self, sql: str, *args) -> list[dict]:
        if self._pg:
            rows = await self._pg.fetch(sql, *args)
            return [_to_dict(r) for r in rows]
        async with self._sq.execute(_pg_to_sqlite(sql), args) as cur:
            return [_to_dict(r) for r in await cur.fetchall()]

    async def fetchrow(self, sql: str, *args) -> dict | None:
        if self._pg:
            row = await self._pg.fetchrow(sql, *args)
            return _to_dict(row) if row else None
        async with self._sq.execute(_pg_to_sqlite(sql), args) as cur:
            row = await cur.fetchone()
            return _to_dict(row) if row else None

    async def execute(self, sql: str, *args) -> str:
        """Run a DML statement. Returns a Postgres-style command tag e.g. 'DELETE 1'."""
        if self._pg:
            return await self._pg.execute(sql, *args)
        cur = await self._sq.execute(_pg_to_sqlite(sql), args)
        await self._sq.commit()
        cmd = sql.strip().split()[0].upper()
        return f"{cmd} {cur.rowcount}"

    async def run_migrations(self, sql_dir: str = "sql") -> None:
        """Execute all *.sql files in sql_dir in alphabetical order."""
        for path in sorted(Path(sql_dir).glob("*.sql")):
            sql = path.read_text()
            if self._pg:
                await self._pg.execute(sql)
            else:
                await self._sq.executescript(_pg_to_sqlite(sql))
                await self._sq.commit()


async def init_db() -> DB:
    """
    Connect to the database and return a ready DB instance.

    Resolution order:
      1. DATABASE_URL secret/env → Postgres
      2. No URL + not in container → SQLite dev.db (local dev)
      3. No URL + in container → returns unconnected DB (warning shown in UI)
    """
    from platform_auth import read_secret
    url = read_secret("DATABASE_URL")
    if url:
        return await DB.from_postgres(url)
    if not _IN_CONTAINER:
        return await DB.from_sqlite("dev.db")
    return DB()
