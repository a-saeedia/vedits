# vedits — zero-budget, FFmpeg-powered social media video editor

Pure-Python + FFmpeg. No proprietary NLEs, no cloud budgets, no subscriptions.
Hermes orchestrates the pipeline, OpenCode does any model inference, and
FFmpeg slices, transforms and reassembles the frames.

## Requirements

- Python 3.10+ (stdlib only — no third-party deps)
- FFmpeg on PATH **or** set `VEDITS_FFMPEG` / `VEDITS_FFPROBE` to the binaries.

On this machine FFmpeg lives at `C:\Users\User\tools\ffmpeg\` and the default
paths in `vedits/util.py` already point there. Override with env vars for portability.

## Quick start

```bat
vedits presets                       REM list platform presets
vedits info assets\sample.mp4        REM probe a video
vedits fit assets\sample.mp4 -o out\t.mp4 --preset tiktok_1080
vedits trim assets\sample.mp4 -o out\trim.mp4 --start 1 --end 4
vedits caption assets\sample.mp4 -o out\c.mp4 --preset reels_1080 --text "hello"
vedits make --preset reels_1080 -o out\mix.mp4 assets\a.mp4 assets\b.mp4
```

Run without the `vedits.bat` shim:

```
C:\Users\User\tools\litellm\.venv\Scripts\python.exe run.py presets
```

## Web editor (CapCut-style GUI)

```bat
vedits serve
REM -> http://127.0.0.1:8456  (opens in a standalone app-mode window)
```

A browser-based editor with:

- **Media bin** — drag & drop clips in, they upload to the server
- **Timeline** — multi-clip track; drag to reorder, drag edges to trim
- **Inspector** — per-clip trim in/out, playback speed, burned caption text,
  xfade transition to the next clip (fade / dissolve / wipe / slide / …)
- **Preview** — renders the timeline, plays it back in-browser
- **Export** — renders and downloads an upload-ready video for the chosen
  platform preset
- **🤖 Agent console** — talk to a built-in Gemini agent in plain english.
  *"make 3 tiktoks from assets/vlog.mp4 with captions"* → it calls the same
  pipeline tools with proper arguments, runs them on this machine, and
  reports real output files. Tools: `list_media`, `probe_file`,
  `smart_cut`, `render_project`, `read_pipeline_report`.
- **⚙ Pipeline** — one-click *smart cut*: scene-change detection → auto
  plan → render platform-ready shorts from any long video. With
  `curate=True` the plan stage is **vision-curated**: sampled frames are sent
  to a vision model, only the high-interest scenes are kept, and the model's
  suggested captions are burned in.
- **🕵 Watch folder** — drop a video into `watch/` and it is auto-processed
  into shorts (background thread, 5s poll).
- **👁 Video understanding (`vedits vlm <video>`)** — vedits can *watch* a
  video: FFmpeg samples timestamped frames → a vision model returns a scene
  map (`start/end/description/interest/caption`). Works with any free-tier
  vision provider, first key found wins:

  | provider   | requires             | model                          | free tier                     |
  |------------|----------------------|--------------------------------|-------------------------------|
  | `github`   | `GITHUB_MODELS_TOKEN` | `gpt-4o-mini`                  | free, high daily limits       |
  | `openrouter` | `OPENROUTER_API_KEY` | `google/gemini-2.5-flash:free` | $0 variant                    |
  | `gemini`   | `GEMINI_API_KEY`      | `gemini-2.5-flash`             | 20 free requests/day          |

  Keys are read at runtime from `C:\Users\User\tools\litellm\.env` (never
  hardcoded). Fallback chain: github → openrouter → gemini; dead keys and
  quota errors auto-skip to the next provider.

The GUI builds the exact same project JSON an agent can POST:

```bash
curl -X POST http://127.0.0.1:8456/api/render \
  -H "Content-Type: application/json" \
  -d @project.json   # {"preset":"reels_1080","clips":[{"src":"media/abc.mp4",...}]}
