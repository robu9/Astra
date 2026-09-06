"""Crash-safe writes and cross-process locks for Astra's durable state."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Replace a text file atomically so interruption cannot leave partial JSON/TSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _file_lock(resource: Path, timeout: float = 30.0):
    digest = hashlib.sha256(str(resource.resolve()).encode()).hexdigest()
    lock_root = Path(tempfile.gettempdir()) / "astra-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{digest}.lock"
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + timeout

    if os.name == "nt":
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()

        def acquire():
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

        def release():
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def acquire():
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release():
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    while True:
        try:
            acquire()
            break
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f"timed out waiting for Astra state lock: {resource}")
            time.sleep(0.1)
    try:
        yield
    finally:
        release()
        handle.close()


@contextmanager
def exclusive_paths(paths: list[Path], timeout: float = 30.0):
    """Lock multiple state roots in stable order, preventing races and deadlocks."""
    unique = sorted({p.resolve() for p in paths}, key=str)
    with ExitStack() as stack:
        for path in unique:
            stack.enter_context(_file_lock(path, timeout))
        yield
