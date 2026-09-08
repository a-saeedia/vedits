"""Project model: a CapCut-style edit described declaratively as JSON.

This is the lingua franca between the GUI, the agent, and the renderer. A
project is a list of clips with trim windows, per-clip text overlays, and
optional xfade transitions between consecutive clips.

An agent can POST this JSON to /api/render and get back a finished upload-ready
video — no human in the loop. The GUI edits the same JSON.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import ops, probe, util
from .presets import Preset, get as get_preset
from .text import TextOverlay, burn_text_filter


@dataclass
class ProjectClip:
    id: str
    src: str
    start: Optional[float] = None     # trim in (media seconds)
    end: Optional[float] = None       # trim out (media seconds)
    duration: Optional[float] = None  # explicit duration (overrides end)
    text: Optional[str] = None        # caption burned onto this clip
    text_size: int = 48
    text_color: str = "white"
    speed: float = 1.0                # 0.25 - 4.0 playback speed
    transition_to_next: Optional[str] = None  # xfade name, e.g. "fade", "dissolve"
    transition_duration: float = 0.0

    @property
    def label(self) -> str:
        return self.text or f"clip {self.id}"

    @property
    def source_duration(self) -> float:
        """Duration of the trim window in source media seconds."""
        info = probe.probe(self.src)
        total = probe.duration(info)
        if self.duration:
            return self.duration
        if self.start is not None and self.end is not None:
            return max(0.1, self.end - self.start)
        if self.start is not None:
            return max(0.1, total - self.start)
        if self.end is not None:
            return max(0.1, self.end)
        return max(0.1, total)

    @property
    def output_duration(self) -> float:
        """Duration in timeline seconds after speed adjustment."""
        return self.source_duration / self.speed


@dataclass
class Project:
    clips: List[ProjectClip]
    preset: Preset
    output: str
    fps: int = 30
    crop_mode: str = ops.CROP_CENTER

    def __post_init__(self):
        if isinstance(self.preset, str):
            self.preset = get_preset(self.preset)

    # --- serialization -------------------------------------------------------
    @classmethod
    def from_dict(cls, d: Dict[str, Any], output: str, preset: str | Preset) -> "Project":
        clips = [
            ProjectClip(
                id=c.get("id", f"c{i}"),
                src=c["src"],
                start=c.get("start"),
                end=c.get("end"),
                duration=c.get("duration"),
                text=c.get("text"),
                text_size=c.get("text_size", 48),
                text_color=c.get("text_color", "white"),
                speed=c.get("speed", 1.0),
                transition_to_next=c.get("transition_to_next"),
                transition_duration=c.get("transition_duration", 0.0),
            )
            for i, c in enumerate(d.get("clips", []))
        ]
        obj = cls(clips=clips, preset=preset, output=output)
        obj.fps = int(d.get("fps", 30))
        obj.crop_mode = d.get("crop_mode", ops.CROP_CENTER)
        return obj

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fps": self.fps,
            "crop_mode": self.crop_mode,
            "clips": [
                {
                    "id": c.id,
                    "src": c.src,
                    "start": c.start,
                    "end": c.end,
                    "duration": c.duration,
                    "text": c.text,
                    "text_size": c.text_size,
                    "text_color": c.text_color,
                    "speed": c.speed,
                    "transition_to_next": c.transition_to_next,
                    "transition_duration": c.transition_duration,
                }
                for c in self.clips
            ],
        }

    @classmethod
    def load(cls, path: str, preset: str | Preset) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls.from_dict(d, path, preset)

    # --- render --------------------------------------------------------------
    def total_duration(self) -> float:
        total = 0.0
        for i, c in enumerate(self.clips):
            total += c.output_duration
            if i + 1 < len(self.clips) and c.transition_to_next and c.transition_duration:
                total -= c.transition_duration
        return total

    def render(self, work_dir: Optional[str] = None) -> str:
        """Render the project to self.output.

        Strategy:
          - Normalize each clip (trim -> fit/reencode to preset) into temp files,
            burning per-clip text overlays.
          - Concatenate. If any transitions are present, use an xfade filter
            chain; otherwise use fast stream-copy concat.
        """
        work_dir = work_dir or tempfile.mkdtemp(prefix="vedits_")
        os.makedirs(work_dir, exist_ok=True)

        normalized: List[str] = []
        durations: List[float] = []
        for i, clip in enumerate(self.clips):
            norm = os.path.join(work_dir, f"seg_{i}.mp4")
            self._normalize_clip(clip, norm)
            normalized.append(norm)
            durations.append(clip.output_duration)

        has_transitions = any(
            c.transition_to_next and c.transition_duration for c in self.clips[:-1]
        )

        if has_transitions and len(normalized) > 1:
            self._concat_with_xfade(normalized, durations, self.output)
        else:
            ops.concat(normalized, self.output)

        for n in normalized:
            try:
                os.remove(n)
            except OSError:
                pass
        return self.output

    def _normalize_clip(self, clip: ProjectClip, dst: str) -> None:
        """Produce a single preset-fitted, text-burned, speed-adjusted clip."""
        # 1) Trim to the source window (stream copy is fast).
        trimmed = clip.src
        cleanup = None
        if clip.start is not None or clip.end is not None or clip.duration is not None:
            trimmed = dst + ".trim.mp4"
            ops.trim(clip.src, trimmed, start=clip.start, end=clip.end, duration=clip.duration)
            cleanup = trimmed

        # 2) Build filter chain: fit + optional speed + optional text.
        info = probe.probe(trimmed)
        sw, sh = probe.dimensions(info)
        has_audio = probe.audio_stream(info) is not None
        filters = [ops.auto_crop_scale_filter(sw, sh, self.preset.width, self.preset.height, self.crop_mode)]

        if clip.speed != 1.0:
            # setpts scales video time; atempo scales audio (0.5-100, chain multiples <2).
            filters.append(f"setpts={1.0/clip.speed:.6f}*PTS")
        if clip.text:
            ov = TextOverlay(text=clip.text, fontsize=clip.text_size, fontcolor=clip.text_color)
            filters.append(ov.filter())

        ff = util.resolve_ffmpeg()
        args = [ff, "-hide_banner", "-y", "-i", trimmed]
        if clip.speed != 1.0 and has_audio:
            args += ["-af", _atempo_filter(clip.speed)]
        args += ["-vf", ",".join(filters)]
        args += ["-map", "0:v", "-map", "0:a?"]
        args += self.preset.ffmpeg_args()
        args += ["-movflags", "+faststart", dst]
        util.run(args)
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass

    def _concat_with_xfade(self, clips: List[str], durations: List[float], dst: str) -> None:
        """Concatenate normalized clips using chained xfade (+ acrossfade audio)."""
        n = len(clips)
        ff = util.resolve_ffmpeg()

        # Video: build chained xfade.
        # offset_k = final_dur_{k} - trans_dur_k ; final grows by d_{k+1} - trans_k.
        final = durations[0]
        offsets: List[float] = []
        trans_durations: List[float] = []
        for k in range(n - 1):
            td = self.clips[k].transition_duration or 0.0
            trans_durations.append(td)
            off = max(0.0, final - td)
            offsets.append(off)
            final = final + durations[k + 1] - td

        vf = []
        prev_label = "0:v"
        for k in range(n - 1):
            trans = self.clips[k].transition_to_next or "fade"
            out_label = f"vx{k}" if k < n - 2 else "vout"
            left = prev_label
            vb = f"{k+1}:v"
            vf.append(
                f"[{left}][{vb}]xfade=transition={trans}:duration={trans_durations[k]:.3f}"
                f":offset={offsets[k]:.3f}[{out_label}]"
            )
            prev_label = out_label

        # Audio: acrossfade (drop if any clip lacks audio).
        audio_present = all(probe.audio_stream(probe.probe(c)) for c in clips)
        af_args = []
        if audio_present and n > 1:
            af = []
            prev_a = "0:a"
            for k in range(n - 1):
                td = trans_durations[k]
                ol = f"ax{k}" if k < n - 2 else "aout"
                ab = f"{k+1}:a"
                af.append(f"[{prev_a}][{ab}]acrossfade=d={td:.3f}[{ol}]")
                prev_a = ol
            af_args = ["-filter_complex", ";".join(af), "-map", "[aout]"]
        elif audio_present:
            af_args = ["-map", "0:a"]

        vf_cmd = ";".join(vf) if vf else "0:v"
        base = [ff, "-hide_banner", "-y"]
        for c in clips:
            base += ["-i", c]
        base += ["-filter_complex", vf_cmd, "-map", "[vout]"]
        base += af_args
        base += self.preset.ffmpeg_args()
        base += ["-movflags", "+faststart", dst]
        util.run(base)


def _atempo_filter(speed: float) -> str:
    """Return an atempo filter (or chain) for the given speed."""
    # atempo supports 0.5–100 per filter; chain for out-of-range values.
    if speed <= 2.0 and speed >= 0.5:
        return f"atempo={speed:.6f}"
    # Decompose as repeated factors of 2/0.5 within bounds.
    filters = []
    s = speed
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        filters.append("atempo=0.5")
        s *= 2.0
    filters.append(f"atempo={s:.6f}")
    return ",".join(filters)
