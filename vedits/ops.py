"""Core FFmpeg edit operations: trim, crop, resize, transform, concat, export."""
from __future__ import annotations

import os
from typing import List, Optional, Sequence

from . import probe, util
from .presets import Preset

# Crop modes for changing aspect (e.g. horizontal -> vertical).
CROP_CENTER = "center"
CROP_LEFT = "left"
CROP_RIGHT = "right"
CROP_TOP = "top"
CROP_BOTTOM = "bottom"


def _ff(args: List[str], *, check: bool = True):
    return util.run([util.resolve_ffmpeg(), "-hide_banner", "-y"] + args, check=check)


def _normalize_geometry(width: int, height: int) -> str:
    """Force even dimensions (required by many codecs)."""
    w = width if width % 2 == 0 else width - 1
    h = height if height % 2 == 0 else height - 1
    return f"{w}:{h}"


def auto_crop_scale_filter(src_w: int, src_h: int, dst_w: int, dst_h: int, mode: str = CROP_CENTER) -> str:
    """Build a scale+crop filter chain to fit source into target dimensions
    without distortion.

    Behavior:
      - If target aspect is wider than source, crop vertically (top/bottom).
      - If target aspect is taller (narrower) than source, crop horizontally.
      - Scale up/down so the cropped region fills the target exactly.
    """
    src_aspect = src_w / src_h
    dst_aspect = dst_w / dst_h
    filters = []
    if src_aspect > dst_aspect:
        # Source is wider -> crop horizontal (left/right) to match target aspect.
        crop_h = src_h
        crop_w = int(round(src_h * dst_aspect))
        if mode == CROP_LEFT:
            x = 0
        elif mode == CROP_RIGHT:
            x = src_w - crop_w
        else:
            x = (src_w - crop_w) // 2
        filters.append(f"crop={crop_w}:{crop_h}:{x}:0")
        filters.append(f"scale={dst_w}:{dst_h}")
    elif src_aspect < dst_aspect:
        # Source is taller -> crop vertical (top/bottom).
        crop_w = src_w
        crop_h = int(round(src_w / dst_aspect))
        if mode == CROP_TOP:
            y = 0
        elif mode == CROP_BOTTOM:
            y = src_h - crop_h
        else:
            y = (src_h - crop_h) // 2
        filters.append(f"crop={crop_w}:{crop_h}:0:{y}")
        filters.append(f"scale={dst_w}:{dst_h}")
    else:
        filters.append(f"scale={dst_w}:{dst_h}")
    return ",".join(filters)


def trim(src: str, dst: str, *, start: Optional[float] = None, end: Optional[float] = None,
         duration: Optional[float] = None) -> str:
    """Trim a video to a time range. Returns the output path."""
    args = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        # ffmpeg -t is a duration; convert end to duration if start known.
        d = end - (start or 0)
        args += ["-t", f"{d:.3f}"]
    elif duration is not None:
        args += ["-t", f"{duration:.3f}"]
    args += ["-i", src]
    # Stream copy when no re-encode requested (fast, lossless).
    args += ["-c", "copy", dst]
    _ff(args)
    return dst


def crop(src: str, dst: str, *, width: int, height: int,
         x: int = 0, y: int = 0) -> str:
    """Crop a region of a video without re-encoding the remainder.

    x/y default to center-region via 'crop=w:h' with auto centering when
    x/y negative (ffmpeg centers). Negative x/y is supported natively.
    """
    mode = "crop"
    w = width if width % 2 == 0 else width - 1
    h = height if height % 2 == 0 else height - 1
    if x < 0 or y < 0:
        # Negative -> centered (ffmpeg autoplaces).
        args = ["-i", src, "-vf", f"crop={w}:{h}", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "copy", dst]
    else:
        args = ["-i", src, "-vf", f"crop={w}:{h}:{x}:{y}", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "copy", dst]
    _ff(args)
    return dst


