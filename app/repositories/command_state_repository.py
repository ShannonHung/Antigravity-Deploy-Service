import asyncio
import logging
from datetime import timedelta
from typing import Callable, Coroutine, Optional
from redis.asyncio import Redis
from app.domain.command import CommandState, CommandStatus
from app.core.exceptions import CommandExecutionException

_logger = logging.getLogger(__name__)


class CommandStateRepository:
    PREFIX = "command"

    def __init__(self, redis: Redis):
        self.redis = redis

    def _key(self, command_id: str) -> str:
        return f"{self.PREFIX}:{command_id}"

    async def save(self, state: CommandState, ttl_seconds: int):
        await self.redis.setex(
            self._key(state.command_id),
            timedelta(seconds=ttl_seconds),
            state.model_dump_json(),
        )

    async def get(self, command_id: str) -> CommandState:
        data = await self.redis.get(self._key(command_id))
        if not data:
            raise CommandExecutionException(
                f"Execution record {command_id} not found in Redis."
            )
        try:
            return CommandState.model_validate_json(data)
        except Exception as exc:
            raise CommandExecutionException(
                f"Invalid record format for {command_id}."
            ) from exc

    async def update(
        self,
        command_id: str,
        updater: Callable[[CommandState], Coroutine[None, None, None] | None],
        ttl_seconds: int,
    ):
        """Fetch, apply updater function, and save state."""
        state = await self.get(command_id)
        result = updater(state)
        if asyncio.iscoroutine(result):
            await result
        await self.save(state, ttl_seconds)

    async def update_if(
        self,
        command_id: str,
        condition: Callable[[CommandState], bool],
        updater: Callable[[CommandState], Coroutine[None, None, None] | None],
        ttl_seconds: int,
    ) -> bool:
        """Fetch, check condition, apply updater, and save. Returns True if updated, False otherwise."""
        state = await self.get(command_id)
        if not condition(state):
            return False

        result = updater(state)
        if asyncio.iscoroutine(result):
            await result
        await self.save(state, ttl_seconds)
        return True

    async def list_states(
        self, statuses: Optional[set[CommandStatus]] = None
    ) -> list[CommandState]:
        """Scan all command:* keys and return the parsed states.

        Cursor-based scan_iter (not KEYS) so it is safe on a shared Redis.
        Unparseable records are skipped with a warning. When `statuses` is
        given, only states with a matching status are returned.
        """
        out: list[CommandState] = []
        async for key in self.redis.scan_iter(match=f"{self.PREFIX}:*"):
            raw = await self.redis.get(key)
            if not raw:
                continue
            try:
                state = CommandState.model_validate_json(raw)
            # A single unparseable Redis record must not abort a scan over all of them.
            except Exception:  # pylint: disable=broad-exception-caught
                _logger.warning("Skipping unparseable command state at key %s", key)
                continue
            if statuses is None or state.status in statuses:
                out.append(state)
        return out
