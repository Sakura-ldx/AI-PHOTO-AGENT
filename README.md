# AI Photo Agent

**Language:** English | [简体中文](README.zh-CN.md)

AI Photo Agent is a local-first AI photography assistant. Upload a photo, let a vision
model diagnose it in context, get three photo-specific editing styles, and send the
result to Lightroom Classic for a real preview.

## Current capabilities

1. Select a JPG/JPEG, PNG, TIF/TIFF, or Canon CR3 file; it uploads and analyzes automatically, with no extra upload button.
2. CR3 uses the camera's embedded JPEG preview for analysis while the original `.cr3`
   stays untouched; TIFF is fully decoded with Pillow.
3. By default, call **DeepSeek vision** on the uploaded photo to produce:
   - an overall diagnosis;
   - technical, composition, and color/tone observations;
   - exactly three styles with complete, executable parameters for this photo.
4. The UI labels the first style as the “AI pick” and selects it automatically; the user
   can still choose another.
5. Render a Pillow approximation, export a `lightroom_xmp_v2` preset, and generate
   `lightroom_develop_v1` develop settings.
6. Display the photo in the upload card immediately; normalize JPEG EXIF / CR3 CMT1
   orientation so the upload, before/after previews, and rendered preview agree.
7. Through the Lightroom Classic plug-in, write an `AI Photo Agent - Before` snapshot,
   apply the settings, and switch to the Develop module.
8. Download XMP presets as attachments.
9. Phase 2 reference-photo learning (backend API and plugin tool) accepts up to eight
   references and returns composition, color, and practice summaries; that workflow still
   uses Mock (per-photo learning summaries do not depend on it). The web editing UI no
   longer shows this panel, so the page focuses on editing and the knowledge base.
10. Long analyses can run in the background through `/api/photos/{id}/analyze/jobs` with
    stage progress and cancellation (bounded to 8 runners / 16 live threads per process).
11. Send the current style straight to Lightroom, install the plug-in from the UI, and
    inspect bridge status; full-screen lightbox supports anchored wheel zoom and panning.
12. **Phase 2 learning summaries / knowledge base.** Every successful analysis also
    persists a learning summary: the image itself (thumbnail + metadata), the analysis
    snapshot, all three editing directions with parameter summaries, an intelligently
    recommended intensity whose preview is rendered and verified through the preview
    pipeline, and deterministic classification tags. The knowledge base can be browsed,
    filtered by tag dimension/category, and rebuilt per photo through `/api/knowledge/*`;
    the summaries are regenerated automatically when a photo is re-analyzed. The page has
    two top-level views — editing studio and knowledge base — and the top navigation
    switches between them in both directions; the second tab of the DeepSeek Harness
    plugin panel shows the same knowledge base.

## Why diagnosis and styles are no longer fixed

The runtime default provider is `deepseek`, using `deepseek-flash`, which accepts image
input. Every upload is re-examined, so the diagnosis, style IDs, names, reasons, and
parameters change with the photo. The application refuses to start without a key and
never silently falls back to Mock.

The key is resolved from:

1. `AI_PHOTO_AGENT_DEEPSEEK_API_KEY`
2. `DEEPSEEK_API_KEY`
3. `AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE`, only when you point it at a YAML
   file that contains `DEEPSEEK_API_KEY: ...`

The plug-in does not read another application's credential store implicitly. For
local development, copy `.env.example` to `.env`; `run_app.py` loads it while
already-exported environment variables keep precedence.

The key is never logged or returned over HTTP. See
[DeepSeek live diagnosis and dynamic styles](docs/DEEPSEEK_ANALYSIS.zh-CN.md)
for implementation and measured results.

## Quick start

Python 3.12 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_app.py
```

The server starts at `http://127.0.0.1:8000` and opens a browser. To start without a
browser:

```powershell
.\.venv\Scripts\python.exe run_app.py --no-browser --port 8000
```

## Lightroom integration

The repository ships a Lightroom Classic plug-in at:

