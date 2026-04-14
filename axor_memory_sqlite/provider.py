from __future__ import annotations

"""
SQLite-backed MemoryProvider for axor-core.

Zero external dependencies — uses Python's built-in sqlite3.
Thread-safe via asyncio.Lock + aiosqlite pattern (runs in thread pool).

Schema:

    CREATE TABLE memory_fragments (
        namespace   TEXT NOT NULL,
        key         TEXT NOT NULL,
        content     TEXT NOT NULL,
        value       TEXT NOT NULL DEFAULT 'working',
        token_count INTEGER NOT NULL DEFAULT 0,
        tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array
        created_at  TEXT NOT NULL,
        accessed_at TEXT NOT NULL,
        metadata    TEXT NOT NULL DEFAULT '{}',   -- JSON object
        PRIMARY KEY (namespace, key)
    );
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from axor_core.contracts.memory import (
    MemoryFragment,
    MemoryProvider,
    MemoryQuery,
    FragmentValue,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_fragments (
    namespace    TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    value        TEXT    NOT NULL DEFAULT 'working',
    token_count  INTEGER NOT NULL DEFAULT 0,
    tags         TEXT    NOT NULL DEFAULT '[]',
    created_at   TEXT    NOT NULL,
    accessed_at  TEXT    NOT NULL,
    metadata     TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_namespace ON memory_fragments(namespace);
CREATE INDEX IF NOT EXISTS idx_value     ON memory_fragments(value);
CREATE INDEX IF NOT EXISTS idx_accessed  ON memory_fragments(accessed_at);
"""

_NOW = lambda: datetime.now(timezone.utc).isoformat()


def _row_to_fragment(row: tuple) -> MemoryFragment:
    ns, key, content, value, token_count, tags_json, created_at, accessed_at, meta_json = row
    return MemoryFragment(
        namespace=ns,
        key=key,
        content=content,
        value=FragmentValue(value),
        token_count=token_count,
        tags=json.loads(tags_json),
        created_at=datetime.fromisoformat(created_at),
        accessed_at=datetime.fromisoformat(accessed_at),
        metadata=json.loads(meta_json),
    )


class SQLiteMemoryProvider(MemoryProvider):
    """
    SQLite-backed MemoryProvider.

    All I/O runs in a thread pool via asyncio.to_thread()
    so async callers are never blocked.

    Usage:

        from axor_memory_sqlite import SQLiteMemoryProvider
        from axor_core import GovernedSession, AgentDefinition

        provider = SQLiteMemoryProvider("~/.axor/memory.db")

        session = GovernedSession(
            executor=...,
            capability_executor=...,
            agent_def=AgentDefinition(
                name="my-agent",
                memory_namespaces=("my-agent", "shared"),
            ),
            memory_provider=provider,
        )

        # save memory after a session
        await session.save_memory(
            key="last_project",
            content="Working on axor federation support",
            value=FragmentValue.WORKING,
        )
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(Path(db_path).expanduser()) if db_path != ":memory:" else ":memory:"
        self._lock    = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    def _open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    async def _run(self, fn):
        """Run a blocking DB call in thread pool."""
        return await asyncio.to_thread(fn)

    # ── MemoryProvider interface ────────────────────────────────────────────────

    async def load(self, query: MemoryQuery) -> list[MemoryFragment]:
        def _load():
            conn = self._open()
            parts: list[str] = []
            params: list[Any] = []

            if query.namespaces:
                placeholders = ",".join("?" * len(query.namespaces))
                parts.append(f"namespace IN ({placeholders})")
                params.extend(query.namespaces)

            if query.values:
                placeholders = ",".join("?" * len(query.values))
                parts.append(f"value IN ({placeholders})")
                params.extend(v.value for v in query.values)

            where = ("WHERE " + " AND ".join(parts)) if parts else ""
            sql = f"""
                SELECT namespace, key, content, value, token_count, tags,
                       created_at, accessed_at, metadata
                FROM memory_fragments
                {where}
                ORDER BY
                    CASE value
                        WHEN 'pinned'    THEN 0
                        WHEN 'knowledge' THEN 1
                        WHEN 'working'   THEN 2
                        WHEN 'ephemeral' THEN 3
                    END,
                    accessed_at DESC
                LIMIT ?
            """
            params.append(query.max_results)
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_fragment(tuple(r)) for r in rows]

        try:
            async with self._lock:
                return await self._run(_load)
        except Exception:
            return []

    async def save(self, fragments: list[MemoryFragment]) -> None:
        def _save():
            conn = self._open()
            now  = _NOW()
            rows = [
                (
                    f.namespace,
                    f.key,
                    f.content,
                    f.value.value,
                    f.token_count or len(f.content) // 4,
                    json.dumps(f.tags),
                    f.created_at.isoformat() if f.created_at else now,
                    now,
                    json.dumps(f.metadata),
                )
                for f in fragments
            ]
            conn.executemany("""
                INSERT INTO memory_fragments
                    (namespace, key, content, value, token_count, tags,
                     created_at, accessed_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    content     = excluded.content,
                    value       = excluded.value,
                    token_count = excluded.token_count,
                    tags        = excluded.tags,
                    accessed_at = excluded.accessed_at,
                    metadata    = excluded.metadata
            """, rows)
            conn.commit()

        try:
            async with self._lock:
                await self._run(_save)
        except Exception:
            pass

    async def delete(self, namespace: str, keys: list[str]) -> int:
        def _delete():
            conn = self._open()
            placeholders = ",".join("?" * len(keys))
            cur = conn.execute(
                f"DELETE FROM memory_fragments WHERE namespace=? AND key IN ({placeholders})",
                [namespace, *keys],
            )
            conn.commit()
            return cur.rowcount

        try:
            async with self._lock:
                return await self._run(_delete)
        except Exception:
            return 0

    async def evict(
        self,
        namespace: str,
        values: tuple[FragmentValue, ...] = (FragmentValue.EPHEMERAL,),
        max_age_seconds: int | None = None,
    ) -> int:
        def _evict():
            conn = self._open()
            parts: list[str] = ["namespace = ?"]
            params: list[Any] = [namespace]

            if values:
                placeholders = ",".join("?" * len(values))
                parts.append(f"value IN ({placeholders})")
                params.extend(v.value for v in values)

            if max_age_seconds is not None:
                parts.append(
                    "accessed_at < datetime('now', ?)"
                )
                params.append(f"-{max_age_seconds} seconds")

            where = " AND ".join(parts)
            cur = conn.execute(
                f"DELETE FROM memory_fragments WHERE {where}",
                params,
            )
            conn.commit()
            return cur.rowcount

        try:
            async with self._lock:
                return await self._run(_evict)
        except Exception:
            return 0

    async def namespaces(self) -> list[str]:
        def _ns():
            conn = self._open()
            rows = conn.execute(
                "SELECT DISTINCT namespace FROM memory_fragments ORDER BY namespace"
            ).fetchall()
            return [r[0] for r in rows]

        try:
            async with self._lock:
                return await self._run(_ns)
        except Exception:
            return []

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