def rescale(src: str, dst: str, *, width: Optional[int] = None, height: Optional[int] = None) -> str:
    """Resize a video, preserving aspect (letterbox/pillarbox not applied)."""
    vf = []
    if width and height:
        vf.append(f"scale={_normalize_geometry(width, height)}")
    elif width:
        vf.append(f"scale={width if width % 2 == 0 else width - 1}:-2")
    elif height:
        vf.append(f"scale=-2:{height if height % 2 == 0 else height - 1}")
    else:
        raise ValueError("rescale requires width and/or height")
    _ff(["-i", src, "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", dst])
    return dst


def fit(src: str, dst: str, preset: Preset, *, mode: str = CROP_CENTER,
        audio: bool = True, crf: Optional[int] = None, extra_filters: Optional[str] = None) -> str:
    """Reframe + re-encode a video to fill a target preset exactly (crop to fill).

    This is the workhorse for social-media output: takes any source aspect and
    produces a video that exactly matches the target dimensions.
    """
    info = probe.probe(src)
    sw, sh = probe.dimensions(info)
    has_audio = probe.audio_stream(info) is not None
    if not sw or not sh:
        raise ValueError(f"Could not determine dimensions for {src}")
    filters = [auto_crop_scale_filter(sw, sh, preset.width, preset.height, mode)]
    if extra_filters:
        filters.append(extra_filters)
    args = ["-i", src, "-vf", ",".join(filters)]
    args += ["-map", "0:v"]
    if audio and has_audio:
        args += ["-map", "0:a?"]
    args += preset.ffmpeg_args(crf=crf, audio=audio and has_audio)
    args += ["-movflags", "+faststart", dst]
    _ff(args)
    return dst


def concat(clips: Sequence[str], dst: str, *, audio: bool = True) -> str:
    """Concatenate clips with identical codecs/settings (fast, stream-copy)."""
    if not clips:
        raise ValueError("concat requires at least one clip")
    list_file = os.path.join(os.path.dirname(dst) or ".", ".vedits_concat.txt")
    # Write ABSOLUTE posix-style paths: ffmpeg's concat demuxer resolves
    # relative 'file' entries against the list file's own directory, so
    # relative inputs get mangled (out/out/a.mp4).
    with open(list_file, "w", encoding="utf-8") as f:
        for c in clips:
            abs_c = os.path.abspath(c).replace("\\", "/")
            esc = abs_c.replace("'", "'\\''")
            f.write(f"file '{esc}'\n")
    try:
        args = ["-f", "concat", "-safe", "0", "-i", list_file]
        args += ["-c", "copy"] if audio else ["-c:v", "copy", "-an"]
        args += ["-movflags", "+faststart", dst]
        _ff(args)
    finally:
        if os.path.exists(list_file):
            os.remove(list_file)
    return dst


def reencode(src: str, dst: str, *, fps: Optional[float] = None,
             audio_bitrate: Optional[str] = None) -> str:
    """Uniformly re-encode (normalize settings); used before concat when clips differ."""
    args = ["-i", src]
    vf = []
    if fps:
        vf.append(f"fps={fps}")
    v = []
    if vf:
        v += ["-vf", ",".join(vf)]
    v += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if fps:
        v += ["-r", f"{fps}"]
    if audio_bitrate:
        v += ["-c:a", "aac", "-b:a", audio_bitrate]
    else:
        v += ["-c:a", "aac"]
    args += ["-map", "0:v", "-map", "0:a?"]
    args += v + ["-movflags", "+faststart", dst]
    _ff(args)
    return dst


def reverse(src: str, dst: str) -> str:
    """Reverse a video clip (re-encoded)."""
    args = ["-i", src, "-vf", "reverse", "-af", "areverse", "-preset", "veryfast", "-c:a", "aac", dst]
    # '-af areverse' only when audio present; if none, drop it.
    _ff(args)
    return dst


def extract_frame(src: str, dst: str, *, at: float = 0.0) -> str:
    """Extract a single frame as an image (PNG)."""
    args = ["-ss", f"{at:.3f}", "-i", src, "-frames:v", "1", dst]
    _ff(args)
    return dst
