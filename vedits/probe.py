"""FFprobe wrapper: probe media files for metadata."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from . import util


def probe(path: str, ffprobe: str | None = None) -> Dict[str, Any]:
    """Probe a media file and return the ffprobe JSON object."""
    ffprobe = ffprobe or util.resolve_ffprobe()
    proc = util.run(
        [
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            path,
        ]
    )
    return json.loads(proc.stdout)


def video_stream(info: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return the first video stream."""
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def audio_stream(info: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return the first audio stream."""
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def duration(info: Dict[str, Any]) -> float:
    """Total duration in seconds (format-level, falling back to video stream)."""
    try:
        return float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        vs = video_stream(info)
        try:
            return float(vs.get("duration", 0) or 0)
        except (TypeError, ValueError):
            return 0.0


def dimensions(info: Dict[str, Any]) -> tuple[int, int]:
    """Return (width, height) of the first video stream."""
    vs = video_stream(info)
    if not vs:
        return (0, 0)
    try:
        return (int(vs.get("width", 0)), int(vs.get("height", 0)))
    except (TypeError, ValueError):
        return (0, 0)


def frame_rate(info: Dict[str, Any]) -> float:
    """Best-effort frame rate of the first video stream (avg_frame_rate)."""
    vs = video_stream(info)
    if not vs:
        return 0.0
    afr = vs.get("avg_frame_rate", "0/0") or "0/0"
    try:
        num, den = afr.split("/")
        num, den = float(num), float(den)
        return num / den if den else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def codecs(info: Dict[str, Any]) -> Dict[str, str]:
    """Return {video: codec, audio: codec} of the first streams."""
    out: Dict[str, str] = {}
    vs = video_stream(info)
    if vs:
        out["video"] = vs.get("codec_name", "")
    as_ = audio_stream(info)
    if as_:
        out["audio"] = as_.get("codec_name", "")
    return out


def summarize(path: str) -> str:
    """Human-readable one-line summary for a media file."""
    info = probe(path)
    w, h = dimensions(info)
    dur = duration(info)
    codec = codecs(info)
    fps = frame_rate(info)
    return (
        f"{path}  {w}x{h}  {dur:.2f}s  {fps:.2f}fps  "
        f"video={codec.get('video','-')}  audio={codec.get('audio','-')}"
    )