```text
lightroom/AIPhotoAgent.lrplugin/
```

First use:

1. In Lightroom, open `File > Plug-in Manager > Add`.
2. Select the `lightroom/AIPhotoAgent.lrplugin` folder.
3. Restart Lightroom.
4. In the web UI, choose a photo/style and click “Send to Lightroom”.
5. In Lightroom, select the target photo and run
   `Library > Plug-in Extras > AI Photo Agent - Apply pending web edit`.

The plug-in writes an `AI Photo Agent - Before` snapshot, applies the settings, and
switches to Develop. On the tested Lightroom Classic 15.1 Windows build,
`createVirtualCopy` blocked, so virtual-copy mode is disabled
(`USE_VIRTUAL_COPY=false`); the edit is applied to the selected photo and can be reverted
with the Before snapshot or Ctrl+Z. Installation, real-machine evidence, and limitations
are in [the Lightroom guide (Chinese)](docs/LIGHTROOM_PLUGIN.zh-CN.md).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AI_PHOTO_AGENT_ANALYSIS_PROVIDER` | `deepseek` | `deepseek` / `mock` / `openai` / `codex` |
| `AI_PHOTO_AGENT_DEEPSEEK_API_KEY` | empty | DeepSeek key (preferred) |
| `DEEPSEEK_API_KEY` | empty | DeepSeek key fallback |
| `AI_PHOTO_AGENT_OPENAI_API_KEY` | empty | OpenAI key (preferred) |
| `OPENAI_API_KEY` | empty | OpenAI key fallback |
| `AI_PHOTO_AGENT_DEEPSEEK_MODEL` | `deepseek-flash` | DeepSeek vision model |
| `AI_PHOTO_AGENT_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible base URL |
| `AI_PHOTO_AGENT_DEEPSEEK_TIMEOUT_SECONDS` | `180` | Per-analysis timeout |
| `AI_PHOTO_AGENT_DEEPSEEK_MAX_RETRIES` | `1` | Retries for 429/5xx, with backoff and `Retry-After` |
| `AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE` | empty (disabled) | Optional explicit YAML key file |
| `AI_PHOTO_AGENT_OPENAI_MODEL` | `gpt-5.6-terra` | Model for the `openai` provider |
| `AI_PHOTO_AGENT_OPENAI_TIMEOUT_SECONDS` | `45` | OpenAI request timeout |
| `AI_PHOTO_AGENT_OPENAI_MAX_RETRIES` | `2` | OpenAI SDK retries |
| `AI_PHOTO_AGENT_CODEX_MODEL` | `gpt-5.6-sol` | Model for the `codex` provider |
| `AI_PHOTO_AGENT_CODEX_EXECUTABLE` | from `PATH` | Explicit `codex` executable |
| `AI_PHOTO_AGENT_CODEX_TIMEOUT_SECONDS` | `600` | Codex CLI timeout |
| `AI_PHOTO_AGENT_DATA_DIR` | repository root | Storage root; private state lives under `<root>/data/` and uploads under `<root>/uploads/` |
| `AI_PHOTO_AGENT_LIGHTROOM_ROOT` | auto-detect | Lightroom user directory |
| `AI_PHOTO_AGENT_MAX_UPLOAD_BYTES` | `268435456` (256 MiB) | Upload byte limit |
| `AI_PHOTO_AGENT_MAX_IMAGE_PIXELS` | `50000000` | Decoded pixel limit per image |
| `AI_PHOTO_AGENT_SERVER_URL` | published by `run_app.py` | Base URL embedded in Lightroom bridge jobs |

Filesystem paths (`DATA_DIR`, `LIGHTROOM_ROOT`, `CODEX_EXECUTABLE`, and the optional
credentials file) expand `~`, Windows `%VAR%`, and POSIX `$VAR`.

For offline runs/tests:

```powershell
$env:AI_PHOTO_AGENT_ANALYSIS_PROVIDER = "mock"
```

## Security and behavior notes

