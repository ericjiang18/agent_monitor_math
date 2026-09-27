"""Shared streaming subprocess helper for engine runners."""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time

from agent_monitor.process_control import terminate_descendants


def stream_subprocess(
    cmd: list[str],
    *,
    cwd: str,
    env: dict,
    timeout: int = 86400,
    on_start=None,
    on_output=None,
) -> tuple[str, int, bool]:
    """Run cmd streaming combined stdout/stderr.

    Calls on_start(proc) after spawn and on_output(text_so_far) periodically.
    Returns (output, returncode, timed_out).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if on_start:
        on_start(proc)

    buf: list[str] = []
    started = time.time()
    last_cb = 0.0
    timed_out = False
    assert proc.stdout is not None

    # Reading a pipe with `for line in proc.stdout` blocks until a child emits
    # a newline. Some native harnesses instead write their live state only to
    # artifact JSONL, so their monitor appeared blank for many minutes. Keep
    # the blocking reader in a daemon thread while this loop emits heartbeats;
    # the callback can then enrich the run from those artifacts.
    output_queue: queue.Queue[object] = queue.Queue()
    reader_done = object()

    def _read_stdout() -> None:
        try:
            for line in proc.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(reader_done)

    reader = threading.Thread(
        target=_read_stdout,
        daemon=True,
        name=f"stream-reader-{proc.pid}",
    )
    reader.start()
    finished_reading = False
    while True:
        try:
            item = output_queue.get(timeout=0.25)
        except queue.Empty:
            item = None
        if item is reader_done:
            finished_reading = True
        elif isinstance(item, str):
            buf.append(item)

        now = time.time()
        if on_output and now - last_cb > 2.0:
            try:
                on_output("".join(buf))
            except Exception:  # noqa: BLE001
                pass
            last_cb = now
        if now - started > timeout:
            terminate_descendants(proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
            timed_out = True
            break
        if finished_reading and proc.poll() is not None:
            break

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        terminate_descendants(proc.pid)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        proc.wait(timeout=10)
    terminate_descendants(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    reader.join(timeout=1)
    proc.stdout.close()
    while not output_queue.empty():
        item = output_queue.get_nowait()
        if isinstance(item, str):
            buf.append(item)
    output = "".join(buf)
    if on_output:
        try:
            on_output(output)
        except Exception:  # noqa: BLE001
            pass
    return output, proc.returncode, timed_out
