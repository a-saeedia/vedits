"""Social media platform presets: resolution, aspect, fps, bitrates, codec."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class Preset:
    name: str
    width: int
    height: int
    fps: int
    video_bitrate: str       # e.g. "4M"
    audio_bitrate: str       # e.g. "192k"
    video_codec: str         # libx264 / libx265 / libsvtav1
    preset: str              # x264 preset / svt-av1 preset
    pix_fmt: str = "yuv420p"
    audio_codec: str = "aac"
    profile: Optional[str] = None
    maxrate: Optional[str] = None
    bufsize: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)

    def vertical(self) -> "Preset":
        """Return a copy with width/height swapped for vertical video."""
        return Preset(
            name=self.name,
            width=self.height,
            height=self.width,
            fps=self.fps,
            video_bitrate=self.video_bitrate,
            audio_bitrate=self.audio_bitrate,
            video_codec=self.video_codec,
            preset=self.preset,
            pix_fmt=self.pix_fmt,
            audio_codec=self.audio_codec,
            profile=self.profile,
            maxrate=self.maxrate,
            bufsize=self.bufsize,
            extra=dict(self.extra),
        )

    def ffmpeg_args(self, *, crf: Optional[int] = None, audio: bool = True) -> list[str]:
        """Build output codec args. crf overrides bitrate-based rate control."""
        args: list[str] = []
        args += ["-c:v", self.video_codec]

        if self.video_codec == "libsvtav1":
            args += ["-preset", self.preset]
            if crf is not None:
                args += ["-crf", str(crf)]
            else:
                args += ["-b:v", self.video_bitrate]
        elif self.video_codec in ("libx264", "libx265"):
            args += ["-preset", self.preset]
            if self.profile:
                args += ["-profile:v", self.profile]
            if crf is not None:
                args += ["-crf", str(crf)]
            else:
                args += ["-b:v", self.video_bitrate]
            if self.maxrate and self.bufsize:
                args += ["-maxrate", self.maxrate, "-bufsize", self.bufsize]
        else:
            if crf is not None:
                args += ["-crf", str(crf)]
            else:
                args += ["-b:v", self.video_bitrate]

        args += ["-pix_fmt", self.pix_fmt, "-r", str(self.fps)]
        if audio:
            args += ["-c:a", self.audio_codec, "-b:a", self.audio_bitrate]
        for k, v in self.extra.items():
            args += [k, v]
        return args


# Registered presets keyed by name.
PRESETS: Dict[str, Preset] = {}

def _reg(p: Preset):
    PRESETS[p.name] = p
    return p

# --- Vertical (9:16) short-form platforms -----------------------------------
_reg(Preset("tiktok_1080",     1080, 1920, 30, "6M",  "192k", "libx264", "veryfast", profile="high", maxrate="8M", bufsize="12M"))
_reg(Preset("reels_1080",      1080, 1920, 30, "6M",  "192k", "libx264", "veryfast", profile="high", maxrate="8M", bufsize="12M"))
_reg(Preset("shorts_1080",     1080, 1920, 30, "6M",  "192k", "libx264", "veryfast", profile="high", maxrate="8M", bufsize="12M"))
# Lower-res / faster vertical for quick uploads.
_reg(Preset("vertical_720",     720, 1280, 30, "3.5M", "160k", "libx264", "veryfast", profile="high", maxrate="5M", bufsize="8M"))
# SVT-AV1 vertical — smaller files, slower to encode.
_reg(Preset("vertical_av1",    720, 1280, 30, "2M",  "160k", "libsvtav1", "6", pix_fmt="yuv420p10le", maxrate="3M", bufsize="6M"))

# --- Horizontal (16:9) long-form -------------------------------------------
_reg(Preset("youtube_1080",    1920, 1080, 30, "8M",  "192k", "libx264", "medium", profile="high", maxrate="12M", bufsize="16M"))
_reg(Preset("youtube_720",     1280, 720,  30, "5M",  "192k", "libx264", "medium", profile="high", maxrate="7M", bufsize="10M"))
_reg(Preset("landscape_1080",  1920, 1080, 30, "6M",  "192k", "libx264", "veryfast", profile="high", maxrate="8M", bufsize="12M"))
_reg(Preset("landscape_720",   1280, 720,  30, "4M",  "192k", "libx264", "veryfast", profile="high", maxrate="5M", bufsize="8M"))

# --- Square (for some ads / IG posts) --------------------------------------
_reg(Preset("square_1080",     1080, 1080, 30, "5M",  "192k", "libx264", "veryfast", profile="high", maxrate="7M", bufsize="10M"))
_reg(Preset("square_720",      720,  720,  30, "3.5M", "160k", "libx264", "veryfast", profile="high", maxrate="5M", bufsize="8M"))


def get(name: str) -> Preset:
    """Look up a preset by name; raise KeyError with the valid list if missing."""
    p = PRESETS.get(name)
    if p is None:
        raise KeyError(f"Unknown preset '{name}'. Available: {', '.join(sorted(PRESETS))}")
    return p


def list_presets() -> list[str]:
    return sorted(PRESETS)
