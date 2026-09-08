"""Timeline model: describe a multi-clip edit declaratively, then render it.

The Timeline is a list of Segments. Each Segment references a source media file
and optionally a trim window. Rendered output fits a target Preset (crop-to-fill),
optionally with per-segment text overlays, and concatenates into one video.

This is the layer the Hermes orchestrator drives: it builds a Timeline from a
script/prompt and calls render() to produce a final social-media-ready video.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from . import ops
from .presets import Preset, get as get_preset
from .text import TextOverlay, burn_text_filter


@dataclass
class Segment:
    source: str                     # path to source media
    label: str = ""                 # optional human label
    start: Optional[float] = None   # trim start (s)
    end: Optional[float] = None     # trim end (s)
    duration: Optional[float] = None
    overlays: List[TextOverlay] = field(default_factory=list)


@dataclass
class Timeline:
    segments: List[Segment]
    preset: Preset
    output: str
    crop_mode: str = ops.CROP_CENTER

    def __post_init__(self):
        if isinstance(self.preset, str):
            self.preset = get_preset(self.preset)

    def render(self, work_dir: str | None = None) -> str:
        """Render each segment to a normalized clip, then concatenate.

        Returns the final output path.
        """
        import os
        import tempfile
        from pathlib import Path

        work_dir = work_dir or tempfile.mkdtemp(prefix="vedits_")
        os.makedirs(work_dir, exist_ok=True)

        clips: List[str] = []
        for i, seg in enumerate(self.segments):
            # 1) Trim to window (fast stream copy).
            trimmed: Optional[str] = None
            if seg.start is not None or seg.end is not None or seg.duration is not None:
                trimmed = os.path.join(work_dir, f"trim_{i}.mp4")
                ops.trim(seg.source, trimmed, start=seg.start, end=seg.end, duration=seg.duration)
                source_for_fit = trimmed
            else:
                source_for_fit = seg.source

            # 2) Fit + re-encode to target preset, burning any overlays.
            out = os.path.join(work_dir, f"clip_{i}.mp4")
            extra = burn_text_filter(seg.overlays) if seg.overlays else None
            ops.fit(source_for_fit, out, self.preset, mode=self.crop_mode,
                    extra_filters=extra)
            clips.append(out)

        # 3) Concatenate normalized clips.
        ops.concat(clips, self.output)
        # Clean up intermediates.
        for c in clips:
            try:
                os.remove(c)
            except OSError:
                pass
        return self.output


def simple_timeline(sources: Sequence[str], preset: str | Preset, output: str,
                    labels: Optional[Sequence[str]] = None) -> Timeline:
    """Build a Timeline from a flat list of sources using a named preset."""
    segs = [
        Segment(source=s, label=(labels[i] if labels and i < len(labels) else ""))
        for i, s in enumerate(sources)
    ]
    return Timeline(segments=segs, preset=preset, output=output)
