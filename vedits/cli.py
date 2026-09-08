"""Vedits CLI — zero-budget social media video editing on FFmpeg.

Usage examples:
  vedits info assets/sample.mp4
  vedits presets
  vedits fit assets/sample.mp4 out/t.mp4 --preset tiktok_1080
  vedits trim assets/sample.mp4 out/t.mp4 --start 1 --end 4
  vedits caption assets/sample.mp4 out/t.mp4 --text "Hello world" --preset tiktok_1080
  vedits make --preset reels_1080 --out out/mix.mp4 assets/a.mp4 assets/b.mp4
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import ops, probe, util
from .presets import get as get_preset, PRESETS
from .text import TextOverlay, burn_text_filter
from .timeline import Segment, Timeline


def _eprint(*a):
    print(*a, file=sys.stderr)


def _out_path(dst: Optional[str], src: str, suffix: str = "_out") -> str:
    """Default output path from input path."""
    if dst:
        return dst
    root, ext = os.path.splitext(src)
    return root + suffix + ".mp4"


def cmd_info(args) -> int:
    p = args.path
    if os.path.isdir(p):
        for root, _, files in os.walk(p):
            for f in sorted(files):
                if f.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".avi")):
                    print(probe.summarize(os.path.join(root, f)))
        return 0
    print(probe.summarize(p))
    return 0


def cmd_presets(_args) -> int:
    print(f"{'preset':<16}{'size':>10}{'fps':>4}  {'codec':<9}{'bitrate':>8}")
    for name in sorted(PRESETS):
        p = PRESETS[name]
        print(f"{name:<16}{p.width}x{p.height:>6}{p.fps:>4}  {p.video_codec:<9}{p.video_bitrate:>8}")
    return 0


def cmd_fit(args) -> int:
    preset = get_preset(args.preset)
    dst = _out_path(args.out, args.input)
    ops.fit(args.input, dst, preset, mode=args.crop, crf=args.crf)
    print(f"wrote {dst}")
    return 0


def cmd_trim(args) -> int:
    dst = _out_path(args.out, args.input, "_trim")
    start = util.parse_timecode(args.start) if args.start else None
    end = util.parse_timecode(args.end) if args.end else None
    dur = util.parse_timecode(args.duration) if args.duration else None
    ops.trim(args.input, dst, start=start, end=end, duration=dur)
    print(f"wrote {dst}")
    return 0


def cmd_caption(args) -> int:
    preset = get_preset(args.preset)
    dst = _out_path(args.out, args.input, "_captioned")
    overlay = TextOverlay(
        text=args.text,
        fontsize=args.size,
        fontcolor=args.color,
        x=args.x or "(w-text_w)/2",
        y=args.y or "h-th-60",
        box=not args.no_box,
    )
    ops.fit(args.input, dst, preset, mode=args.crop,
            extra_filters=burn_text_filter([overlay]), crf=args.crf)
    print(f"wrote {dst}")
    return 0


def cmd_make(args) -> int:
    """Build a multi-clip video from a list of sources."""
    preset = get_preset(args.preset)
    if not args.inputs:
        _eprint("make requires at least one input clip")
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    timeline = Timeline(
        segments=[Segment(source=s) for s in args.inputs],
        preset=preset,
        output=args.out,
    )
    timeline.render()
    print(f"wrote {args.out}")
    return 0


def cmd_render_project(args) -> int:
    """Render a project JSON file (the format the GUI/API uses)."""
    import json
    from .project import Project
    from .presets import get as _get
    with open(args.project, "r", encoding="utf-8") as f:
        data = json.load(f)
    preset = _get(args.preset or data.get("preset", "tiktok_1080"))
    proj = Project.from_dict(data, args.out, preset)
    proj.render()
    print(f"wrote {args.out}  ({proj.total_duration():.2f}s)")
    return 0


def cmd_vlm(args) -> int:
    """Watch a video with a vision model: scene map + curated picks."""
    from . import vlm
    try:
        res = vlm.analyze_video(args.input, provider=args.provider, max_frames=args.frames)
    except RuntimeError as e:
        _eprint(f"error: {e}")
        return 1
    picks = vlm.curate(res)
    if args.json:
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"provider: {res['provider']}  frames: {res['frames']}  scenes: {len(res['scenes'])}")
    for s in res["scenes"]:
        star = "*" if s in picks else " "
        print(f" {star} {s['start']:6.1f}-{s['end']:6.1f}s  interest {s['interest']:.2f}  "
              + (s["description"] or "")[:60])
    if picks:
        print("\ncurated picks for shorts:")
        for s in picks:
            print(f"   {s['start']:.1f}-{s['end']:.1f}s  caption: {s['caption'] or '(none)'}")
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vedits", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="Show media metadata")
    p.add_argument("path")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("presets", help="List platform presets")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("fit", help="Reframe + re-encode to a platform preset")
    p.add_argument("input")
    p.add_argument("--out", "-o")
    p.add_argument("--preset", required=True, help="preset name, see 'presets'")
    p.add_argument("--crop", default=ops.CROP_CENTER, choices=[ops.CROP_CENTER, ops.CROP_LEFT, ops.CROP_RIGHT, ops.CROP_TOP, ops.CROP_BOTTOM])
    p.add_argument("--crf", type=int, default=None, help="override bitrate with CRF")
    p.set_defaults(func=cmd_fit)

    p = sub.add_parser("trim", help="Trim a video to a time range")
    p.add_argument("input")
    p.add_argument("--out", "-o")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--duration")
    p.set_defaults(func=cmd_trim)

    p = sub.add_parser("caption", help="Burn a text caption onto a video (fits preset)")
    p.add_argument("input")
    p.add_argument("--out", "-o")
    p.add_argument("--preset", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--size", type=int, default=48)
    p.add_argument("--color", default="white")
    p.add_argument("--x")
    p.add_argument("--y")
    p.add_argument("--no-box", action="store_true")
    p.add_argument("--crop", default=ops.CROP_CENTER, choices=[ops.CROP_CENTER, ops.CROP_LEFT, ops.CROP_RIGHT, ops.CROP_TOP, ops.CROP_BOTTOM])
    p.add_argument("--crf", type=int, default=None)
    p.set_defaults(func=cmd_caption)

    p = sub.add_parser("make", help="Concatenate multiple clips into one video")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--out", "-o", required=True)
    p.add_argument("--preset", required=True)
    p.set_defaults(func=cmd_make)

    p = sub.add_parser("render-project", help="Render a project JSON file to video")
    p.add_argument("project")
    p.add_argument("--out", "-o", required=True)
    p.add_argument("--preset", default=None, help="override preset from JSON")
    p.set_defaults(func=cmd_render_project)

    p = sub.add_parser("vlm", help="Watch a video (frame sampling -> vision model): scene map + curated picks")
    p.add_argument("input")
    p.add_argument("--provider", choices=["gemini", "github", "openrouter"], default=None,
                   help="default: first configured (github > openrouter > gemini)")
    p.add_argument("--frames", type=int, default=24, help="max sampled frames")
    p.add_argument("--json", action="store_true", help="raw scene map as JSON")
    p.set_defaults(func=cmd_vlm)

    p = sub.add_parser("serve", help="Launch the web editor GUI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8456)
    p.add_argument("--no-open", action="store_true", help="don't auto-open browser")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        _eprint(f"error: {e}")
        return 1
    except KeyError as e:
        _eprint(f"error: {e}")
        return 1
    except RuntimeError as e:
        _eprint(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
