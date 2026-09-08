"""Text overlays: burn-in titles and captions using FFmpeg drawtext filter.

Note: drawtext needs a font file. On Windows we use the Segoe UI / Arial that
always ship with the OS (C:/Windows/Fonts). The text filter escapes quotes,
colons, commas, apostrophes and percent signs which are drawtext-special.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import util

# Default Windows fonts (checked in order, first existing wins).
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
]
_default_font: Optional[str] = None


def default_font() -> str:
    global _default_font
    if _default_font:
        return _default_font
    import os
    for cand in _FONT_CANDIDATES:
        if os.path.isfile(cand):
            _default_font = cand.replace("\\", "/")
            return _default_font
    return "Sans"


def _escape(text: str) -> str:
    """Escape drawtext-special characters in user text."""
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    text = text.replace(",", "\\,")
    text = text.replace("%", "\\%")
    text = text.replace(";", "\\;")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace("(", "\\(")
    text = text.replace(")", "\\)")
    return text


@dataclass
class TextOverlay:
    text: str
    # x/y accept numbers, or expressions like "(w-text_w)/2", "w-tw-40".
    x: str = "(w-text_w)/2"
    y: str = "h-th-60"
    fontsize: int = 48
    fontcolor: str = "white"
    box: bool = True
    boxcolor: str = "black@0.55"
    boxborderw: int = 14
    font: Optional[str] = None
    enable: Optional[str] = None   # e.g. "between(t,0,3)"

    def filter(self) -> str:
        font = (self.font or default_font()).replace("\\", "/")
        # Escape the colon in a Windows drive path (C:/...) so drawtext's
        # `:` option separator doesn't split it, and wrap in quotes for safety.
        font_esc = font.replace(":", "\\:")
        parts = [
            f"text='{_escape(self.text)}'",
            f"x={self.x}",
            f"y={self.y}",
            f"fontsize={self.fontsize}",
            f"fontcolor={self.fontcolor}",
            f"fontfile='{font_esc}'",
        ]
        if self.box:
            parts.append(f"box=1:boxcolor={self.boxcolor}:boxborderw={self.boxborderw}")
        if self.enable:
            parts.append(f"enable='{self.enable}'")
        return "drawtext=" + ":".join(parts)


def burn_text_filter(overlays: List[TextOverlay]) -> str:
    """Join multiple overlays into a single -vf filter string."""
    return ",".join(o.filter() for o in overlays)
