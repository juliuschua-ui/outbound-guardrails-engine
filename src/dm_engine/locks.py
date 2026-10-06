"""Cross-process single-writer lock. Contract: PLAN.md §12.2.

`hold()` takes an exclusive `flock()` on `<locks_dir>/<name>.lock`. This is what keeps
two concurrent `dmctl stage` runs from both passing the daily-cap check (§10 row (c)):
the cap count and the `stage_events` insert run inside one flock + one transaction.

flock ownership belongs to the open file description, not the process, so a second
`hold()` of the same name blocks even inside the same process. Do not nest.
"""

import errno
import fcntl
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Config

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_POLL_INTERVAL_S = 0.05


def lock_path(cfg: Config, name: str) -> Path:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"lock name must be [A-Za-z0-9_.-]+, got {name!r}")
    return cfg.locks_dir / f"{name}.lock"


@contextmanager
def hold(cfg: Config, name: str, timeout_s: float = 30.0):
    """Hold the named lock for the duration of the block.

    Raises TimeoutError if another process still holds it after `timeout_s`.
    Yields the lock file path (handy for logging which file is contended).
    """
    path = lock_path(cfg, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"lock '{name}' still held after {timeout_s}s ({path})"
                    ) from exc
                time.sleep(_POLL_INTERVAL_S)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
