"""Process-wide limits for the memory-hungry stages.

Stem separation (Demucs) and model-based lyric alignment (WhisperX and friends)
are the two slow, memory-hungry parts of a render. They used to share one slot,
which made a batch look sequential: song 2 could not touch its own stems until
song 1 had finished aligning. They now get independent budgets, so one song can
align while another separates stems.

Both default to 1 slot: each holds hundreds of megabytes of weights (gigabytes
on CUDA). Raise LYRIC_STEM_CONCURRENCY / LYRIC_ALIGN_CONCURRENCY only on a
machine with the RAM/VRAM to back it.

The gates are threading.Semaphore because the ML stages run in worker threads;
run_stem/run_align acquire inside the worker so the event loop is never blocked
while a job waits its turn.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

TR = TypeVar("TR")

# kind -> (environment variable, default slots)
_LIMITS: dict[str, tuple[str, int]] = {
    "stem": ("LYRIC_STEM_CONCURRENCY", 1),
    "align": ("LYRIC_ALIGN_CONCURRENCY", 1),
}

# Older installations set this name for the one gate that used to exist.
_ALIASES = {"stem": "LYRIC_HEAVY_CONCURRENCY"}

_slots: dict[str, threading.Semaphore | None] = {kind: None for kind in _LIMITS}
_slots_lock = threading.Lock()


def limit(kind: str) -> int:
    """Slot count for one stage kind, read from the environment on first use.

    Lazy so a value loaded from .env by the app takes effect: concurrency is
    imported before load_dotenv() runs.
    """
    env, default = _LIMITS[kind]
    raw = os.environ.get(env)
    if raw is None:
        alias = _ALIASES.get(kind)
        if alias:
            raw = os.environ.get(alias)
    try:
        return max(1, min(8, int(raw if raw is not None else default)))
    except ValueError:
        return default


def _slot(kind: str) -> threading.Semaphore:
    with _slots_lock:
        sem = _slots.get(kind)
        if sem is None:
            sem = threading.Semaphore(limit(kind))
            _slots[kind] = sem
        return sem


@contextmanager
def stem_stage() -> Iterator[None]:
    """Hold a stem-separation slot for the duration of the block."""
    with _slot("stem"):
        yield


@contextmanager
def align_stage() -> Iterator[None]:
    """Hold a lyric-alignment slot for the duration of the block."""
    with _slot("align"):
        yield


def _run_in_slot(kind: str, fn: Callable[..., TR], args: tuple, kwargs: dict) -> TR:
    with _slot(kind):
        return fn(*args, **kwargs)


async def run_stage(kind: str, fn: Callable[..., TR], *args, **kwargs) -> TR:
    """Run a blocking ML call in a worker thread, holding that stage's slot."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _run_in_slot(kind, fn, args, kwargs)
    )


async def run_stem(fn: Callable[..., TR], *args, **kwargs) -> TR:
    return await run_stage("stem", fn, *args, **kwargs)


async def run_align(fn: Callable[..., TR], *args, **kwargs) -> TR:
    return await run_stage("align", fn, *args, **kwargs)


def limits() -> dict[str, int]:
    """Current slot counts, for diagnostics."""
    return {kind: limit(kind) for kind in _LIMITS}


def _reset_slots() -> None:
    """Forget the cached semaphores so tests can change the environment first."""
    with _slots_lock:
        for kind in _slots:
            _slots[kind] = None


# Backwards-compatible names: the old single gate was effectively the stem gate.
heavy_stage = stem_stage
run_heavy = run_stem
