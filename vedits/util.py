"""Shared helpers: subprocess running, paths, timestamps."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# Default tool locations — configurable via env for portability.
DEFAULT_FFMPEG = str(Path(os.environ.get("VEDITS_FFMPEG", r"C:\Users\User\tools\ffmpeg\ffmpeg.exe")))
DEFAULT_FFPROBE = str(Path(os.environ.get("VEDITS_FFPROBE", r"C:\Users\User\tools\ffmpeg\ffprobe.exe")))


def resolve_ffmpeg() -> str:
    """Return the ffmpeg binary path, searching PATH if env not set."""
    return _resolve(DEFAULT_FFMPEG, "ffmpeg")


def resolve_ffprobe() -> str:
    """Return the ffprobe binary path, searching PATH if env not set."""
    return _resolve(DEFAULT_FFPROBE, "ffprobe")


def _resolve(default: str, name: str) -> str:
    if default and os.path.isfile(default):
        return default
    # Fall back to PATH.
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(d, name + (".exe" if sys.platform == "win32" else ""))
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        f"{name} not found. Install FFmpeg or set VEDITS_{name.upper()} env var."
    )


def parse_timecode(tc: str) -> float:
    """Parse 'HH:MM:SS.mmm', 'MM:SS.mmm', 'SS.mmm', or bare float to seconds."""
    tc = tc.strip()
    if not tc:
        return 0.0
    try:
        return float(tc)
    except ValueError:
        pass
    parts = tc.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def format_timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def run(cmd: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, streaming stderr, returning the CompletedProcess."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n--- output ---\n{proc.stdout}"
        )
    return proc


def safe_name(name: str) -> str:
    """Sanitize a string for use as a filename fragment."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)
