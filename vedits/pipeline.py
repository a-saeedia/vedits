"""Pipeline engine: staged, observable video-production pipeline.

Stages:
  INGEST   - locate/collect source media
  ANALYZE  - probe media, detect scenes + silence (ffmpeg)
  PLAN     - build a Project (edit plan) from the analysis
  RENDER   - execute the edit -> output video(s)
  DELIVER  - verify outputs, write a report

Everything emits progress events so the GUI / agents can show live status.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import ops, probe, util
from .presets import PRESETS
from .project import Project, ProjectClip

ProgressFn = Callable[[str, str, str], None]  # (stage, message, detail)

STAGES = ["INGEST", "ANALYZE", "PLAN", "RENDER", "DELIVER"]


@dataclass
class PipelineReport:
    stages: Dict[str, str] = field(default_factory=dict)   # stage -> status
    outputs: List[str] = field(default_factory=list)
    log: List[Dict[str, str]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "stages": self.stages,
            "outputs": self.outputs,
            "log": self.log,
            "summary": self.summary,
        }


class Pipeline:
    def __init__(self, progress: Optional[ProgressFn] = None):
        self.progress = progress or (lambda *a: None)
        self.report = PipelineReport()

    def _emit(self, stage: str, message: str, detail: str = ""):
        self.report.stages[stage] = "running"
        self.report.log.append({"stage": stage, "message": message, "detail": detail})
        self.progress(stage, message, detail)

    def _done(self, stage: str):
        self.report.stages[stage] = "done"

    # ------------------------------------------------------------------ API
    def smart_cut(self, src: str, out_dir: str, preset: str = "tiktok_1080",
                  scene_threshold: float = 0.30,
                  min_dur: float = 3.0, max_dur: float = 30.0,
                  captions: bool = False,
                  curate: bool = False,
                  max_scenes: int = 3,
                  interest_threshold: float = 0.4) -> PipelineReport:
        """Split a long video into platform-ready shorts at scene changes.

        curate=True: sends sampled frames to a vision model (vedits/vlm) and
        keeps only the high-interest scenes, with model-suggested captions.

        Full pipeline: INGEST -> ANALYZE -> PLAN -> RENDER -> DELIVER.
        """
        os.makedirs(out_dir, exist_ok=True)

        # ---- INGEST
        self._emit("INGEST", f"ingesting {os.path.basename(src)}")
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        info = probe.probe(src)
        total = probe.duration(info)
        w, h = probe.dimensions(info)
        self._emit("INGEST", f"source {w}x{h}, {total:.1f}s")
        self._done("INGEST")

        # ---- ANALYZE
        self._emit("ANALYZE", "detecting scene changes (ffmpeg select=gt(scene,…))")
        cuts = analyze_scenes(src, scene_threshold)
        self._emit("ANALYZE", f"found {len(cuts)} scene boundaries: "
                              + ", ".join(f"{c:.1f}s" for c in cuts[:8]) + ("…" if len(cuts) > 8 else ""))
        self._done("ANALYZE")

        # ---- PLAN
        if curate:
            self._emit("PLAN", "vision-curating scenes (frame sampling -> vision model)")
            from . import vlm
            scene_map = vlm.analyze_video(src)
            picked = vlm.curate(scene_map, max_scenes=max_scenes,
                                min_dur=min_dur, interest_threshold=interest_threshold)
            if not picked:
                self._emit("PLAN", "no scene passed curation threshold; falling back to boundary cut")
                picked = None
            if picked:
                windows = [(round(s["start"], 2), round(s["end"], 2)) for s in picked]
                captions_by_idx = {round(s["start"], 1): s.get("caption", "") for s in picked}
                self._emit("PLAN", f"curated {len(windows)} best scenes: "
                                   + ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in windows))
            else:
                windows = plan_windows(analyze_scenes(src, scene_threshold), total,
                                       min_dur, max_dur)
                captions_by_idx = {}
        else:
            windows = plan_windows(cuts, total, min_dur, max_dur)
            captions_by_idx = {}
        self._emit("PLAN", f"planned {len(windows)} shorts: "
                           + ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in windows[:6])
                           + ("…" if len(windows) > 6 else ""))
        self._done("PLAN")

        # ---- RENDER
        outputs: List[str] = []
        for i, (a, b) in enumerate(windows):
            out = os.path.join(out_dir, f"short_{i+1:02d}.mp4")
            self._emit("RENDER", f"rendering short {i+1}/{len(windows)}  {a:.1f}-{b:.1f}s")
            cap_text = None
            if captions:
                cap_text = captions_by_idx.get(round(a, 1)) or ("Part %d" % (i + 1))
            clip = ProjectClip(
                id=f"s{i}", src=src, start=a, end=b,
                text=cap_text,
                text_size=64,
            )
            proj = Project(clips=[clip], preset=preset, output=out, fps=30)
            proj.render(work_dir=os.path.join(out_dir, ".work"))
            outputs.append(out)
            self._emit("RENDER", f"short {i+1} done -> {os.path.basename(out)}")
        self._done("RENDER")

        # ---- DELIVER
        self._emit("DELIVER", f"{len(outputs)} shorts produced in {out_dir}")
        report_path = os.path.join(out_dir, "report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({"preset": preset, "source": src, "outputs": outputs,
                       "windows": [[a, b] for a, b in windows]}, f, indent=2)
        self.report.outputs = outputs
        self.report.summary = f"{len(outputs)} shorts ready: " + ", ".join(os.path.basename(o) for o in outputs)
        self._done("DELIVER")
        return self.report

    # convenience: whole-file processing for the agent
    def batch_shorts(self, src_dir: str, out_dir: str, preset: str = "tiktok_1080",
                     **kw) -> PipelineReport:
        """Run smart_cut on every video file in a directory (agent-friendly)."""
        self._emit("INGEST", f"scanning {src_dir}")
        videos = [os.path.join(src_dir, f) for f in sorted(os.listdir(src_dir))
                  if f.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))]
        if not videos:
            raise FileNotFoundError(f"no videos in {src_dir}")
        self.report.outputs = []
        for v in videos:
            self._emit("INGEST", f"processing {os.path.basename(v)}")
            self.smart_cut(v, os.path.join(out_dir, os.path.splitext(os.path.basename(v))[0]), preset, **kw)
        self.report.summary = f"processed {len(videos)} files -> {len(self.report.outputs)} shorts"
        return self.report


# ============================================================================
# Scene/silence analysis (ffmpeg-level, "deep backend")
# ============================================================================
def analyze_scenes(path: str, threshold: float = 0.30) -> List[float]:
    """Detect scene-change timestamps using ffmpeg's scene score.

    Uses select=gt(scene,thr) + showinfo, and parses pts_time from stderr.
    Returns a sorted list of cut times in seconds.
    """
    ff = util.resolve_ffmpeg()
    proc = util.run(
        [ff, "-hide_banner", "-i", path,
         "-vf", f"select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"],
        check=False,
    )
    cuts: List[float] = []
    for line in proc.stdout.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            t = float(m.group(1))
            if t > 0.05:
                cuts.append(t)
    cuts.sort()
    return cuts


def analyze_silence(path: str, threshold: float = -35.0, min_dur: float = 0.8) -> List[tuple]:
    """Detect silence windows using ffmpeg silencedetect. Returns [(start, end), …]."""
    ff = util.resolve_ffmpeg()
    proc = util.run(
        [ff, "-hide_banner", "-i", path, "-af",
         f"silencedetect=noise={threshold}dB:d={min_dur}", "-f", "null", "-"],
        check=False,
    )
    out: List[tuple] = []
    cur = None
    for line in proc.stdout.splitlines():
        m = re.search(r"silence_(start|end):\s*([0-9.]+)", line)
        if m:
            if m.group(1) == "start":
                cur = float(m.group(2))
            elif m.group(1) == "end" and cur is not None:
                out.append((cur, float(m.group(2))))
                cur = None
    return out


def plan_windows(cuts: List[float], total: float, min_dur: float = 3.0,
                 max_dur: float = 30.0) -> List[tuple]:
    """Turn scene boundaries into non-overlapping [start, end] windows.

    - Merge boundaries closer than min_dur (avoid slivers).
    - Split windows longer than max_dur at the nearest boundary.
    - Drains gaps shorter than 0.4s.
    """
    if not cuts:
        cuts = [total / 2]
    bounds = [round(t, 2) for t in cuts if 0.5 < t < total - 0.5]
    if not bounds:
        bounds = [round(total / 2, 2)]

    # merge slivers: drop boundaries within min_dur of the previous kept one
    kept = []
    for b in bounds:
        if kept and b - kept[-1] < min_dur:
            kept[-1] = b  # extend the previous boundary
        else:
            kept.append(b)
    bounds = kept

    # build windows between consecutive boundaries + edges
    pts = [0.0] + bounds + [total]
    windows: List[tuple] = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        dur = b - a
        if dur < 0.5:
            continue
        # clamp to max_dur by splitting at the boundary-following point
        while dur > max_dur:
            windows.append((round(a, 2), round(a + max_dur, 2)))
            a += max_dur
            dur = b - a
        windows.append((round(a, 2), round(b, 2)))
    return windows