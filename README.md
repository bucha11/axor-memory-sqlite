# axor-memory-sqlite

[![CI](https://github.com/Bucha11/axor-memory-sqlite/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Bucha11/axor-memory-sqlite/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/axor-memory-sqlite?cacheSeconds=300)](https://pypi.org/project/axor-memory-sqlite/)
[![Python](https://img.shields.io/pypi/pyversions/axor-memory-sqlite?cacheSeconds=300)](https://pypi.org/project/axor-memory-sqlite/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**SQLite memory provider for [axor-core](https://github.com/Bucha11/axor-core).**

Persistent cross-session memory for governed agents. Zero extra dependencies — uses Python's built-in `sqlite3`.

---

## Installation

```bash
pip install axor-memory-sqlite
```

Requires `axor-core >= 0.1.0`.

---

## Quick Start

```python
import axor_claude
from axor_core import AgentDefinition, AgentDomain, FragmentValue, MemoryFragment
from axor_memory_sqlite import SQLiteMemoryProvider

provider = SQLiteMemoryProvider("~/.axor/memory.db")

session = axor_claude.make_session(
    api_key="sk-ant-...",
    agent_def=AgentDefinition(
        name="my-agent",
        domain=AgentDomain.CODING,
        personality="You are an expert Python engineer.",
        memory_namespaces=("my-agent",),   # loaded at every session start
    ),
    memory_provider=provider,
)

result = await session.run("refactor the auth module")

# save what you want to remember next time
await provider.save([
    MemoryFragment(
        namespace="my-agent",
        key="auth_module_status",
        content="Auth module refactored to use JWT. Entry point: auth/jwt.py.",
        value=FragmentValue.KNOWLEDGE,
    ),
])
```

---

## FragmentValue — what gets remembered how

Every `MemoryFragment` has a `value` that controls how the compressor treats it when it appears in `ContextView`:

| Value | Compressor behavior | Typical use |
|-------|--------------------|----|
| `PINNED` | Never touched — survives all turns | User preferences, system rules, agent personality |
| `KNOWLEDGE` | Dedup + error collapse only — no truncation | Project docs, domain context, API specs |
| `WORKING` | Normal compression pipeline | Task findings, recent tool results |
| `EPHEMERAL` | Aggressive compression — evicted first | Debug output, one-turn scratch |

Eviction priority: `EPHEMERAL` → `WORKING` → `KNOWLEDGE` → `PINNED` (never evicted).

---

## API

### `SQLiteMemoryProvider(db_path)`

```python
from axor_memory_sqlite import SQLiteMemoryProvider

provider = SQLiteMemoryProvider("~/.axor/memory.db")  # persistent
provider = SQLiteMemoryProvider(":memory:")            # in-memory, tests only
```

All methods are async. I/O runs in a thread pool — async callers are never blocked.

### `save(fragments)`

Upsert by `(namespace, key)` — existing fragments are overwritten:

```python
await provider.save([
    MemoryFragment(
        namespace="my-agent",
        key="project_stack",
        content="FastAPI + async SQLAlchemy + PostgreSQL",
        value=FragmentValue.PINNED,
        tags=["stack", "tech"],
    ),
])
```

### `load(query)`

```python
from axor_core import MemoryQuery, FragmentValue

# load all from namespace, pinned first
fragments = await provider.load(MemoryQuery(
    namespaces=("my-agent",),
    max_results=20,
))

# filter by value
pinned = await provider.load(MemoryQuery(
    namespaces=("my-agent",),
    values=(FragmentValue.PINNED, FragmentValue.KNOWLEDGE),
    max_results=10,
))
```

Results are ordered by priority (`PINNED` first) then by `accessed_at` descending.

### `delete(namespace, keys)`

```python
n = await provider.delete("my-agent", ["stale_key_1", "stale_key_2"])
print(f"deleted {n} fragments")
```

### `evict(namespace, values, max_age_seconds)`

Remove stale fragments by value and/or age:

```python
# evict all ephemeral fragments
await provider.evict("my-agent", values=(FragmentValue.EPHEMERAL,))

# evict working fragments older than 24 hours
await provider.evict(
    "my-agent",
    values=(FragmentValue.WORKING,),
    max_age_seconds=86400,
)
```

### `namespaces()`

```python
ns = await provider.namespaces()
# ["my-agent", "shared", "project-x"]
```

### `close()`

```python
await provider.close()
```

Call on shutdown. The provider can be used as an async context manager in tests:

```python
async with SQLiteMemoryProvider(":memory:") as provider:
    await provider.save([...])
```

---

## Namespaces

Namespaces are logical groupings — they do not require explicit creation. A namespace exists as soon as you save a fragment with that name.

Recommended pattern:

| Namespace | Content |
|-----------|---------|
| `{agent-name}` | Agent-specific memory — not shared |
| `shared` | Shared across all agents in a project |
| `project:{name}` | Project-specific facts |
| `user:{id}` | Per-user preferences |

```python
# agent reads from its own namespace + shared
agent = AgentDefinition(
    name="billing-agent",
    memory_namespaces=("billing-agent", "shared"),
)
```

---

## Schema

```sql
CREATE TABLE memory_fragments (
    namespace    TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    value        TEXT    NOT NULL DEFAULT 'working',
    token_count  INTEGER NOT NULL DEFAULT 0,
    tags         TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    created_at   TEXT    NOT NULL,
    accessed_at  TEXT    NOT NULL,
    metadata     TEXT    NOT NULL DEFAULT '{}',   -- JSON object
    PRIMARY KEY (namespace, key)
);
```

The database file is a standard SQLite file — readable with any SQLite tool.

---

## Testing

Use `:memory:` for tests — no file created, no cleanup needed:

```python
import pytest
from axor_memory_sqlite import SQLiteMemoryProvider
from axor_core import MemoryFragment, FragmentValue, MemoryQuery

@pytest.mark.asyncio
async def test_memory():
    p = SQLiteMemoryProvider(":memory:")

    await p.save([
        MemoryFragment(namespace="test", key="k1",
                       content="hello", value=FragmentValue.WORKING),
    ])
    results = await p.load(MemoryQuery(namespaces=("test",), max_results=5))
    assert len(results) == 1
    assert results[0].content == "hello"

    await p.close()
```

---

## Requirements

- Python 3.11+
- `axor-core >= 0.1.0`
- No extra dependencies — uses stdlib `sqlite3` + `asyncio`

---

## License

MIT
