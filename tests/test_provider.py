from __future__ import annotations

from datetime import datetime, timezone

import pytest

from axor_core.contracts.memory import FragmentValue, MemoryFragment, MemoryQuery
from axor_memory_sqlite import SQLiteMemoryProvider


@pytest.mark.asyncio
async def test_save_and_load_orders_by_value_priority() -> None:
    provider = SQLiteMemoryProvider(":memory:")
    try:
        await provider.save([
            MemoryFragment(namespace="agent", key="working", content="w", value=FragmentValue.WORKING),
            MemoryFragment(namespace="agent", key="pinned", content="p", value=FragmentValue.PINNED),
        ])

        fragments = await provider.load(MemoryQuery(namespaces=("agent",), max_results=10))

        assert [fragment.key for fragment in fragments] == ["pinned", "working"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_delete_and_namespaces() -> None:
    provider = SQLiteMemoryProvider(":memory:")
    try:
        await provider.save([
            MemoryFragment(namespace="agent-a", key="one", content="a", value=FragmentValue.KNOWLEDGE),
            MemoryFragment(namespace="agent-b", key="two", content="b", value=FragmentValue.EPHEMERAL),
        ])

        namespaces = await provider.namespaces()
        deleted = await provider.delete("agent-a", ["one"])
        remaining = await provider.load(MemoryQuery(namespaces=("agent-a", "agent-b"), max_results=10))

        assert namespaces == ["agent-a", "agent-b"]
        assert deleted == 1
        assert [fragment.namespace for fragment in remaining] == ["agent-b"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_evict_by_value() -> None:
    provider = SQLiteMemoryProvider(":memory:")
    try:
        old = datetime(2000, 1, 1, tzinfo=timezone.utc)
        await provider.save([
            MemoryFragment(namespace="agent", key="old", content="x", value=FragmentValue.EPHEMERAL, created_at=old),
            MemoryFragment(namespace="agent", key="keep", content="y", value=FragmentValue.PINNED, created_at=old),
        ])

        removed = await provider.evict("agent", values=(FragmentValue.EPHEMERAL,))
        remaining = await provider.load(MemoryQuery(namespaces=("agent",), max_results=10))

        assert removed == 1
        assert [fragment.key for fragment in remaining] == ["keep"]
    finally:
        await provider.close()