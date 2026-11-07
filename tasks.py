"""Task model, global task registry and the concurrency limiter (queue)."""
import asyncio
import itertools
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import config


class TaskCancelled(Exception):
    """Raised when the user cancels a task."""


class UserError(Exception):
    """An expected error whose message is safe to show to the user."""


_sequence = itertools.count(1)


@dataclass
class Task:
    user_id: int
    chat_id: int
    name: str
    source: str  # "telegram" | "url"
    client: object = None
    status_msg: object = None
    state: str = "queued"  # queued | downloading | processing | uploading
    seq: int = field(default_factory=lambda: next(_sequence))
    created: float = field(default_factory=time.time)
    started: float = 0.0
    last_edit: float = 0.0
    cancelled: bool = False
    proc: object = None  # running subprocess (aria2c / ffmpeg), if any
    notes: list = field(default_factory=list)  # warnings shown to the user after upload

    @property
    def task_id(self) -> str:
        return str(self.seq)

    def cancel(self) -> None:
        self.cancelled = True
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    def check(self) -> None:
        if self.cancelled:
            raise TaskCancelled()


TASKS: dict = {}  # task_id -> Task (queued + running)


def queue_position(task: Task) -> int:
    waiting = [t for t in TASKS.values() if t.state == "queued" and t.seq < task.seq]
    return len(waiting) + 1


class Limiter:
    """Runs at most ``limit`` tasks at once; the rest wait in FIFO order."""

    def __init__(self, limit: int = 1) -> None:
        self.limit = max(1, int(limit))
        self.active = 0
        self._cond = None

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def set_limit(self, value: int) -> None:
        cond = self._condition()
        async with cond:
            self.limit = max(1, int(value))
            cond.notify_all()

    async def wake(self) -> None:
        cond = self._condition()
        async with cond:
            cond.notify_all()

    @asynccontextmanager
    async def slot(self, task: Task):
        cond = self._condition()
        async with cond:
            await cond.wait_for(lambda: task.cancelled or self.active < self.limit)
            task.check()
            self.active += 1
        try:
            yield
        finally:
            async with cond:
                self.active -= 1
                cond.notify_all()


limiter = Limiter(config.MAX_CONCURRENT_TASKS)
