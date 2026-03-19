"""
task_registry.py - Global registry of active extraction tasks, keyed by chat_id.

Both the trigger-monitor path (bot.py) and the direct /extract path
(conversation.py) register their tasks here so that /cancel can always
find and stop the running extraction regardless of how it was started.
"""
import asyncio
from typing import Optional

# chat_id -> asyncio.Task
_ACTIVE: dict[int, asyncio.Task] = {}


def register(chat_id: int, task: asyncio.Task) -> None:
    _ACTIVE[chat_id] = task


def unregister(chat_id: int) -> None:
    _ACTIVE.pop(chat_id, None)


def get(chat_id: int) -> Optional[asyncio.Task]:
    return _ACTIVE.get(chat_id)


def cancel(chat_id: int) -> bool:
    """Cancel task for chat_id. Returns True if a running task was found."""
    task = _ACTIVE.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False
