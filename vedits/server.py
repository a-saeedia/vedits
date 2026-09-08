"""Stdlib HTTP server: serves the vedits web GUI and renders projects.

Endpoints:
  GET  /                  -> GUI (index.html)
  GET  /<static files>    -> css/js assets
  GET  /api/presets       -> JSON list of platform presets
  POST /api/render        -> body: { "preset": "...", "fps":30, "crop_mode":"center", "clips":[...] }
                              renders and returns the output path (relative to server root).
  GET  /outputs/<file>    -> download a rendered video

The render endpoint is agent-friendly: an agent (Hermes/OpenCode) can POST the
same project JSON the GUI builds and get back a finished video — no browser.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socketserver
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

from . import project as project_mod
from .presets import PRESETS

_GUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui")
_OUT_DIR = "outputs"
_MEDIA_DIR = "media"
_WATCH_DIR = "watch"
_AGENT_LOCK = threading.Lock()
_WATCH_STATE = {
    "folder": None, "preset": "tiktok_1080", "enabled": False,
    "seen": set(), "last": None, "processed": [], "running": False,
}


class VeditsServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threading HTTP server so renders don't block the whole editor."""
    daemon_threads = True
    allow_reuse_address = True


class VeditsHandler(http.server.BaseHTTPRequestHandler):
    server_version = "VeditsGUI/0.1"

    # ------------------------------------------------------------------ utils
    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------- dispatch
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            path = "/index.html"
        if path == "/api/presets":
            return self._json(200, {k: _preset_summary(v) for k, v in PRESETS.items()})
        if path == "/api/agent/tools":
            from .agent import TOOL_FUNCS, TOOL_DEFS
            return self._json(200, {
                "llm": "gemini-2.5-flash",
                "tools": [t["name"] for t in TOOL_DEFS],
                "implemented": sorted(TOOL_FUNCS),
            })
        if path == "/api/watch":
            return self._json(200, _watch_status())
        # Allow serving files only within gui/, media/, or outputs/.
        if path.startswith("/outputs/"):
            return self._serve_output(path)
        if path.startswith("/media/"):
            return self._serve_output(path, subdir=_MEDIA_DIR)
        return self._serve_gui(path)

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/api/render":
            return self._handle_render()
        if path == "/api/upload":
            return self._handle_upload()
        if path == "/api/agent/chat":
            return self._handle_agent()
        if path == "/api/pipeline/smartcut":
            return self._handle_smartcut()
        if path == "/api/watch":
            return self._handle_watch()
        self._json(404, {"error": "not found"})

    # ------------------------------------------------------------------ files
    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
    }

    def _safe_path(self, root: str, rel: str) -> Optional[str]:
        full = os.path.abspath(os.path.normpath(os.path.join(root, rel.lstrip("/"))))
        root_abs = os.path.abspath(root)
        if not full.startswith(root_abs + os.sep) and full != root_abs:
            return None
        if not os.path.isfile(full):
            return None
        return full

    def _serve_gui(self, path: str):
        full = self._safe_path(_GUI_DIR, path)
        if not full:
            # SPA fallback to index.
            full = os.path.join(_GUI_DIR, "index.html")
        if not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, self._MIME.get(ext, "application/octet-stream"))

    def _serve_output(self, path: str, subdir: str = _OUT_DIR):
        full = self._safe_path(subdir, path.split("/", 2)[2] if "/" in path else "")
        if not full:
            return self._json(404, {"error": "not found"})
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, self._MIME.get(ext, "application/octet-stream"))

    # ------------------------------------------------------------------- render
    def _handle_upload(self):
        """Upload a media file via multipart/form-data. Returns a server path.

        Body field 'file' -> stored under media/ with a safe name.
        """
        import cgi
        import uuid
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return self._json(400, {"error": "expected multipart/form-data"})
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
            },
        )
        item = form["file"]
        if not item.filename:
            return self._json(400, {"error": "missing 'file' part"})
        os.makedirs(_MEDIA_DIR, exist_ok=True)
        safe_ext = os.path.splitext(item.filename)[1].lower() or ".mp4"
        safe_ext = safe_ext if re.fullmatch(r"\.[a-z0-9]{1,5}", safe_ext) else ".mp4"
        name = f"{uuid.uuid4().hex[:10]}{safe_ext}"
        dest = os.path.join(_MEDIA_DIR, name)
        with open(dest, "wb") as f:
            f.write(item.file.read())
        return self._json(200, {
            "ok": True,
            "src": dest.replace("\\", "/"),
            "url": f"/media/{name}",
            "name": item.filename,
        })

    def _handle_render(self):
        data = self._read_body()
        preset_name = data.get("preset")
        if not preset_name:
            return self._json(400, {"error": "missing 'preset'", "available": sorted(PRESETS)})
        if preset_name not in PRESETS:
            return self._json(400, {"error": f"unknown preset '{preset_name}'", "available": sorted(PRESETS)})
        clips = data.get("clips")
        if not clips:
            return self._json(400, {"error": "missing 'clips'"})

        os.makedirs(_OUT_DIR, exist_ok=True)
        out_name = f"render_{int(time_now() * 1000)}.mp4"
        out_path = os.path.join(_OUT_DIR, out_name)

        try:
            proj = project_mod.Project.from_dict(data, out_path, preset_name)
            proj.render(work_dir=os.path.join(_OUT_DIR, ".work"))
        except Exception as e:  # surface to client for debuggability
            import traceback
            self._json(500, {"error": str(e), "trace": traceback.format_exc()})
            return

        self._json(200, {
            "ok": True,
            "output": f"/outputs/{out_name}",
            "duration": round(proj.total_duration(), 3),
            "size": os.path.getsize(out_path),
        })

    # ------------------------------------------------------------------ agent / pipeline
    def _handle_agent(self):
        """POST /api/agent/chat  { text }  -> { reply, steps, turns }"""
        data = self._read_body()
        text = (data.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "missing 'text'"})
        from .agent import VeditsAgent
        try:
            with _AGENT_LOCK:
                steps_buf = []

                def on_step(s):
                    steps_buf.append(s)

                result = VeditsAgent(on_step=on_step).chat(text)
            return self._json(200, {"ok": True, "reply": result["reply"],
                                    "steps": result["steps"], "log": steps_buf})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def _handle_smartcut(self):
        """POST /api/pipeline/smartcut
        { src, preset?, min_dur?, max_dur?, captions? } -> pipeline report
        """
        data = self._read_body()
        src = (data.get("src") or "").strip()
        if not src:
            return self._json(400, {"error": "missing 'src' (absolute path or relative to project root)"})
        preset = data.get("preset", "tiktok_1080")
        if preset not in PRESETS:
            return self._json(400, {"error": f"unknown preset", "available": sorted(PRESETS)})
        if not os.path.isabs(src):
            src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), src)
        from .pipeline import Pipeline
        try:
            name = os.path.splitext(os.path.basename(src))[0]
            out_dir = os.path.join(_OUT_DIR, "shorts", name)
            report = Pipeline(progress=lambda s, m, d: None).smart_cut(
                src, out_dir, preset=preset,
                min_dur=float(data.get("min_dur", 3)),
                max_dur=float(data.get("max_dur", 45)),
                captions=bool(data.get("captions", True)),
            )
            outputs = [o.replace("\\", "/") for o in report.outputs]
            return self._json(200, {
                "ok": True, "summary": report.summary,
                "outputs": outputs, "outputs_url": [f"/{o}" for o in outputs],
                "stages": report.stages, "log": report.log,
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def _handle_watch(self):
        """POST /api/watch  { folder?, preset?, enabled? } -> current state"""
        data = self._read_body()
        if "folder" in data and data["folder"] is not None:
            _WATCH_STATE["folder"] = str(data["folder"]).strip() or None
        if "preset" in data and data["preset"] in PRESETS:
            _WATCH_STATE["preset"] = data["preset"]
        if "enabled" in data:
            _WATCH_STATE["enabled"] = bool(data["enabled"])
        return self._json(200, _watch_status())

    # silence request logs
    def log_message(self, *args):
        pass


def time_now():
    import time
    return time.time()


def _preset_summary(p) -> Dict[str, Any]:
    return {
        "name": p.name,
        "width": p.width,
        "height": p.height,
        "fps": p.fps,
        "video_bitrate": p.video_bitrate,
        "video_codec": p.video_codec,
        "orientation": "vertical" if p.height > p.width else ("square" if p.height == p.width else "horizontal"),
    }


def _watch_status() -> Dict[str, Any]:
    return {
        "folder": _WATCH_STATE["folder"],
        "preset": _WATCH_STATE["preset"],
        "enabled": _WATCH_STATE["enabled"],
        "running": _WATCH_STATE["running"],
        "last": _WATCH_STATE["last"],
        "processed": list(_WATCH_STATE["processed"])[-10:],
    }


def _watch_loop(stop: threading.Event):
    """Background thread: auto smart-cut every new video dropped in the watch folder."""
    from .pipeline import Pipeline
    while not stop.is_set():
        folder = _WATCH_STATE.get("folder")
        if _WATCH_STATE.get("enabled") and folder and os.path.isdir(folder):
            try:
                videos = sorted(
                    os.path.join(folder, f) for f in os.listdir(folder)
                    if f.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))
                )
                new = [v for v in videos if v not in _WATCH_STATE["seen"]]
                if new:
                    _WATCH_STATE["running"] = True
                    for v in new:
                        _WATCH_STATE["seen"].add(v)
                        name = os.path.splitext(os.path.basename(v))[0]
                        try:
                            Pipeline().smart_cut(
                                v, os.path.join(_OUT_DIR, "watch", name),
                                preset=_WATCH_STATE["preset"], captions=True)
                            _WATCH_STATE["processed"].append(v)
                            _WATCH_STATE["last"] = f"ok: {os.path.basename(v)}"
                        except Exception as e:
                            _WATCH_STATE["last"] = f"error: {os.path.basename(v)}: {e}"
                    _WATCH_STATE["running"] = False
            except Exception as e:
                _WATCH_STATE["last"] = f"watch error: {e}"
        stop.wait(5)


def serve(host: str = "127.0.0.1", port: int = 8456, open_browser: bool = True) -> None:
    """Start the vedits editor server (blocking)."""
    os.makedirs(_OUT_DIR, exist_ok=True)
    os.makedirs(_WATCH_DIR, exist_ok=True)
    if not _WATCH_STATE["folder"]:
        _WATCH_STATE["folder"] = os.path.abspath(_WATCH_DIR)
    _WATCH_STATE["seen"].update(
        os.path.join(_WATCH_STATE["folder"], f) for f in os.listdir(_WATCH_STATE["folder"])
        if os.path.isfile(os.path.join(_WATCH_STATE["folder"], f)))
    stop = threading.Event()
    threading.Thread(target=_watch_loop, args=(stop,), daemon=True).start()
    with VeditsServer((host, port), VeditsHandler) as httpd:
        print(f"vedits editor running at  http://{host}:{port}")
        print(f"watch folder: {_WATCH_STATE['folder']}  (drop videos -> auto shorts)")
        print("agent: POST /api/agent/chat  |  pipeline: POST /api/pipeline/smartcut")
        if open_browser:
            try:
                import webbrowser
                threading.Timer(0.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
            except Exception:
                pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            stop.set()
