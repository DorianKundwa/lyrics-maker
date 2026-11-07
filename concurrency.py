"""Process-wide limits for the memory-hungry stages.

Demucs separation and model-based lyric alignment each hold hundreds of
megabytes of weights (several gigabytes on CUDA). Two of them at once exhausts
a consumer GPU, so by default they share a single slot. Raise
LYRIC_HEAVY_CONCURRENCY only on a machine with the VRAM to back it.

The gate is a threading.Semaphore because the ML stages themselves run in
worker threads; `run_heavy` acquires it inside the worker so the event loop is
never blocked while a job waits its turn.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

TR = TypeVar("TR")


def _limit() -> int:
    try:
        return max(1, int(os.environ.get("LYRIC_HEAVY_CONCURRENCY", "1")))
    except ValueError:
        return 1


_HEAVY = threading.Semaphore(_limit())


@contextmanager
def heavy_stage() -> Iterator[None]:
    """Hold the shared ML slot for the duration of the block."""
    with _HEAVY:
        yield


async def run_heavy(fn: Callable[..., TR], *args, **kwargs) -> TR:
    """Run a blocking ML call in a worker thread, holding the shared slot."""
    loop = asyncio.get_running_loop()

    def call() -> TR:
        with _HEAVY:
            return fn(*args, **kwargs)

    return await loop.run_in_executor(None, call)
