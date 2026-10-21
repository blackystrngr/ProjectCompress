# 🛠️ ProjectCompress — Developer Guide

A complete walkthrough of the architecture, structure, and workflow for anyone joining the project or extending it.

---

## 📖 Table of Contents

1. [What Is This Project?](#1-what-is-this-project)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Folder Structure](#3-folder-structure)
4. [How It Was Built](#4-how-it-was-built)
5. [The Task System (Core Concept)](#5-the-task-system-core-concept)
6. [Real-Time Updates (SSE)](#6-real-time-updates-sse)
7. [Settings System](#7-settings-system)
8. [Theme System (Classic vs Modern)](#8-theme-system-classic-vs-modern)
9. [Telegram Logger](#9-telegram-logger)
10. [Authentication](#10-authentication)
11. [Feature Modules](#11-feature-modules)
12. [Frontend Structure](#12-frontend-structure)
13. [How to Add a New Feature](#13-how-to-add-a-new-feature)
14. [How to Add a New Setting](#14-how-to-add-a-new-setting)
15. [How to Maintain the Project](#15-how-to-maintain-the-project)
16. [Common Debugging](#16-common-debugging)
17. [Conventions & Best Practices](#17-conventions--best-practices)
18. [Deployment](#18-deployment)

---

## 1. What Is This Project?

**ProjectCompress** is a self-hosted Flask web app that gives you a single dashboard for:

- Downloading videos/files from any URL
- Compressing videos with FFmpeg
- Managing files on Google Drive and local disk
- Scanning and downloading from Telegram chats
- OCR, subtitles, proxies, torrents, face-swap, web crawling

It is not a "framework" — it's a **modular toolbox**. Every feature is a Python file in `features/` that plugs into the main Flask app.

**Audience:** Personal VPS deployment. Not multi-user SaaS.

**Recent additions (v2):**
- Dual-theme UI (classic + animated modern) with persistent switcher
- Right-side settings panel with live-apply (no restart)
- Central `settings.json` store for all runtime toggles
- Telegram log-forwarding (bot or user session, batch + rate-limited)
- Basic Auth using `/root/.password` (env-overridable, multi-location)
- Auto-join for Telegram channels (invite, public, private-ID)

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       BROWSER (SPA-like)                     │
│  • Dual stylesheets (classic + modern), one active           │
│  • Tabs for each feature                                     │
│  • Slide-out settings panel (right edge)                     │
│  • SSE connection for live task updates                      │
│  • Per-feature polling for their own task progress           │
└─────────────────────────────────────────────────────────────┘
                              │ HTTP + SSE + Basic Auth
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  FLASK APP (app.py, Waitress)                │
│  /start  /progress/<id>  /cancel/<id>  /get_tasks            │
│  /tasks/stream (SSE)   /system_stats                         │
│  /settings GET/POST   /settings/debug                        │
│  /telegram/log/test   /telegram/log/status                   │
│  /upload_cookies      /upload_token                          │
└─────────────────────────────────────────────────────────────┘
                              │ register_routes(app)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      features/*.py                           │
│  auth.py  settings.py  telegram_logger.py  (+ all features)  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  yt-dlp · ffmpeg · telethon · google-api · tesseract ·       │
│  libtorrent · requests · playwright                          │
└─────────────────────────────────────────────────────────────┘
```

**Key idea:** Every long-running job is a **task** stored as a JSON file in `tasks/`, reported to the UI via SSE or polling. Every runtime toggle lives in `settings.json`, exposed via `/settings`, applied live — no restart.

---

## 3. Folder Structure

```
ProjectCompress/
├── app.py                    # Flask entry, core routes, SSE, system stats
├── config.py                 # Paths, secrets, folder IDs
├── tasks.py                  # Task state + cache + broadcaster
├── requirements.txt
├── install.sh
├── settings.json             # Runtime settings (auto-generated)
├── cookies.txt               # YouTube/site cookies
├── token.json                # Google OAuth (account "default")
├── token_<name>.json         # Additional Drive accounts
├── telegram_creds.json       # Telegram API id/hash
├── .password                 # LEGACY (now /root/.password)
│
├── features/
│   ├── __init__.py
│   ├── auth.py               # Basic Auth (via /root/.password)
│   ├── settings.py           # settings.json API + validation
│   ├── telegram_logger.py    # logs + events → Telegram
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
├── templates/
│   ├── base.html             # Shell (theme switcher + settings panel)
│   ├── index.html
│   ├── _features_macro.html
│   └── features/ (url.html, telegram.html, drive.html,
│                  local.html, video_clipper.html,
│                  proxy_fetcher.html, subtitle_finder.html,
│                  ocr.html, web_crawler.html)
│
├── static/
│   ├── css/
│   │   ├── style.css         # Classic theme
│   │   └── style-modern.css  # Modern animated theme
│   └── js/app.js             # Global SSE, tabs, helpers
│
├── downloads/
│   ├── .thumbnails/
│   ├── playlist_<id>_<q>/
│   ├── clips_<task_id>/
│   ├── frames_<task_id>/
│   └── crack_debug/
│
├── tasks/                    # Task JSON files
├── proxy_cache/
└── venv/ (optional)

External:
/root/.password               # Auth (primary location)
/root/.cache/ms-playwright/   # Playwright browsers
```

---

## 4. How It Was Built

Every new capability follows the same pattern:

1. **Create feature module** `features/my_feature.py` — self-contained with helpers, worker, `register_routes(app)`
2. **Register in `features/__init__.py`** — `from . import my_feature; my_feature.register_routes(app)`
3. **Create UI pane** `templates/features/my_feature.html` — form + JS
4. **Add to `_features_macro.html`** — tab button + pane include
5. **Test end-to-end** — drop a file in → watch progress → see output

---

## 5. The Task System (Core Concept)

Every long-running job is a **task** — UUID-named JSON file in `tasks/`.

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

**Why tasks?** Persistent (survive restarts), inspectable (read by ID), cancellable (flip flag).

**Lifecycle:** `queued → running → done | error | cancelled`

**Writing:**
```python
from tasks import load_task, save_task
task = load_task(task_id)
task['progress'] = 50
save_task(task_id, task)
```

`save_task()` updates cache → writes disk → broadcasts SSE → forwards to Telegram logger.

**Cleanup:** Terminal tasks removed after 10 min (`cleanup_old_tasks()`).

---

## 6. Real-Time Updates (SSE)

One-way stream from server to browser.

**Server (`app.py`):**
```python
@app.route('/tasks/stream')
def tasks_stream():
    q = queue.Queue(maxsize=1)
    add_subscriber(q)
    def event_stream():
        try:
            yield f"data: {json.dumps(get_active_tasks())}\n\n"
            while True:
                try:
                    yield f"data: {q.get(timeout=HEARTBEAT_SECONDS)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            remove_subscriber(q)
    return Response(stream_with_context(event_stream()),
                    mimetype="text/event-stream")
```

**Why not WebSockets?** SSE is simpler, works over plain HTTP, auto-reconnects, one-way is enough here.

**Log noise:** `waitress` logs `Client disconnected` when tabs close. Not an error. Silence:
```python
logging.getLogger('waitress').setLevel(logging.WARNING)
```

---

## 7. Settings System

Central config store powering the settings panel.

**Design:** One file (`settings.json`), one API (`/settings`), live apply, validation, secrets stay server-side.

**Key functions (`features/settings.py`):**
```python
load_settings()                     # raw dict
save_settings(data) -> (ok, err)    # atomic write
get_effective_config()              # settings.json → env → default
```

**Schema:**
```python
ALLOWED_KEYS = {
    'ui_theme':             {'type': 'str',  'choices': ('classic', 'modern')},
    'thumbnails_enabled':   {'type': 'bool'},
    'coordinator_enabled':  {'type': 'bool'},
    'tg_log_enabled':       {'type': 'bool'},
    'tg_log_channel':       {'type': 'str',  'max': 200},
    'tg_log_bot_token':     {'type': 'str',  'max': 200},
    'tg_log_level':         {'type': 'str',
                             'choices': ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')},
}
```

**Value precedence:** `settings.json` → env var → default.

**Endpoints:**
| Method | Path | Purpose |
|---|---|---|
| GET | `/settings` | Config (token masked) + env hints |
| POST | `/settings` | Write subset, apply to subsystems |
| GET | `/settings/debug` | Diagnostic (path, permissions, write test) |

POST handler applies changes:
```python
if 'thumbnails_enabled' in updated:
    from .thumbnails import set_thumbnails_enabled
    set_thumbnails_enabled(current['thumbnails_enabled'])
if 'coordinator_enabled' in updated:
    from .google_drive import set_coordinator_enabled
    set_coordinator_enabled(current['coordinator_enabled'])
if any(k.startswith('tg_log_') for k in updated):
    from .telegram_logger import reconfigure
    reconfigure()
```

`ui_theme` is applied **client-side** — server persists only.

**Diagnostic endpoint:** `/settings/debug` returns path, permissions, `write_test: "ok"|error`. First thing to check when saves fail.

---

## 8. Theme System (Classic vs Modern)

Two complete stylesheets, one active. **Classic is never modified.**

```
static/css/
├── style.css           # Original dark theme
└── style-modern.css    # Animated, glassmorphic
```

**Load (`base.html`):**
```html
<link rel="stylesheet" href="...style.css"        id="theme-classic">
<link rel="stylesheet" href="...style-modern.css" id="theme-modern">
<script>
    (function () {
        var theme = 'classic';
        try { theme = localStorage.getItem('ui-theme') || 'classic'; } catch (e) {}
        if (theme !== 'modern' && theme !== 'classic') theme = 'classic';
        document.documentElement.dataset.theme = theme;
        document.getElementById('theme-classic').disabled = (theme === 'modern');
        document.getElementById('theme-modern').disabled  = (theme !== 'modern');
    })();
</script>
```

Both sheets download once. `disabled` toggling = instant switch, no FOUC.

**Switching:** `window.applyTheme(theme, syncServer)` — called from sys-stats pill and settings panel.

**Persistence:** localStorage (fast, no flash) + settings.json (source of truth).

**Modern-only effects** (spotlight, ripple) gate on `document.documentElement.dataset.theme === 'modern'`.

**GPU note:** `backdrop-filter` and `filter: blur()` are expensive. For low-power devices add a perf-mode toggle:
```css
html[data-perf="1"] *, html[data-perf="1"] *::before, html[data-perf="1"] *::after {
    animation: none !important;
    transition: none !important;
    backdrop-filter: none !important;
    filter: none !important;
}
```

**`prefers-reduced-motion`** respected in both themes.

---

## 9. Telegram Logger

Forwards `WARNING+` logs + task events to a Telegram channel.

**Modes:**
| Mode | When | Session |
|---|---|---|
| **Bot** (recommended) | `tg_log_bot_token` set | `:memory:` — no locks |
| **User** | token empty, session exists | cloned from main |

**Config (`settings.json`):** `tg_log_enabled`, `tg_log_channel`, `tg_log_bot_token`, `tg_log_level`. All fall back to `TG_LOG_*` env vars.

**Channel normalization** — `_normalize_channel()` accepts:
| Input | Normalized |
|---|---|
| `-1004480032378` (str) | `int(-1004480032378)` ✅ |
| `@mychannel` | `'@mychannel'` |
| `https://t.me/mychannel` | `'@mychannel'` |
| `https://t.me/+InviteCode` | `'+InviteCode'` (rejected — bot can't join) |

**Critical:** Telethon bots require **integer** IDs for private channels.

**Worker architecture:**
```
log record → _Handler.emit() → Worker.enqueue()
                                 ↓
                          [asyncio thread]
                                 ↓
                     collect_batch (3s, max 15)
                                 ↓
                     rate-limit (min 3s between sends)
                                 ↓
                     _send() → Telethon → Telegram
                                 ↓
                     retry [0, 5, 15, 60]s on fail
```

**Safety:**
| Concern | Handling |
|---|---|
| Blocking requests | `put_nowait()` — microseconds |
| Flood limits | Batch + min interval ≈ 20/min |
| Chatty `save_task` | Only fires on first-seen + terminal |
| Telegram down | Retry then drop |
| Crashes | Every layer in try/except |
| Circular import | Lazy import inside `save_task()` |
| Database locks | Bot mode uses `:memory:` |
| Long messages | Truncate at 3800 chars |
| HTML parse fail | Fallback to plain text |
| Queue overflow | Drop past 500 |
| Shutdown | `atexit` flushes |

**Endpoints:**
| GET | `/telegram/log/test` | Send test, wait 15s |
| GET | `/telegram/log/status` | State JSON |

**Bot setup:**
1. `@BotFather` → create bot → copy token
2. Add bot as **administrator** of channel with "Post messages"
3. Settings panel → paste channel + token → Save
4. Click "Send test message"

If bot isn't admin → `/telegram/log/test` returns 202, server log shows `ChatWriteForbiddenError`.

---

## 10. Authentication

HTTP Basic Auth on every route, with exempt-list for header-token endpoints.

**Password file resolution order:**
1. `$AUTH_PASSWORD_FILE` (env override)
2. `/root/.password` (primary)
3. `~/.password`
4. `<project_root>/.password` (legacy)

First existing wins. **Fail-closed** if none exist (all 401).

**Setup:**
```bash
sudo sh -c 'echo "YourPassword" > /root/.password'
sudo chmod 600 /root/.password
sudo chown root:root /root/.password
```

**Live reload:** mtime checked each request — edits take effect immediately, no restart.

**Exempt paths:**
```python
EXEMPT_PATHS = {'/favicon.ico', '/upload_cookies',
                '/upload_token', '/processed_list'}
EXEMPT_PREFIXES = ('/mark_processing/', '/mark_done/', '/mark_failed/')
```

**Timing-safe comparison:** `hmac.compare_digest`.

**Nginx option:** If using Nginx basic auth, disable Flask auth to avoid double prompts (remove `auth.register_auth(app)` from `__init__.py`).

---

## 11. Feature Modules

**Skeleton:**
```python
# features/my_feature.py
import os, uuid, threading, logging
from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

def process_task(task_id, input_data):
    task = load_task(task_id)
    task['status'] = 'running'
    save_task(task_id, task)
    try:
        # ... work ... periodically save_task with task['progress']
        # check cancellation: if load_task(task_id).get('cancelled'): return
        task['status'] = 'done'
        task['output_file'] = 'result.mp4'
        save_task(task_id, task)
    except Exception as e:
        logger.exception("Feature failed")
        task = load_task(task_id)
        task['status'] = 'error'
        task['error_msg'] = str(e)
        save_task(task_id, task)

def register_routes(app):
    @app.route('/my_feature/start', methods=['POST'])
    def my_feature_start():
        value = request.form.get('value')
        if not value:
            return jsonify({'error': 'value required'}), 400
        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
        })
        threading.Thread(target=process_task, args=(task_id, value),
                         daemon=True).start()
        return jsonify({'task_id': task_id})
```

**Feature reference:**
| Feature | Purpose | Dep |
|---|---|---|
| `url_download.py` | URL/magnet/torrent/playlist download | yt-dlp, requests |
| `video_clipper.py` | Clips, frames, summary, trim | ffmpeg |
| `ocr.py` | Text extraction | pytesseract |
| `telegram.py` | Chat scan/download/upload | telethon |
| `google_drive.py` | Drive browse/download/upload | google-api |
| `local_files.py` | File browser | Flask |
| `web_crawler.py` | Domain discovery | requests, bs4 |
| `thumbnails.py` | Video thumbnails | ffmpeg |
| `auth.py` | Basic Auth | Flask |
| `settings.py` | Config store | Flask |
| `telegram_logger.py` | Log forwarding | telethon |

**`__init__.py` order matters:**
```python
def register_all_features(app):
    telegram_logger.register_logging(app)  # FIRST — captures startup logs
    auth.register_auth(app)                # Early — before_request hook
    # ...all feature routes...
    settings.register_routes(app)          # Last — POSTs need subsystems ready
    telegram_logger.register_routes(app)
```

**Special:** `features/telegram.py` has `resolve_entity(client, chat_link)` — always use it instead of raw `get_entity()`. Handles all link formats, auto-joins, returns readable errors.

---

## 12. Frontend Structure

**`base.html` contains:**
- System-stats bar (CPU/RAM/Disk/Net + Pause Thumbs + Theme toggle)
- Header + tab bar
- Settings FAB (right edge) + slide-out panel
- Active tasks panel
- Early theme script (prevents FOUC)
- Settings panel logic
- Modern-UI effects (gated on theme)
- `<script src="app.js">`

**`_features_macro.html`:** `render_tabs()` + `render_panes()` macros.

**`app.js` responsibilities:** SSE connection, task rendering, tab switching, helpers (`showToast`, `escapeHtml`, `formatBytes`), per-feature polling.

**Per-feature JS:** inside each pane — form submission, own task polling, own result UI.

**Settings panel structure:**
```html
<button id="settingsFab">⚙</button>
<div id="settingsOverlay"></div>
<aside id="settingsPanel">
    <header>...</header>
    <div class="settings-body">
        <section>Appearance (ui_theme)</section>
        <section>Telegram (enabled/channel/token/level + test)</section>
        <section>Colab Coordinator</section>
        <section>Thumbnails</section>
    </div>
    <footer>Save / Reload</footer>
</aside>
```

Panel CSS is inline in `base.html` (theme-independent). JS: loads `/settings` on open, diffs on save (only changed keys), displays field errors via `#setSaveStatus`, applies `ui_theme` locally after save, syncs thumbnails with sys-stats.

---

## 13. How to Add a New Feature

Example: "MP3 Converter".

1. **Create `features/mp3_converter.py`** with worker + `register_routes(app)` (see §11 skeleton).
2. **Register in `features/__init__.py`:**
   ```python
   from . import mp3_converter
   # inside register_all_features:
   mp3_converter.register_routes(app)
   ```
3. **Create `templates/features/mp3_converter.html`** — form + JS polling `/progress/<task_id>`.
4. **Add to `_features_macro.html`:**
   ```html
   <button class="tab-btn" data-tab="mp3">
       <i class="fas fa-music"></i> MP3
   </button>
   <div id="mp3-tab" class="tab-pane">
       {% include 'features/mp3_converter.html' %}
   </div>
   ```
5. **Restart Flask** → feature live, uses standard task system, auto-integrates with global panel + Telegram logger.

---

## 14. How to Add a New Setting

Six steps for a new toggle (example: `whisper_model`).

**1. Schema in `features/settings.py`:**
```python
ALLOWED_KEYS = {
    'whisper_model': {'type': 'str',
                      'choices': ('tiny', 'base', 'small', 'medium', 'large')},
}
```

**2. `get_effective_config()`:**
```python
'whisper_model': s.get('whisper_model', 'base'),
```

**3. UI in `base.html` (`settings-body`):**
```html
<section class="settings-section">
    <h3><i class="fas fa-microphone"></i> Whisper</h3>
    <label class="settings-row">
        <span>Model</span>
        <select id="setWhisperModel">
            <option value="tiny">tiny</option>
            <option value="base">base</option>
            <option value="small">small</option>
            <option value="medium">medium</option>
            <option value="large">large</option>
        </select>
    </label>
</section>
```

**4. Panel JS:**
- In `load()`: `inWhisperModel.value = current.whisper_model || 'base';`
- In save handler: `if (inWhisperModel.value !== current.whisper_model) payload.whisper_model = inWhisperModel.value;`

**5. Optional subsystem side effect** (in `settings_post`):
```python
if 'whisper_model' in updated:
    from .whisper import set_model
    set_model(current['whisper_model'])
    applied.append('whisper')
```

**6. Done** — new key appears on next `/settings` GET, applies on next POST, no restart.

---

## 15. How to Maintain the Project

**Daily/weekly:**
- `ls tasks/*.json | wc -l` — should stay small (auto-cleanup daily)
- Check `downloads/` size
- Check `cookies.txt` freshness (YouTube cookies expire ~weeks)
- `curl /telegram/log/status` — confirm queue=0, dropped=0
- Restart Flask periodically to clear memory

**Monthly:**
- `pip install --break-system-packages -U yt-dlp` (YouTube breaks it often)
- Re-run `install.sh` if ffmpeg updated
- Empty Drive trash
- Rotate `/root/.password`
- Rotate bot tokens (`@BotFather` → paste via settings panel)
- `journalctl -u projectcompress -n 200` — review errors

**When YouTube fails:** export fresh cookies → upload → test `yt-dlp --cookies cookies.txt <url>`.

**When tasks hang:** read `tasks/<id>.json`, check `ps aux | grep -E "ffmpeg|yt-dlp"`, cancel via UI.

**When settings won't save:** `/settings/debug` first. `write_test ≠ "ok"` = permission issue. `file_exists: false` + `dir_writable: true` = route not registered (check `__init__.py`).

**When Telegram silent:** `curl /telegram/log/status` → `/telegram/log/test`. If 202: bot not admin. If disabled: `tg_log_enabled` off in settings.

**When scans fail:** `Cannot get entity` — `resolve_entity` should auto-join. If it can't, error explains why (approval-only, expired invite, banned).

---

## 16. Common Debugging

| Symptom | Cause / Fix |
|---|---|
| Task stuck in `queued` | Thread never started — check `.start()` |
| Progress but no file | `output_file` not set at worker end |
| SSE errors in console | Check `/tasks/stream` Content-Type; Nginx needs `proxy_buffering off` |
| `ffmpeg not found` | Prepend `/usr/local/bin` to `PATH` in worker |
| Cancel doesn't work | Worker must check `load_task(id).get('cancelled')` periodically |
| Settings panel "Failed to load" | `curl -i /settings` — 404 = route missing, 500 = check log, 401 = Basic Auth blocking |
| `database is locked` (Telethon) | Bot mode uses `:memory:` — switch or ensure single client per session |
| `Cannot get entity by phone number as a bot` | Passed string ID to bot — normalize to `int` |
| `/telegram/log/test` returns 202 | Message queued, not confirmed — check `ChatWriteForbiddenError` (bot not admin) |

**Verbose logging:** `logging.basicConfig(level=logging.DEBUG)`.

**Silence noise:**
```python
logging.getLogger('telethon.network.mtprotosender').setLevel(logging.WARNING)
logging.getLogger('telethon.network.connection').setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.WARNING)
```

---

## 17. Conventions & Best Practices

**Python:**
- One feature = one file
- All workers threaded, `daemon=True`
- Always check cancellation in loops
- Use `load_task()` / `save_task()` — never write task JSON directly
- Catch every exception in worker, set `status: 'error'`
- `logger.exception()` for failures, `logger.info()` for progress
- `os.path.join()` everywhere
- Never trust user input (validate paths, URLs)
- Never import `features.telegram_logger` at module top in `tasks.py` (circular)

**Settings:**
- All runtime toggles in `settings.json` via `features/settings.py`
- Never expose secrets in GET responses (mask tokens)
- Validate on server, not just client
- Apply live — call subsystem `set_*` / `reconfigure` from `settings_post`

**Path safety:**
```python
if '..' in user_path or user_path.startswith('/'):
    return jsonify({'error': 'Invalid path'}), 400
```

**Frontend:**
- One feature = one HTML pane
- Always `escapeHtml()` user-controlled strings
- Use `showToast()` not `alert()`
- Poll every 2–5s, not more
- Use `storeTaskId()` / `startPollingForTask()` for page-reload survival
- Theme-gate JS effects on `document.documentElement.dataset.theme`

**Git:** never commit `cookies.txt`, `token*.json`, `telegram_creds.json`, `settings.json`, `telegram_*_session*`, `downloads/`, `tasks/`, `proxy_cache/`, `venv/`.

**Security:**
- Basic Auth by default — also use HTTPS (Basic over HTTP is trivial to intercept)
- Treat `COOKIE_UPLOAD_TOKEN`, `tg_log_bot_token`, `/root/.password` as real secrets
- `hmac.compare_digest` for all secret comparisons

---

## 18. Deployment

**One-liner:**
```bash
git clone https://github.com/blackystrng/ProjectCompress.git
cd ProjectCompress
chmod +x install.sh
sudo ./install.sh
```

**Set up auth:**
```bash
sudo sh -c 'echo "YourPassword" > /root/.password'
sudo chmod 600 /root/.password
```

**Foreground:** `python3 app.py`

**Systemd** (`/etc/systemd/system/projectcompress.service`):
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
```

**If running as non-root**, move password file:
```bash
sudo mv /root/.password /etc/projectcompress.password
sudo chown <app-user>:<app-user> /etc/projectcompress.password
```
Then add `Environment="AUTH_PASSWORD_FILE=/etc/projectcompress.password"`.

**Env vars:**
```env
SECRET_KEY=long-random-string
PROXY_URL=http://user:pass@host:8080
DRIVE_FOLDER_ID=1abc...xyz
COOKIE_UPLOAD_TOKEN=openssl-rand-hex-32
TG_LOG_ENABLED=false
TG_LOG_CHANNEL=
TG_LOG_BOT_TOKEN=
TG_LOG_LEVEL=WARNING
```

**Nginx (with HTTPS):**
```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}
server {
    listen 443 ssl http2;
    server_name your-domain.com;
    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;         # REQUIRED for SSE
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

**Critical:** `proxy_buffering off` — otherwise SSE silently breaks.

---

## 🎓 Mental Model — One Page Summary

1. **Flask app** registers all feature routes at startup.
2. **Every long job = a task** (JSON file with status/progress/output).
3. **`save_task()`** = cache → disk → SSE broadcast → Telegram logger.
4. **UI** = tabs, task panel, settings gear on right, theme switcher in stats bar.
5. **Settings** = `settings.json` source of truth; `/settings` API; subsystem `set_*` applies live; secrets server-side only.
6. **Themes** = two CSS files, one disabled at a time; localStorage + settings.json; modern effects gate on `dataset.theme`.
7. **Auth** = `/root/.password` (or env), mtime-checked, fail-closed.
8. **Logger** = bot mode `:memory:` (no locks); user mode clones session; batch + rate-limit + retry; non-blocking.
9. **Adding features** = new file + register + pane + macro entry.
10. **Adding settings** = schema + UI + JS wiring + optional subsystem hook.
11. **Debug** = read task JSON, check logs, hit `/settings/debug` and `/telegram/log/status`.
