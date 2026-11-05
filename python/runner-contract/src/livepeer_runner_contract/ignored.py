"""Logging each ignored runner once (SPEC.md, gateway rule 2)."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from ._json import safe
from .envelope import Ineligible
from .select import RunnerKey
from .standard import REPOSITORY

__all__ = ["forget_ignored", "log_ignored"]

_MAX_REMEMBERED = 65536
_seen: OrderedDict[RunnerKey | str, None] = OrderedDict()
_lock = threading.Lock()


def _key_text(key: RunnerKey | str) -> str:
    if isinstance(key, RunnerKey):
        return f"{safe(key.runner_id)} at {safe(key.orchestrator, 128)}"
    return safe(key, 128)


def log_ignored(
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
    runner_key: RunnerKey | str,
    reason: Ineligible | str,
    *,
    level: int = logging.WARNING,
) -> bool:
    """Log that a runner is ignored, with the reason and a link to the standard, once per runner.

    Returns whether it logged. Runner IDs and addresses come from discovery, so they are quoted
    where they aren't plain tokens, and no line can carry a runner's own newlines. The process
    remembers the last 65,536 runners it logged; ``forget_ignored`` clears one or all.
    """
    with _lock:
        if runner_key in _seen:
            _seen.move_to_end(runner_key)
            return False
        _seen[runner_key] = None
        if len(_seen) > _MAX_REMEMBERED:
            _seen.popitem(last=False)
    detail = str(reason).replace("\r", " ").replace("\n", " ")
    logger.log(level, "ignoring runner %s: %s; see %s", _key_text(runner_key), detail, REPOSITORY)
    return True


def forget_ignored(runner_key: RunnerKey | str | None = None) -> None:
    """Forget that a runner (or, with no key, every runner) was logged, so it is logged again."""
    with _lock:
        if runner_key is None:
            _seen.clear()
        else:
            _seen.pop(runner_key, None)
