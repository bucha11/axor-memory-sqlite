"""axor-memory-sqlite — SQLite MemoryProvider for axor-core."""
from axor_memory_sqlite._version import get_version
from axor_memory_sqlite.provider import SQLiteMemoryProvider

__version__ = get_version("axor-memory-sqlite")
__all__ = ["SQLiteMemoryProvider", "__version__"]
