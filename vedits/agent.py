"""Agent engine: a Gemini-powered editing agent that drives vedits tools.

The agent reads the Gemini key from the existing litellm .env (never
hardcoded), calls Gemini with function declarations, executes the tools
locally on this machine (probe / list / smart-cut / render), and loops
until it has a final answer.

This is the "agent in the app": the GUI console and any external caller
talk to it, and it drives the deep backend (FFmpeg pipeline).
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Dict, List, Optional

from . import probe, util
from .pipeline import Pipeline
from .presets import PRESETS
from .project import Project, ProjectClip

ENV_PATH = os.environ.get("VEDITS_LLM_ENV", r"C:\Users\User\tools\litellm\.env")
DEFAULT_MODEL = "gemini-2.5-flash"
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = """You are Vedits, the video-editing agent embedded in a local app. \
You turn natural-language requests into finished social-media videos using tools \
that run FFmpeg on the local machine.

Rules:
- Prefer real tools over guessing. probe_file before building any project.
- For 'make me shorts from X', call smart_cut with sensible defaults \
(min_dur 3, max_dur 45, captions true for a reel).
- If a path is not given and assets/ exists, list_media there.
- Confirm output paths when a tool returns them. Be concise: 2-3 sentences.
- Never fabricate file paths or metadata.
"""

TOOL_DEFS = [
    {
        "name": "list_media",
        "description": "List video files (.mp4/.mov/.mkv/.webm) in a directory on this machine.",
        "parameters": {
            "type": "object",
            "properties": {"directory": {"type": "string", "description": "absolute path, e.g. C:\\\\Users\\\\User\\\\Desktop\\\\Vedit\\\\assets"}},
            "required": ["directory"],
        },
    },
    {
        "name": "probe_file",
        "description": "Probe a video file: resolution, duration, fps, codecs.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "smart_cut",
        "description": "Split a long video into platform-ready shorts at scene changes. Runs the full pipeline (analyze->plan->render->deliver).",
        "parameters": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "source video path"},
                "out_dir": {"type": "string", "description": "output dir (default: outputs/shorts/<name>)"},
                "preset": {"type": "string", "description": "preset name", "enum": list(PRESETS.keys())},
                "min_dur": {"type": "number", "description": "minimum clip length seconds"},
                "max_dur": {"type": "number", "description": "maximum clip length seconds"},
                "captions": {"type": "boolean", "description": "burn numbered captions onto shorts"},
            },
            "required": ["src"],
        },
    },
    {
        "name": "render_project",
        "description": "Render a project JSON document (clips, captions, transitions, preset) to an mp4.",
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "object", "description": "{preset, fps, output, clips:[{src,start,end,speed,text,text_size,text_color,transition_to_next,transition_duration}]}"}
            },
            "required": ["project"],
        },
    },
    {
        "name": "read_pipeline_report",
        "description": "Read the report.json produced by smart_cut (list of output files).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "path to report.json"}},
            "required": ["path"],
        },
    },
]


class AgentToolError(Exception):
    pass


def _load_gemini_key() -> Optional[str]:
    if os.path.isfile(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8", errors="ignore"):
            m = re.match(r"\s*GEMINI_API_KEY\s*=\s*(\S+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return os.environ.get("GEMINI_API_KEY")


# --------------------------------------------------------------------------
# Tool implementations (run on the local machine)
# --------------------------------------------------------------------------
TOOL_FUNCS: Dict[str, Callable[..., object]] = {}


def _register(fn=None, name: str = None):
    def deco(f):
        TOOL_FUNCS[name or f.__name__] = f
        return f
    return deco(fn) if fn else deco


@_register
def list_media(directory: str = "assets") -> dict:
    if not os.path.isabs(directory):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        directory = os.path.join(base, directory)
    files = [f for f in os.listdir(directory)
             if f.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))]
    return {"directory": directory, "files": files}


@_register
def probe_file(path: str) -> dict:
    if not os.path.isfile(path):
        raise AgentToolError(f"no such file: {path}")
    info = probe.probe(path)
    return {
        "path": path,
        "width": probe.dimensions(info)[0],
        "height": probe.dimensions(info)[1],
        "duration_s": round(probe.duration(info), 2),
        "fps": round(probe.frame_rate(info), 2),
        "video_codec": probe.codecs(info).get("video"),
        "audio_codec": probe.codecs(info).get("audio"),
    }


@_register
def smart_cut(src: str, out_dir: str = None, preset: str = "tiktok_1080",
              min_dur: float = 3.0, max_dur: float = 45.0,
              captions: bool = True) -> dict:
    if not os.path.isfile(src):
        raise AgentToolError(f"no such file: {src}")
    if preset not in PRESETS:
        raise AgentToolError(f"unknown preset {preset}; known: {sorted(PRESETS)}")
    out_dir = out_dir or os.path.join("outputs", "shorts",
                                      os.path.splitext(os.path.basename(src))[0])
    if not os.path.isabs(out_dir):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out_dir = os.path.join(base, out_dir)
    report = Pipeline().smart_cut(src, out_dir, preset=preset,
                                  min_dur=min_dur, max_dur=max_dur, captions=captions)
    return {"summary": report.summary, "outputs": report.outputs,
            "report": os.path.join(out_dir, "report.json")}


@_register
def render_project(project: dict) -> dict:
    output = project.get("output") or os.path.join("outputs", f"agent_{int(__import__('time').time())}.mp4")
    if not os.path.isabs(output):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output = os.path.join(base, output)
    preset = project.get("preset", "tiktok_1080")
    if preset not in PRESETS:
        raise AgentToolError(f"unknown preset {preset}; known: {sorted(PRESETS)}")
    try:
        proj = Project.from_dict(project, output=output, preset=preset)
    except Exception as e:
        raise AgentToolError(f"bad project: {e}")
    os.makedirs(os.path.dirname(output), exist_ok=True)
    proj.render(work_dir=os.path.join(os.path.dirname(output), ".work"))
    return {"output": output,
            "duration_s": round(probe.duration(probe.probe(output)), 2)}


@_register
def read_pipeline_report(path: str) -> dict:
    if not os.path.isfile(path):
        raise AgentToolError(f"no such file: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


NONE_MARKER = {"value": None}


def run_tool(name: str, args: dict) -> tuple:
    """Execute an agent tool locally. Returns (ok, result)."""
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return False, {"error": f"unknown tool {name}"}
    try:
        res = fn(**args)
        return True, (res if res is not None else NONE_MARKER)
    except AgentToolError as e:
        return False, {"error": str(e)}
    except Exception as e:  # surface everything, agent can retry differently
        return False, {"error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# Gemini function-calling loop
# --------------------------------------------------------------------------
def _call_gemini(key: str, model: str, contents: list, tools: list) -> dict:
    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "tools": [{"functionDeclarations": tools}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    import urllib.request
    req = urllib.request.Request(
        API.format(model=model) + f"?key={key}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"gemini HTTP {e.code}: {e.read().decode()[:400]}")


def _part_text(content) -> str:
    return "".join(p.get("text", "") for p in content.get("parts", []))


class VeditsAgent:
    """Tool-using editing agent. Exposes a chat() loop returning text + step log."""

    def __init__(self, model: str = DEFAULT_MODEL, on_step: Optional[Callable[[str], None]] = None):
        self.model = model
        self.key = _load_gemini_key()
        if not self.key:
            raise RuntimeError("GEMINI_API_KEY not found in " + ENV_PATH)
        self.on_step = on_step or (lambda s: None)

    def _log(self, s: str):
        self.on_step(s)

    def chat(self, user_text: str, history: Optional[List[dict]] = None,
             max_turns: int = 12) -> dict:
        contents = [{"role": "user", "parts": [{"text": user_text}]}]
        steps: List[str] = []
        self._log(f"agent: {user_text}")
        for turn in range(max_turns):
            resp = _call_gemini(self.key, self.model, contents, TOOL_DEFS)
            if "error" in resp:
                raise RuntimeError(f"gemini error: {resp['error']}")
            candidates = resp.get("candidates") or []
            if not candidates:
                raise RuntimeError("gemini: no candidates")
            content = candidates[0].get("content", {})
            text = _part_text(content)
            calls = [p for p in content.get("parts", []) if "functionCall" in p]

            if not calls:
                reply = text or "done."
                self._log(f"agent reply: {reply[:80]}")
                return {"reply": reply, "steps": steps, "turns": turn + 1}

            # execute each requested tool, feed back as functionResponse parts
            resp_parts = []
            for call in calls:
                fc = call["functionCall"]
                name, args = fc.get("name", ""), fc.get("args", {}) or {}
                self._log(f"tool_call: {name}({json.dumps(args)[:120]})")
                ok, result = run_tool(name, args)
                steps.append({"tool": name, "args": args, "ok": ok,
                              "result": result if ok else {"error": result.get("error", "?")}})
                self._log(f"  -> {'ok' if ok else 'ERROR'}: {json.dumps(result)[:160]}")
                resp_parts.append({
                    "functionResponse": {
                        "name": name,
                        "response": {"name": name, "content": result},
                    }
                })
            contents.append(content)
            contents.append({"role": "user", "parts": resp_parts})
        raise RuntimeError(f"agent: exceeded {max_turns} tool turns without finishing")