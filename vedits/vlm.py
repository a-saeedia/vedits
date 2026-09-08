"""Video understanding: give vedits eyes.

Pattern: FFmpeg samples timestamped frames -> a vision model describes and
scores each scene. Works with any vision-capable provider, all free-tier:

  gemini     GEMINI_API_KEY        gemini-2.5-flash         (AI Studio, 20 free req/day)
  openrouter OPENROUTER_API_KEY    google/gemini-2.5-flash:free       ($0 variant)

NOTE (2026-09): GitHub Models was RETIRED 2026-07-30 (docs.github.com/github-models)
— its Azure inference endpoint no longer exists and PATs are useless for it.

Provider selection (first key found, override via VEDITS_VLM_PROVIDER):
  openrouter > gemini   (higher free quota first)

Output: a "scene map" — [{start, end, description, interest (0..1), caption}]
used by the pipeline to curate: keep only high-interest windows, auto-caption.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Dict, List, Optional

from . import util

ENV_PATH = os.environ.get("VEDITS_LLM_ENV", r"C:\Users\User\tools\litellm\.env")
PROVIDER_PREF = ["openrouter", "gemini"]

SCENE_PROMPT = (
    "These are frames sampled equally from a video file; each frame is tagged "
    "[frame at t=Ns]. Watch them in order like a video. Return STRICT JSON only, "
    "one object per visually distinct scene, no prose, no code fence:\n"
    '[{"start":0.0,"end":5.0,"description":"what is in the scene, 5-15 words",'
    '"interest":0.0,"caption":"suggested short burned caption for social media"}]\n'
    "interest = 0..1 how watchable/engaging this scene is for a TikTok/Reels audience. "
    "Use the frame timestamps to infer start/end boundaries."
)


# ------------------------------------------------------------------ keys
def _load_env() -> Dict[str, str]:
    keys: Dict[str, str] = {}
    if os.path.isfile(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8", errors="ignore"):
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(\S+)", line)
            if m and len(m.group(2).strip().strip('"').strip("'")) > 8:
                keys[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return keys


def pick_provider() -> Optional[str]:
    """Choose a provider whose key is present. openrouter > gemini."""
    keys = _load_env()
    want = os.environ.get("VEDITS_VLM_PROVIDER", "").strip().lower()
    if want:
        return want if want in PROVIDER_PREF and _provider_key(want, keys) else None
    for p in PROVIDER_PREF:
        if _provider_key(p, keys):
            return p
    return None


def _provider_key(provider: str, keys: Dict[str, str]) -> Optional[str]:
    return {
        "gemini": keys.get("GEMINI_API_KEY"),
        "openrouter": keys.get("OPENROUTER_API_KEY"),
    }.get(provider)


# ------------------------------------------------------------------ frame sampling
def extract_frames(path: str, max_frames: int = 24, width: int = 320,
                   out_dir: Optional[str] = None) -> List[tuple]:
    """Sample <=max_frames timestamped frames as small jpgs.

    Returns [(timestamp_seconds, frame_path), ...].
    """
    ff = util.resolve_ffmpeg()
    tmp = out_dir or tempfile.mkdtemp(prefix="vedits_frames_")
    # 1 frame per step seconds, then cap with -frames:v
    proc = subprocess.run(
        [ff, "-hide_banner", "-nostats", "-y", "-i", path,
         "-vf", f"fps=1/{max(1, _duration(path) / max_frames):.3f},scale={width}:-1",
         "-frames:v", str(max_frames), "-q:v", "6",
         os.path.join(tmp, "f_%03d.jpg")],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"frame extraction failed: {proc.stderr[-400:]}")
    frames = sorted(f for f in os.listdir(tmp) if f.endswith(".jpg"))
    dur = _duration(path)
    step = dur / max(len(frames), 1)
    return [(min(i * step, dur), os.path.join(tmp, f)) for i, f in enumerate(frames)]


def _duration(path: str) -> float:
    from . import probe
    info = probe.probe(path)
    return probe.duration(info) or 0.0


# ------------------------------------------------------------------ vision calls
def _call(provider: str, api_key: str, frames: List[tuple], timeout: int = 180) -> str:
    """Send frames to the provider; returns raw model text."""
    if provider == "gemini":
        return _call_gemini(api_key, frames, timeout)
    if provider == "openrouter":
        return _call_openai_like("https://openrouter.ai/api/v1/chat/completions",
                                 api_key, "google/gemini-2.5-flash:free", frames, timeout)
    raise ValueError(f"unknown provider {provider}")


def _call_gemini(key: str, frames: List[tuple], timeout: int) -> str:
    parts = []
    for t, fp in frames:
        with open(fp, "rb") as f:
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(f.read()).decode()}})
        parts.append({"text": f"[frame at t={t:.0f}s]"})
    parts.append({"text": SCENE_PROMPT})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048}}
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        f"?key={key}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    return "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])


def _call_openai_like(url: str, key: str, model: str, frames: List[tuple], timeout: int) -> str:
    content = [{"type": "text", "text": SCENE_PROMPT}]
    for t, fp in frames:
        with open(fp, "rb") as f:
            content.append({"type": "text", "text": f"[frame at t={t:.0f}s]"})
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()}})
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "max_tokens": 1024, "temperature": 0.1}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    return resp["choices"][0]["message"]["content"]


# ------------------------------------------------------------------ parsing
def _extract_json(text: str) -> List[dict]:
    """Pull the first JSON array out of a model reply (tolerates prose/fences)."""
    m = re.search(r"(\[.*\])", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON array in model reply: {text[:200]}")
    data = json.loads(m.group(1))
    out = []
    for d in data if isinstance(data, list) else [data]:
        try:
            out.append({
                "start": float(d.get("start", 0)),
                "end": float(d.get("end", 0)),
                "description": str(d.get("description", "")),
                "interest": float(d.get("interest", 0.5)),
                "caption": str(d.get("caption", "")),
            })
        except (TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ API
def analyze_video(path: str, provider: Optional[str] = None,
                  max_frames: int = 24, retries: int = 4) -> dict:
    """Understand a video: sample frames -> vision model -> scene map.

    Falls back across providers (github > openrouter > gemini) when a
    configured key is dead (401/403) or the free quota is exhausted (429).
    """
    keys = _load_env()
    chain = [provider] if provider else PROVIDER_PREF[:]
    chain = [p for p in chain if _provider_key(p, keys)]
    if not chain:
        raise RuntimeError(
            "no vision provider configured. Set one key in " + ENV_PATH +
            " : GEMINI_API_KEY or OPENROUTER_API_KEY"
            + (f"\n(requested provider {provider!r} missing)" if provider else ""))
    errors = []
    for prov in chain:
        try:
            return _analyze_with(prov, _provider_key(prov, keys), path, max_frames, retries)
        except urllib.error.HTTPError as e:
            errors.append(f"{prov}: HTTP {e.code}")
            if e.code in (401, 403, 429):
                continue  # dead key or quota -> try next provider
            raise
        except Exception as e:
            errors.append(f"{prov}: {e}")
            continue
    raise RuntimeError(f"vision analysis failed. tried: {'; '.join(errors)}")


def _analyze_with(provider: str, key: str, path: str, max_frames: int, retries: int) -> dict:
    frames = extract_frames(path, max_frames=max_frames)
    last = None
    for attempt in range(retries):
        try:
            text = _call(provider, key, frames, timeout=180)
            scenes = _extract_json(text)
            if not scenes:
                raise ValueError("empty scene map")
            return {"provider": provider, "video": path, "frames": len(frames),
                    "scenes": scenes}
        except urllib.error.HTTPError as e:
            raise  # let analyze_video decide on fallback
        except Exception as e:
            last = repr(e)
    raise RuntimeError(f"vision analysis failed ({provider}): {last}")


def curate(scene_map: dict, max_scenes: int = 3, min_dur: float = 2.0,
           interest_threshold: float = 0.4) -> List[dict]:
    """Pick the best scenes for shorts: high interest, sane min length."""
    good = [s for s in scene_map["scenes"]
            if s["end"] - s["start"] >= min_dur and s["interest"] >= interest_threshold]
    good.sort(key=lambda s: s["interest"], reverse=True)
    return good[:max_scenes]


# CLI entry
def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: vedits vlm <video> [--json] [--provider gemini|github|openrouter]")
        return 2
    path = argv[0]
    as_json = "--json" in argv
    provider = None
    if "--provider" in argv:
        provider = argv[argv.index("--provider") + 1]
    try:
        res = analyze_video(path, provider=provider)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    picks = curate(res)
    print(f"provider: {res['provider']}  frames: {res['frames']}  scenes: {len(res['scenes'])}")
    for s in res["scenes"]:
        star = "*" if s in picks else " "
        print(f" {star} {s['start']:6.1f}-{s['end']:6.1f}s  interest {s['interest']:.2f}"
              f"  {s['description'][:60]}")
    if picks:
        print("\ncurated picks for shorts:")
        for s in picks:
            print(f"   {s['start']:.1f}-{s['end']:.1f}s  caption: {s['caption']}")
    return 0