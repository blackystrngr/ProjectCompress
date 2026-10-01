# 🛠️ ProjectCompress — Developer Guide

A complete walkthrough of the architecture, structure, and workflow for anyone
joining the project or extending it.

---

## 📖 Table of Contents

1. [What Is This Project?](#1-what-is-this-project)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Folder Structure](#3-folder-structure)
4. [How It Was Built](#4-how-it-was-built)
5. [The Task System (Core Concept)](#5-the-task-system-core-concept)
6. [Real-Time Updates (SSE)](#6-real-time-updates-sse)
7. [Feature Modules](#7-feature-modules)
8. [Frontend Structure](#8-frontend-structure)
9. [How to Add a New Feature](#9-how-to-add-a-new-feature)
10. [How to Maintain the Project](#10-how-to-maintain-the-project)
11. [Common Debugging](#11-common-debugging)
12. [Conventions & Best Practices](#12-conventions--best-practices)
13. [Deployment](#13-deployment)

---

## 1. What Is This Project?

**ProjectCompress** is a self-hosted Flask web app that gives you a single
dashboard for:

- Downloading videos/files from any URL
- Compressing videos with FFmpeg
- Managing files on Google Drive and local disk
- Scanning and downloading from Telegram chats
- OCR, subtitles, proxies, torrents, face-swap, web crawling

It is not a "framework" — it's a **modular toolbox**. Every feature is a Python
file in `features/` that plugs into the main Flask app.

**Audience:** Personal VPS deployment. Not multi-user SaaS.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       BROWSER (SPA-like)                     │
│  - Tabs for each feature                                     │
│  - SSE connection for live task updates                      │
│  - Per-feature polling for their own task progress           │
└─────────────────────────────────────────────────────────────┘
                              │
                              │  HTTP + SSE
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  FLASK APP (app.py, Waitress)                │
│                                                              │
│  Core routes:                                                │
│   /                       → main UI                          │
│   /start                  → queue download                   │
│   /progress/<id>          → per-task progress                │
│   /cancel/<id>            → cancel task                      │
│   /get_tasks              → list all tasks                   │
│   /tasks/stream           → SSE stream                       │
│   /system_stats           → CPU/RAM/Disk/Network             │
│   /upload_cookies         → receive cookies from extension   │
└─────────────────────────────────────────────────────────────┘
                              │
                              │  register_routes(app)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      features/*.py                           │
│  Every feature is an independent module that registers its   │
│  own routes with the Flask app.                             │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      External Tools                          │
│  yt-dlp · ffmpeg · telethon · google-api · tesseract ·       │
│  libtorrent · requests                                       │
└─────────────────────────────────────────────────────────────┘
```

**Key idea:** Every long-running job is a **task**, stored as a JSON file in
`tasks/`, and reported to the UI via either SSE (global) or polling (per-feature).

---

## 3. Folder Structure

```
ProjectCompress/
├── app.py                    # Flask entry, core routes, SSE, system stats
├── config.py                 # Paths, secrets, API keys
├── tasks.py                  # Task state + in-memory cache + broadcaster
├── requirements.txt
├── install.sh                # One-shot installer
├── settings.json             # Runtime settings (auto-generated)
├── cookies.txt               # YouTube/site cookies (from extension)
├── token.json                # Google OAuth token
├── telegram_creds.json       # Telegram API credentials
│
├── features/                 # ← ALL features live here
│   ├── __init__.py           # register_all_features(app)
│   ├── url_download.py
│   ├── video_extractor.py
│   ├── video_clipper.py
│   ├── face_swap.py
│   ├── ocr.py
│   ├── subtitle_finder.py
│   ├── proxy_fetcher.py
│   ├── telegram.py
│   ├── google_drive.py
│   ├── local_files.py
│   ├── torrent_search.py
│   ├── web_crawler.py
│   └── thumbnails.py
│
├── templates/                # Jinja2 HTML
│   ├── base.html             # Main shell
│   ├── index.html
│   ├── _features_macro.html  # Tab + pane macros
│   └── features/
│       ├── url.html
│       ├── telegram.html
│       ├── drive.html
│       ├── local.html
│       ├── video_clipper.html
│       ├── proxy_fetcher.html
│       ├── subtitle_finder.html
│       ├── ocr.html
│       └── web_crawler.html
│
├── static/
│   ├── css/style.css
│   └── js/app.js             # Global SSE, tabs, helpers, polling
│
├── downloads/                # All user files
│   ├── .thumbnails/          # Cached video thumbnails
│   └── (playlists, clips, etc.)
│
├── tasks/                    # Task state JSON files (one per task)
├── proxy_cache/              # Cached proxy results
└── venv/                     # Optional virtualenv
```

---

## 4. How It Was Built

The project was developed incrementally, feature by feature. Here's the pattern
used for every new capability:

### Step 1: Identify the capability
Example: "I want to OCR an image."

### Step 2: Create a feature module
`features/ocr.py` — a self-contained file with:
- Helper functions
- A background worker function
- A `register_routes(app)` function

### Step 3: Register it in `features/__init__.py`
```python
from . import ocr
ocr.register_routes(app)
```

### Step 4: Create a UI pane
`templates/features/ocr.html` with form + JavaScript.

### Step 5: Add it to the tab macro
In `templates/_features_macro.html`:
```html
<button class="tab-btn" data-tab="ocr">
    <i class="fas fa-file-alt"></i> OCR
</button>
...
<div id="ocr-tab" class="tab-pane">
    {% include 'features/ocr.html' %}
</div>
```

### Step 6: Test end-to-end
Drop a file in → watch task progress → see output.

**Every feature follows this exact pattern.** Once you understand one, you
understand them all.

---

## 5. The Task System (Core Concept)

Every long-running job is a **task** — identified by a UUID, stored as
`tasks/<uuid>.json`.

### Task structure (typical)

```json
{
  "task_id": "a1b2c3d4-...",
  "status": "downloading",
  "progress": 45,
  "created_at": 1738000000.0,
  "cancelled": false,
  "url": "https://...",
  "output_file": null,
  "error_msg": null
}
```

### Why tasks?

- **Persistent** — survive page reloads and even server restarts
- **Inspectable** — any code can read a task's state by ID
- **Cancellable** — flip `cancelled: true` and workers check for it

### The task lifecycle

```
queued → running (multiple sub-statuses) → done | error | cancelled
```

### Writing to a task

```python
from tasks import load_task, save_task

task = load_task(task_id)
task['progress'] = 50
save_task(task_id, task)
```

`save_task()`:
1. Updates the in-memory cache (instant reads)
2. Writes to disk (persistence)
3. Broadcasts to all SSE clients

### Reading a task

```python
task = load_task(task_id)
if task.get('cancelled'):
    return  # user cancelled
```

### Task cleanup

Terminal tasks (`done`, `error`, `cancelled`) are kept for 10 minutes
then auto-removed on next server startup via `cleanup_old_tasks()`.

---

## 6. Real-Time Updates (SSE)

The app uses **Server-Sent Events** — a one-way streaming connection from
server to browser.

### How it works

1. Browser opens `EventSource('/tasks/stream')`.
2. Server pushes the **full task list** whenever any task changes.
3. Browser re-renders the task list.
4. Connection auto-reconnects if it drops.

### Server side (`app.py`)

```python
@app.route('/tasks/stream')
def tasks_stream():
    q = queue.Queue(maxsize=1)
    add_subscriber(q)

    def event_stream():
        try:
            initial = json.dumps(get_active_tasks())
            yield f"data: {initial}\n\n"
            while True:
                try:
                    data = q.get(timeout=HEARTBEAT_SECONDS)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            remove_subscriber(q)

    return Response(stream_with_context(event_stream()),
                    mimetype="text/event-stream")
```

### Broadcast (`tasks.py`)

Every `save_task()` triggers `_broadcast_from_cache()`, which pushes the
snapshot to all subscribers.

### Why not WebSockets?

SSE is simpler, works over plain HTTP, auto-reconnects, and is perfectly
suited for one-way server→client updates. WebSockets are overkill here.

---

## 7. Feature Modules

Each feature in `features/` follows the same structure:

### Skeleton

```python
# features/my_feature.py
import os
import uuid
import threading
import logging
from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)


# ---- Worker (runs in a background thread) ----
def process_task(task_id, input_data):
    task = load_task(task_id)
    task['status'] = 'running'
    save_task(task_id, task)

    try:
        # ... do the actual work ...
        # periodically:
        #   task['progress'] = X
        #   save_task(task_id, task)
        # and check cancellation:
        #   if load_task(task_id).get('cancelled'): return

        task['status'] = 'done'
        task['output_file'] = 'result.mp4'
        save_task(task_id, task)
    except Exception as e:
        logger.exception("Feature failed")
        task = load_task(task_id)
        task['status'] = 'error'
        task['error_msg'] = str(e)
        save_task(task_id, task)


# ---- Routes ----
def register_routes(app):

    @app.route('/my_feature/start', methods=['POST'])
    def my_feature_start():
        # 1. Validate input
        value = request.form.get('value')
        if not value:
            return jsonify({'error': 'value required'}), 400

        # 2. Create a task
        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
        })

        # 3. Launch background worker
        threading.Thread(
            target=process_task,
            args=(task_id, value),
            daemon=True,
        ).start()

        # 4. Return task_id so UI can poll /progress/<task_id>
        return jsonify({'task_id': task_id})
```

That's the entire contract.

### Example features and what they do

| Feature | What it does | Key dependency |
|---------|--------------|----------------|
| `url_download.py` | Downloads videos, files, torrents, playlists | `yt-dlp`, `requests` |
| `video_clipper.py` | Random clips, frames, summarizer | `ffmpeg` |
| `ocr.py` | Extracts text from images/PDFs | `pytesseract` |
| `telegram.py` | Scans/downloads/upload from chats | `telethon` |
| `google_drive.py` | Lists/downloads/deletes Drive files | `google-api-python-client` |
| `local_files.py` | File browser for `downloads/` | Flask only |
| `web_crawler.py` | Recursive domain discovery | `requests`, `bs4` |
| `thumbnails.py` | Extracts thumbnails from videos | `ffmpeg` |

---

## 8. Frontend Structure

### Base shell: `templates/base.html`

Contains:
- Top system stats bar (CPU/RAM/Disk/Network)
- Thumbnail pause toggle
- Header + tab bar
- Active tasks panel
- Global `<script src="app.js">`

### Tabs and panes: `templates/_features_macro.html`

Two macros:
- `render_tabs()` — the tab buttons
- `render_panes()` — the content containers

Every feature appears in both.

### Global JS: `static/js/app.js`

Responsibilities:
- Opens the SSE connection to `/tasks/stream`
- Renders the task list
- Wires up tab switching
- Provides helpers (`showToast`, `escapeHtml`, `formatBytes`)
- Handles persistent task polling per feature

### Per-feature JS: inside each `templates/features/*.html`

Each feature has its own `<script>` block that:
- Handles its form submission
- Polls `/progress/<task_id>` for its own task
- Renders its own result UI

**Two-layer task system:**
- **Global panel** (bottom of page) — shows every active task via SSE
- **Feature-local panel** — shows only its own task via polling

---

## 9. How to Add a New Feature

Let's add a hypothetical **"MP3 Converter"**.

### Step 1: Create `features/mp3_converter.py`

```python
import os
import uuid
import time
import threading
import logging
import subprocess
from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)


def convert_to_mp3(task_id, input_path):
    task = load_task(task_id)
    task['status'] = 'converting'
    save_task(task_id, task)

    out_name = os.path.splitext(os.path.basename(input_path))[0] + '.mp3'
    out_path = os.path.join(UPLOAD_FOLDER, out_name)

    try:
        cmd = ['ffmpeg', '-y', '-i', input_path,
               '-vn', '-c:a', 'libmp3lame', '-b:a', '192k', out_path]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise Exception(result.stderr[:200])

        task = load_task(task_id)
        task['status'] = 'done'
        task['progress'] = 100
        task['output_file'] = out_name
        save_task(task_id, task)
    except Exception as e:
        logger.exception("MP3 conversion failed")
        task = load_task(task_id)
        task['status'] = 'error'
        task['error_msg'] = str(e)
        save_task(task_id, task)


def register_routes(app):

    @app.route('/mp3/convert', methods=['POST'])
    def mp3_convert():
        filename = request.form.get('filename', '')
        if not filename:
            return jsonify({'error': 'filename required'}), 400

        input_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(input_path):
            return jsonify({'error': 'File not found'}), 404

        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
        })
        threading.Thread(
            target=convert_to_mp3,
            args=(task_id, input_path),
            daemon=True,
        ).start()
        return jsonify({'task_id': task_id})
```

### Step 2: Register in `features/__init__.py`

```python
from . import mp3_converter
# ...
def register_all_features(app):
    # ...
    mp3_converter.register_routes(app)
```

### Step 3: Create `templates/features/mp3_converter.html`

```html
<div class="card">
    <h2><i class="fas fa-music"></i> MP3 Converter</h2>
    <div class="input-group">
        <input type="text" id="mp3File" placeholder="video.mp4">
        <button id="mp3ConvertBtn" class="primary">
            <i class="fas fa-play"></i> Convert
        </button>
    </div>
    <div id="mp3Status" class="help-text"></div>
</div>

<script>
document.getElementById('mp3ConvertBtn').onclick = async () => {
    const filename = document.getElementById('mp3File').value.trim();
    if (!filename) return showToast('Filename required', true);

    const form = new URLSearchParams();
    form.append('filename', filename);

    const resp = await fetch('/mp3/convert', { method: 'POST', body: form });
    const data = await resp.json();
    if (data.error) return showToast(data.error, true);

    const statusEl = document.getElementById('mp3Status');
    const interval = setInterval(async () => {
        const r = await fetch(`/progress/${data.task_id}`);
        const task = await r.json();
        if (task.status === 'done') {
            clearInterval(interval);
            statusEl.innerHTML = `✅ <a href="/download_file?path=${task.output_file}">Download</a>`;
        } else if (task.status === 'error') {
            clearInterval(interval);
            statusEl.innerHTML = `❌ ${task.error_msg}`;
        } else {
            statusEl.innerHTML = `⏳ ${task.status}`;
        }
    }, 2000);
};
</script>
```

### Step 4: Add to `_features_macro.html`

```html
{% macro render_tabs() %}
<!-- ... existing tabs ... -->
<button class="tab-btn" data-tab="mp3">
    <i class="fas fa-music"></i> MP3
</button>
{% endmacro %}

{% macro render_panes() %}
<!-- ... existing panes ... -->
<div id="mp3-tab" class="tab-pane">
    {% include 'features/mp3_converter.html' %}
</div>
{% endmacro %}
```

### Step 5: Restart Flask → done

Your feature is live, uses the standard task system, and integrates with
the global task panel automatically.

---

## 10. How to Maintain the Project

### Daily / weekly

- **Monitor task files**: `ls tasks/*.json | wc -l` — should stay small.
  The startup cleanup handles old ones, but you can manually delete stale files.
- **Check `downloads/` size**: Big libraries fill the disk fast.
- **Check `cookies.txt` freshness**: YouTube cookies expire ~every few weeks.
- **Restart Flask periodically** to clear memory and stale threads.

### Monthly

- **Update yt-dlp**: `pip install --break-system-packages -U yt-dlp`
  (YouTube breaks yt-dlp frequently).
- **Update ffmpeg** if you installed via static build — re-run `install.sh`.
- **Empty Drive trash** if you use the Drive features heavily.
- **Review logs**: `journalctl -u projectcompress -n 200` for errors.

### When YouTube downloads fail

1. Export fresh cookies via the browser extension.
2. Upload them via the extension.
3. Verify `cookies.txt` has current entries.
4. Test: `yt-dlp --cookies cookies.txt <url>` in the terminal.

### When a task hangs

1. Check `/tasks/<task_id>.json` — status tells you where it's stuck.
2. Check running processes: `ps aux | grep -E "ffmpeg|yt-dlp"`.
3. Cancel via the UI → it flips `cancelled: true` and calls `kill_process()`.
4. If the worker ignores it, restart Flask.

---

## 11. Common Debugging

### "Task stuck in `queued` forever"

The thread never started. Check that `threading.Thread(...).start()` is called.

### "Task shows progress but file never appears"

The worker finished but `output_file` wasn't set. Check the worker's end path.

### "SSE connection errors in browser console"

- Check `/tasks/stream` returns `Content-Type: text/event-stream`
- Ensure `HEARTBEAT_SECONDS` in `app.py` isn't too high for your proxies
- Check firewall isn't buffering responses

### "ffmpeg not found" when task runs

Subprocesses may not inherit the same `PATH` as your shell. Always:

```python
os.environ['PATH'] = '/usr/local/bin:' + os.environ.get('PATH', '')
```

at the start of any worker that spawns ffmpeg/yt-dlp.

### "Cancel doesn't work"

- The worker must call `load_task(task_id).get('cancelled')` **periodically**.
- For subprocess-based workers, register the process (`register_process()`)
  so `kill_process(task_id)` can kill it.

### Verbose logging

Set `logging.basicConfig(level=logging.DEBUG)` in `app.py`.

---

## 12. Conventions & Best Practices

### Python

- **One feature = one file.** Never mix concerns.
- **All feature workers are threaded**, always `daemon=True`.
- **Always check cancellation** in loops.
- **Use `load_task()` / `save_task()`** for all task state. Never write JSON directly.
- **Catch every exception** inside the worker and set `status: 'error'`.
- **Log with `logger.exception()`** for failures, `logger.info()` for progress.
- **Use `os.path.join`** everywhere, no hardcoded `/` or `\`.
- **Never trust user input** — always validate paths and URLs.

### Path safety

```python
if '..' in user_path or user_path.startswith('/'):
    return jsonify({'error': 'Invalid path'}), 400
```

Applies to every file-path-based route.

### Frontend

- **One feature = one HTML pane.**
- **Always `escapeHtml()`** user-controlled strings in innerHTML.
- **Use `showToast()`** for user feedback (not `alert()`).
- **Poll every 2–5 seconds.** More frequent is wasteful.
- **Use `storeTaskId()`** and `startPollingForTask()` to survive page reloads.

### Git

- **Never commit secrets**: `cookies.txt`, `token.json`, `telegram_creds.json`.
- **Never commit `downloads/`, `tasks/`, `proxy_cache/`, `venv/`.**
- Keep `.gitignore` updated.

### Security

- The app has **no authentication**. Behind a public URL, add Nginx Basic Auth.
- Use HTTPS always.
- Treat `COOKIE_UPLOAD_TOKEN` as a real secret.

---

## 13. Deployment

### One-liner (VPS)

```bash
git clone https://github.com/blackystrngr/ProjectCompress.git
cd ProjectCompress
chmod +x install.sh
sudo ./install.sh
```

### Run in foreground

```bash
python3 app.py
```

### Run as systemd service (recommended)

Create `/etc/systemd/system/projectcompress.service`:

```ini
[Unit]
Description=ProjectCompress
After=network.target bgutil-provider.service

[Service]
Type=simple
WorkingDirectory=/root/ProjectCompress
ExecStart=/usr/bin/python3 /root/ProjectCompress/app.py
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now projectcompress
sudo systemctl status projectcompress
```

### Behind Nginx (with auth)

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        auth_basic "Restricted";
        auth_basic_user_file /etc/nginx/.htpasswd;

        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;         # required for SSE
        proxy_read_timeout 3600s;    # long-lived SSE connections
    }
}
```

**Critical:** `proxy_buffering off` — otherwise SSE breaks.

---

## 🎓 Mental Model — One Page Summary

1. **Flask app** — registers all feature routes at startup.
2. **Every long job = a task** — a JSON file with status/progress/output.
3. **Tasks are updated via `save_task()`** — cache → disk → SSE broadcast.
4. **UI** — tabs on top, SSE-driven task panel at the bottom.
5. **Cancel** — set `cancelled: true`, worker checks it, `kill_process()` for
   subprocesses.
6. **Adding features** — new file in `features/`, register in `__init__.py`,
   add pane to `_features_macro.html`, done.
7. **Debug** — read the task JSON, check the log, check running processes.

That's the entire project in a nutshell. Once you've read one feature
(`features/ocr.py` is a good small one), you've read them all.
