"""Cleanup for harnesses whose native CLI children create new sessions."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import time


def _process_stat(pid: int) -> tuple[int, str, str] | None:
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rpartition(') ')[2].split()
        return int(fields[1]), fields[19], fields[0]  # parent, start time, state
    except (OSError, ValueError, IndexError):
        return None


def _terminate_linux_descendants(pid: int, *, force: bool) -> None:
    """stdlib fallback for isolated engine environments without psutil."""
    try:
        snapshot = {int(p.name): _process_stat(int(p.name))
                    for p in Path('/proc').iterdir() if p.name.isdigit()}
    except OSError:
        return
    descendants: dict[int, tuple[int, str, str]] = {}
    pending = {pid}
    while pending:
        found = {p: stat for p, stat in snapshot.items()
                 if stat and stat[0] in pending and p not in descendants and p != pid}
        descendants.update(found)
        pending = set(found)

    def send(child: int, original: tuple[int, str, str], sig: int) -> bool:
        current = _process_stat(child)
        if current is None or current[1] != original[1] or current[2] == 'Z':
            return False
        try:
            os.kill(child, sig)
        except OSError:
            return False
        return True

    for child, original in reversed(list(descendants.items())):
        send(child, original, signal.SIGKILL if force else signal.SIGTERM)
    if descendants and not force:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not any(_process_stat(p) == original for p, original in descendants.items()):
                break
            time.sleep(.02)
        for child, original in descendants.items():
            send(child, original, signal.SIGKILL)


def terminate_descendants(pid: int, *, force: bool = True) -> None:
    """Capture descendants before killing the wrapper and losing ancestry.

    Signal individual processes: their process groups may be shared with a
    service, or deliberately detached from the registered runner's group.
    psutil checks process identity before signaling, protecting against PID
    reuse. A graceful request has a bounded grace period for stubborn tools.
    """
    try:
        import psutil
    except ImportError:
        _terminate_linux_descendants(pid, force=force)
        return
    try:
        children = psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return
    for child in reversed(children):
        try:
            child.kill() if force else child.terminate()
        except psutil.Error:
            pass
    if not force and children:
        _, alive = psutil.wait_procs(children, timeout=1)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
