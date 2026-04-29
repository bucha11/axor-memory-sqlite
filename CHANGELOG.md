# Changelog

## 0.2.0 — 2026-04-29

### Added
- WAL journal mode (`PRAGMA journal_mode=WAL`) plus
  `PRAGMA synchronous=NORMAL` on non-`:memory:` databases. Writers no
  longer block readers — important for high-frequency working-memory
  writes during agent runs. Falls back silently when the filesystem
  rejects WAL (e.g., some network mounts).
- Schema-versioning hook: `PRAGMA user_version` + module-level
  `_SCHEMA_VERSION = 1`. No migrations to run today; this is the seam
  for future incremental DDL bumps.

### Constraints
- Pin bump: `axor-core>=0.4.0,<0.5` (was `>=0.1.0`).

## 0.1.0 — 2026-04-14

Initial release.

### Added
- `SQLiteMemoryProvider` implementing the `MemoryProvider` ABC from axor-core.
- All DB operations run inside `asyncio.to_thread()` so the event loop is
  never blocked.
- `asyncio.Lock` for async callers plus `threading.Lock` around `_open()`
  for thread safety.
- Async context-manager support
  (`async with SQLiteMemoryProvider(...) as provider:`).
- Schema with `PRIMARY KEY (namespace, key)` and indexes on `namespace`,
  `value`, `accessed_at`, plus a composite `(namespace, value)` for the
  common query pattern.
- Fragment priority ordering enforced via `CASE` in `ORDER BY`:
  PINNED(0) → KNOWLEDGE(1) → WORKING(2) → EPHEMERAL(3).
- `token_count` semantics: explicit `>0` value preserved; `0` or `None`
  estimated as `len(content) // 4`.

### Error handling
- `save()` logs and **re-raises** on error — data loss must be visible.
- `load()`, `delete()`, `evict()`, `namespaces()` log and return empty —
  read failures are non-fatal.
- JSON serialization errors for `tags`/`metadata` are logged with a warning
  and defaulted to `[]` / `{}`.

### Security
- Parameterized queries everywhere — no SQL injection surface.
