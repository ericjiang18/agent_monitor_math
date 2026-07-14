"""Shared streaming subprocess helper for engine runners."""
from __future__ import annotations

import subprocess
import time


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
    )
    if on_start:
        on_start(proc)

    buf: list[str] = []
    started = time.time()
    last_cb = 0.0
    timed_out = False
    assert proc.stdout is not None
    for line in proc.stdout:
        buf.append(line)
        now = time.time()
        if on_output and now - last_cb > 2.0:
            try:
                on_output("".join(buf))
            except Exception:  # noqa: BLE001
                pass
            last_cb = now
        if now - started > timeout:
            proc.kill()
            timed_out = True
            break
    proc.wait(timeout=30)
    output = "".join(buf)
    if on_output:
        try:
            on_output(output)
        except Exception:  # noqa: BLE001
            pass
    return output, proc.returncode, timed_out
