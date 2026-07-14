"""Shared HTTP helpers for harness PDF / paper downloads."""
from __future__ import annotations

import shutil
import ssl
import subprocess
import urllib.request
from pathlib import Path

try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CONTEXT = ssl.create_default_context()


def urlopen(req: urllib.request.Request, timeout: int = 60):
    """urllib.request.urlopen with a proper CA bundle (fixes macOS SSL errors)."""
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)


def curl_download(url: str, dest: Path, timeout: int = 60) -> bool:
    """Download via system curl (handles cert chains Python urllib rejects)."""
    if not shutil.which("curl"):
        return False
    try:
        r = subprocess.run(
            [
                "curl", "-fsSL",
                "--max-time", str(timeout),
                "-A", "Mozilla/5.0 (compatible; deep-read/0.1)",
                "-o", str(dest),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 15,
        )
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
        err = (r.stderr or r.stdout or "").strip()
        print(f"[http_utils] curl failed for {url}: {err[:200]}")
    except Exception as e:
        print(f"[http_utils] curl error for {url}: {e}")
    return False