- Write requests that carry a non-loopback `Origin` are rejected with **403**; requests
  without an `Origin` header (Lightroom Lua, CLI tools, `TestClient`) are allowed. This is
  a minimal CSRF guard, not authentication.
- `Host` headers are restricted to `127.0.0.1`, `localhost`, `[::1]` and the test host;
  other names get **400**. This blocks DNS-rebinding requests from a browser page.
- Every response sets `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` and `Cross-Origin-Resource-Policy: same-origin`.
- A declared `Content-Length` above `max_upload_bytes + 1 MiB` returns **413 before
  multipart parsing**; the chunked read limit still applies afterwards.
- `run_app.py` publishes the actual `--host/--port` as `AI_PHOTO_AGENT_SERVER_URL` before
  importing the app, so Lightroom bridge jobs call back to the right address. An explicit
  environment value always wins.
- Uploads store a sanitized basename in `PhotoSession.original_filename`; bridge jobs
  include it as `photo.original_filename` (basename only, never an absolute path).
- The DSH plug-in defaults its intensity slider to `0.7` and validates
  Host/Origin/Sec-Fetch-Site on its own routes; see `dsh-plugin/README.md`.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -W error -p no:cacheprovider
```

Current result: the full test suite passes. `-p no:cacheprovider` is required in
this workspace because `.pytest_cache` has broken ACLs. The Lightroom plug-in validator
also passes:

```powershell
.\.venv\Scripts\python.exe scripts\validate_lightroom_plugin.py
```

Historical browser evidence (upload orientation, XMP download) remains in
[upload orientation and XMP download fixes (Chinese)](docs/UPLOAD_ORIENTATION_XMP.zh-CN.md):
a real CR3 reported/displayed **4000×6000** portrait, the browser preview was 1067×1600,
and the XMP downloaded as an attachment (2733 bytes).

Live CR3 check: after uploading `D:\Photos\2026\2026-05-23\example.CR3`, analysis returned
200 with three dynamic styles: `urban-blue-clean`, `leafy-film-warm`, and
`high-key-minimal`. The first style then passed `/preview`, `/export`, and `/develop`
with 200 responses. The full analysis is saved at
`data/runtime/deepseek-live-analysis.json`.

## Documentation

- [DeepSeek live diagnosis and dynamic styles (Chinese)](docs/DEEPSEEK_ANALYSIS.zh-CN.md)
- [Upload orientation and XMP download fixes (Chinese)](docs/UPLOAD_ORIENTATION_XMP.zh-CN.md)
- [Lightroom plug-in and bridge guide (Chinese)](docs/LIGHTROOM_PLUGIN.zh-CN.md)
- [Current code index and architecture (Chinese)](docs/CURRENT_CODEBASE_INDEX.zh-CN.md)
- [Project goal (Chinese)](docs/PROJECT_GOAL.zh-CN.md)
- [Product requirements (Chinese)](docs/PRODUCT_REQUIREMENTS.zh-CN.md)
- [MVP roadmap (Chinese)](docs/MVP_ROADMAP.zh-CN.md)

## Known limitations

- DeepSeek analysis depends on network, quota, and model output quality; it usually takes
  20–60 seconds.
- Phase 2 reference-image learning still uses Mock in `deepseek` mode; the new
  per-photo learning summaries are deterministic and do not depend on it.
- Background analysis jobs live in memory only: they are lost on restart, expire after
  one hour, and have no cross-restart queue.
- Uploads still buffer the whole file in memory; the `Content-Length` guard only trusts
  the declared value, so public deployments still need a proxy/ASGI body limit.
- There is no authentication, account, quota, or delete/retention API; the non-loopback
  `Origin` check is a minimal browser guard, not a full CSRF boundary.
- Virtual-copy mode is not enabled; Lightroom has only one verified RAW application, and
  per-control visual checks remain.
- The CR3 display image is still full 4000×6000 (~5.5 MB), so large uploads cost some bandwidth.
- The web preview is a Pillow approximation, not a Lightroom render.