```

That's the agent interface: Hermes/OpenCode assemble `project.json`
(trim windows, captions, transitions, preset) and get back a finished file.

### API endpoints

| Endpoint                  | Method | Purpose                                        |
|---------------------------|--------|------------------------------------------------|
| `/api/presets`            | GET    | list platform presets                          |
| `/api/upload`             | POST   | multipart file upload → `media/…` path         |
| `/api/render`             | POST   | project JSON → rendered mp4                    |
| `/api/agent/tools`        | GET    | agent capabilities (LLM + tool list)           |
| `/api/agent/chat`         | POST   | `{text}` → agent executes tools, returns reply |
| `/api/pipeline/smartcut`  | POST   | `{src,preset,min_dur,max_dur,captions}` → shorts |
| `/api/watch`              | GET/POST | watch-folder status / configure `{folder,preset,enabled}` |
| `/outputs/…`              | GET    | download a rendered file                       |
| `/media/…`                | GET    | download an uploaded source                    |

`vedits render-project project.json -o out.mp4` renders from the CLI, no server needed.

### Pipeline engine (deep backend)

`vedits/pipeline.py` is a staged, observable pipeline:

```
INGEST → ANALYZE → PLAN → RENDER → DELIVER
```

- `analyze_scenes()` — ffmpeg `select=gt(scene,thr)` scene-change detection
- `analyze_silence()` — ffmpeg `silencedetect` dead-air detection
- `plan_windows()` — merges slivers, clamps windows, splits over-long segments
- Each stage emits progress events consumed by the GUI / agents

The agent (Gemini via `vedits/agent.py`, key loaded from
`C:\Users\User\tools\litellm\.env`) turns natural language into tool calls
and drives this pipeline locally.

## Architecture

```
vedits/
├── vedits/
│   ├── util.py       subprocess, timecode parse, binary resolution
│   ├── probe.py      ffprobe wrapper (metadata, dims, fps, codecs)
│   ├── presets.py    social platform specs (res, fps, codec, bitrate)
│   ├── ops.py        FFmpeg ops: trim, fit, crop, rescale, concat, reverse
│   ├── text.py       drawtext overlays (burn-in captions/titles)
│   ├── timeline.py   declarative multi-clip Timeline model -> render()
│   ├── project.py    project-JSON model + renderer w/ xfade transitions
│   ├── pipeline.py   staged pipeline: scene/silence detect -> plan -> render
│   ├── agent.py      Gemini tool-calling agent (drives pipeline + project)
│   ├── server.py     stdlib HTTP server: GUI + agent + pipeline API
│   ├── gui/          browser editor (index.html, app.js, styles.css)
│   └── cli.py        subcommand CLI
├── tests/            self-contained test runner (no pytest required)
└── assets/           sample clips (testsrc / sine)
```

### The Hermes layering

- **Timeline** classes describe an edit declaratively:
  `Segment(source, start, end, overlays)` -> `Timeline(segments, preset, output)`.
- **render()** normalizes each segment (trim -> fit-and-reencode to the target
  preset, burning overlays), then concatenates. A script/LLM builds a Timeline
  from a prompt and calls `render()`.
- OpenCode fits in where model inference is needed: generating caption text,
  choosing crop windows, scene detection, style choices — it emits a Timeline.

## Presets

Vertical (TikTok / Reels / Shorts), horizontal (YT 1080/720), and square.
`fit` reframes to *exactly* the target canvas with crop-to-fill (no bars),
selectable per edge with `--crop`.

## Env vars

- `VEDITS_FFMPEG`, `VEDITS_FFPROBE` — binary paths.
- `VEDITS_WORKDIR` — (future) render scratch dir.

## Tests

```
C:\Users\User\tools\litellm\.venv\Scripts\python.exe tests\test_vedits.py
```
