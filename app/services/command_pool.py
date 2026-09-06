import asyncio
from typing import Dict, List, Optional

from app.domain.command import RunningCommandEntry
from app.core.config import get_settings

settings = get_settings()

_local_running_commands: Dict[str, RunningCommandEntry] = {}


def pool_add(command_id: str, entry: RunningCommandEntry) -> None:
    """Register a locally-running command in the process-wide pool."""
    _local_running_commands[command_id] = entry


def pool_get(command_id: str) -> Optional[RunningCommandEntry]:
    """Fetch a locally-running command entry, or None if not on this pod."""
    return _local_running_commands.get(command_id)


def pool_remove(command_id: str) -> None:
    """Drop a command from the local pool (no-op if absent)."""
    _local_running_commands.pop(command_id, None)


def pool_size() -> int:
    """Number of commands currently running locally (backpressure gate)."""
    return len(_local_running_commands)


def pool_command_ids() -> List[str]:
    """Snapshot of locally-running command ids (for graceful shutdown)."""
    return list(_local_running_commands.keys())


_execution_semaphore: Optional[asyncio.Semaphore] = None  # pylint: disable=invalid-name


def _get_semaphore() -> asyncio.Semaphore:
    """Lazily initialise the concurrency semaphore (must be called inside a running event loop)."""
    global _execution_semaphore
    if _execution_semaphore is None:
        _execution_semaphore = asyncio.Semaphore(settings.COMMAND_MAX_CONCURRENCY)
    return _execution_semaphore
