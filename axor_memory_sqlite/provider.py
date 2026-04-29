from __future__ import annotations

"""
SQLite-backed MemoryProvider for axor-core.

Zero external dependencies — uses Python's built-in sqlite3.
Thread-safe via asyncio.Lock + thread pool execution.

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
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from axor_core.contracts.memory import (
    MemoryFragment,
    MemoryProvider,
    MemoryQuery,
    FragmentValue,
)

logger = logging.getLogger(__name__)

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
CREATE INDEX IF NOT EXISTS idx_ns_value  ON memory_fragments(namespace, value);
"""

# Bumped per migration. Stored in PRAGMA user_version. Future schema
# changes should: read user_version → if behind, run incremental DDL → bump.
_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_fragment(row: tuple) -> MemoryFragment:
    ns, key, content, value, token_count, tags_json, created_at, accessed_at, meta_json = row
    try:
        created = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        created = datetime.now(timezone.utc)
    try:
        accessed = datetime.fromisoformat(accessed_at)
    except (ValueError, TypeError):
        accessed = datetime.now(timezone.utc)
    return MemoryFragment(
        namespace=ns,
        key=key,
        content=content,
        value=FragmentValue(value),
        token_count=token_count,
        tags=json.loads(tags_json),
        created_at=created,
        accessed_at=accessed,
        metadata=json.loads(meta_json),
    )


class SQLiteMemoryProvider(MemoryProvider):
    """
    SQLite-backed MemoryProvider.

    All I/O runs in a thread pool via asyncio.to_thread()
    so async callers are never blocked.

    Usage::

        provider = SQLiteMemoryProvider("~/.axor/memory.db")
        session = GovernedSession(..., memory_provider=provider)

        # or as async context manager
        async with SQLiteMemoryProvider("~/.axor/memory.db") as provider:
            ...
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(Path(db_path).expanduser()) if db_path != ":memory:" else ":memory:"
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.Lock()  # protects _open() from thread pool

    def _open(self) -> sqlite3.Connection:
        with self._conn_lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                # WAL mode: writers don't block readers — important for high-
                # frequency working-memory writes during agent runs. Skip on
                # ":memory:" (WAL is a no-op there) to keep test paths fast.
                if self._db_path != ":memory:":
                    try:
                        self._conn.execute("PRAGMA journal_mode=WAL")
                        # Reasonable durability/throughput tradeoff for a memory store.
                        self._conn.execute("PRAGMA synchronous=NORMAL")
                    except sqlite3.OperationalError:
                        # Some FS (network mounts) reject WAL — fall back silently.
                        pass
                self._conn.executescript(_SCHEMA)
                # Schema versioning: read PRAGMA user_version, run migrations
                # if behind, then bump. Today the schema is at v1 and there
                # are no migrations; this is the hook for future ones.
                cur = self._conn.execute("PRAGMA user_version")
                current_version = int((cur.fetchone() or [0])[0])
                if current_version < _SCHEMA_VERSION:
                    # No migrations to run for v1 → just stamp the version.
                    self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                self._conn.commit()
            return self._conn

    async def _run(self, fn):
        """Run a blocking DB call in thread pool."""
        return await asyncio.to_thread(fn)

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self):
        await self._run(self._open)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    # ── MemoryProvider interface ──────────────────────────────────────────────

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
            logger.exception("memory load failed")
            return []

    async def save(self, fragments: list[MemoryFragment]) -> None:
        if not fragments:
            return

        def _save():
            conn = self._open()
            now = _now()
            rows = []
            for f in fragments:
                try:
                    tags_json = json.dumps(f.tags)
                except (TypeError, ValueError) as e:
                    logger.warning("non-serializable tags for %s:%s: %s", f.namespace, f.key, e)
                    tags_json = "[]"
                try:
                    meta_json = json.dumps(f.metadata)
                except (TypeError, ValueError) as e:
                    logger.warning("non-serializable metadata for %s:%s: %s", f.namespace, f.key, e)
                    meta_json = "{}"
                rows.append((
                    f.namespace,
                    f.key,
                    f.content,
                    f.value.value,
                    f.token_count if f.token_count is not None and f.token_count > 0 else len(f.content) // 4,
                    tags_json,
                    f.created_at.isoformat() if f.created_at else now,
                    now,
                    meta_json,
                ))
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
            logger.exception("memory save failed")
            raise

    async def delete(self, namespace: str, keys: list[str]) -> int:
        if not keys:
            return 0

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
            logger.exception("memory delete failed")
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
                    f"accessed_at < datetime('now', '-{int(max_age_seconds)} seconds')"
                )

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
            logger.exception("memory evict failed")
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
            logger.exception("memory namespaces failed")
            return []

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
