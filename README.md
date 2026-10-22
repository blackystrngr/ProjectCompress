# 🎬 ProjectCompress

**A self-hosted media downloader, converter, and workflow automation platform built with Flask.**

Download from any URL · Compress videos · Automate with node-based workflows · Send to Telegram · Manage Google Drive · OCR · Crawl · Monitor system stats — all from one dashboard.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-latest-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-latest-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

---

## 📖 Table of Contents

- [What's New](#-whats-new)
- [Features](#-features)
- [Quick Start](#-quick-start)
- [Configuration](#%EF%B8%8F-configuration)
- [Authentication](#-authentication)
- [Settings Panel](#%EF%B8%8F-settings-panel)
- [UI Themes](#-ui-themes)
- [Workflow Automation](#-workflow-automation)
- [Telegram Logging](#-telegram-logging)
- [Running](#-running)
- [Project Structure](#-project-structure)
- [Usage Examples](#-usage-examples)
- [Troubleshooting](#-troubleshooting)
- [Security](#-security)
- [Tech Stack](#-tech-stack)
- [License](#-license)

---

## ✨ What's New

Recent additions to ProjectCompress (v2):

- 🎨 **Dual-theme UI** — Classic + animated Modern, switch anytime, no page reload
- ⚙️ **Settings panel** — All runtime toggles in one place, live-apply, no restart
- 🔀 **Node-based workflows** — Drag-drop canvas with wires; chain download → telegram → delete automatically
- 📱 **Telegram logging** — Forward `WARNING+` logs and task events to a Telegram channel
- 🔐 **Basic Auth** — From `/root/.password`, edit live without restart
- 🚀 **Auto-join Telegram channels** — Handles invite links, private IDs, public usernames
- 🌐 **Universal URL cracker** — 15-strategy video detection with Playwright sniffing
- 📊 **Real-time SSE task panel** — No polling, instant updates

---

## 🎯 Features

### 📥 Universal Downloads
- **Any file type** — videos, PDFs, ZIPs, images, audio, APKs
- **Video sites** — YouTube, Vimeo, TikTok, Twitter/X, Instagram, and 1000+ more via `yt-dlp`
- **Streams** — HLS (`.m3u8`), DASH (`.mpd`)
- **Torrents** — magnet links & `.torrent` files (requires `libtorrent`)
- **Playlists** — full or ranged download with resume support
- **Quality control** — 360p → 4K (2160p), plus audio-only
- **Chrome impersonation + cookies** for Cloudflare/age-restricted content
- **Auto folder reuse** — same playlist URL resumes from where it stopped
- **Video cracker** — extracts media from pages without direct URLs

### 🎬 Video Tools
- **Random clip generator** — split into segments, pick random clips, merge
- **AI summarizer** — evenly spaced clips into a short summary
- **Frame extractor** — extract frames every N seconds → ZIP
- **Trim / crop by timestamp** — precise `HH:MM:SS` cuts
- **Compression** — H.265 (x265) or AV1 with CRF and preset control
- **Face swap** — photo → video via Colab integration

### 🔀 Workflow Automation
- **Node-based canvas** — drag nodes, connect with wires
- **10 node types** — trigger, download, compress, telegram, drive, wait, webhook, delete
- **Implicit foreach** — trigger URLs run through the whole chain
- **Visual progress** — live per-node status and progress bars
- **Template placeholders** — `{filename}`, `{url}`, `{size}`, `{index}` in captions/webhooks
- **Saved workflows** — build once, run forever

### 📤 Uploads & Storage
- **Telegram** — scan chats, download videos, send files (as documents)
- **Google Drive** — list, download, upload, delete with multi-account support
- **Local file browser** — preview, download, delete, batch-send files

### 🛠 Utilities
- **OCR** — extract text from images/PDFs (Tesseract, 20+ languages)
- **Subtitles** — search & download from OpenSubtitles
- **Proxy fetcher** — scrape and test live HTTP/SOCKS proxies
- **Web crawler** — recursive domain discovery with SSE streaming

### 📊 Monitoring & Control
- **Live stats bar** — CPU, RAM, Disk, Upload/Download speed
- **Real-time task panel** — SSE-driven, no polling
- **Settings panel** — right-side slide-out with live-apply
- **Telegram logging** — forward warnings + task events to a channel
- **Auto thumbnails** — extracted from the last 30 seconds of each video
- **Thumbnail pause** — save CPU during bulk operations

---

## 🚀 Quick Start

### One-Command Install (Ubuntu/Debian)

```bash
git clone https://github.com/blackystrng/ProjectCompress.git
cd ProjectCompress
chmod +x install.sh
sudo ./install.sh
```

The script installs and configures:

- ✅ **ffmpeg** (static build from BtbN)
- ✅ **Node.js 20.x** (for POT provider)
- ✅ **Deno** (JS runtime for yt-dlp challenges)
- ✅ **yt-dlp + yt-dlp-ejs + curl_cffi**
- ✅ **bgutil-ytdlp-pot-provider** (as systemd service)
- ✅ **Playwright + Chromium** (for URL cracker)
- ✅ **All Python dependencies** with `--break-system-packages`

### Manual Install

```bash
# System packages
sudo apt update
sudo apt install -y python3-pip ffmpeg wget curl git nodejs npm

# Python packages
pip install --break-system-packages -r requirements.txt
pip install --break-system-packages yt-dlp yt-dlp-ejs curl_cffi bgutil-ytdlp-pot-provider

# Playwright (for URL cracker)
playwright install chromium

# Optional: torrent support
sudo apt install -y python3-libtorrent

# OCR support
sudo apt install -y tesseract-ocr tesseract-ocr-eng poppler-utils
```

### Set Up Authentication

```bash
# Create the password file
sudo sh -c 'echo "YourStrongPassword" > /root/.password'
sudo chmod 600 /root/.password
sudo chown root:root /root/.password
```

### Run

```bash
python3 app.py
```

Open `http://YOUR_SERVER_IP:5000` in a browser. You'll be prompted for Basic Auth.

---

## ⚙️ Configuration

### Required files

| File | Purpose | How to get |
|---|---|---|
| `/root/.password` | Basic Auth password | `echo "password" > /root/.password` |
| `cookies.txt` | YouTube/site cookies | Browser extension (see below) |
| `token.json` | Google Drive OAuth | See Drive section |
| `token_<name>.json` | Additional Drive accounts | Same as above |
| `telegram_creds.json` | Telegram API id/hash | See Telegram section |

### YouTube Cookies — `cookies.txt`

For age-restricted or login-only videos:

1. Install the **"[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)"** extension
2. Log in to YouTube
3. Export cookies for `youtube.com`
4. Save as `cookies.txt` in the project root

**Better:** use the built-in browser extension to auto-upload cookies daily. Set `COOKIE_UPLOAD_TOKEN` env var and configure the extension with your VPS URL.

### Google Drive — `token.json`

Only needed for Drive features:

```bash
python3 -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file('credentials.json',
    ['https://www.googleapis.com/auth/drive'])
creds = flow.run_console()
open('token.json', 'w').write(creds.to_json())
"
```

For **multiple Drive accounts**, save as `token_acct1.json`, `token_acct2.json`, etc. The first (`token.json`) is treated as `default`.

### Telegram — `telegram_creds.json`

```json
{
  "api_id": 12345678,
  "api_hash": "your_api_hash_here"
}
```

Get credentials from [my.telegram.org/apps](https://my.telegram.org/apps).

On first run, you'll be prompted (in the terminal) for your phone number and the login code Telegram sends you. This creates `telegram_session.session`.

### Environment Variables (`.env`, optional)

```env
# Flask
SECRET_KEY=generate-with-openssl-rand-hex-32

# Network
PROXY_URL=http://user:pass@proxy.example.com:8080

# Google Drive
DRIVE_FOLDER_ID=1abc...xyz

# Cookie upload token (for browser extension)
COOKIE_UPLOAD_TOKEN=generate-with-openssl-rand-hex-32

# Telegram logger (fallbacks if not in settings.json)
TG_LOG_ENABLED=false
TG_LOG_CHANNEL=
TG_LOG_BOT_TOKEN=
TG_LOG_LEVEL=WARNING

# Custom password file location (overrides /root/.password)
# AUTH_PASSWORD_FILE=/etc/projectcompress.password
```

### `settings.json` (auto-generated)

Created on first save via the Settings panel. Contains:

```json
{
  "ui_theme": "classic",
  "thumbnails_enabled": true,
  "coordinator_enabled": true,
  "tg_log_enabled": false,
  "tg_log_channel": "",
  "tg_log_bot_token": "",
  "tg_log_level": "WARNING"
}
```

`settings.json` **overrides** env vars when set. Env vars act as fallbacks.

---

## 🔐 Authentication

HTTP Basic Auth is enabled by default on every route.

### Password file resolution order

1. `$AUTH_PASSWORD_FILE` (env override)
2. `/root/.password` ← **primary location**
3. `~/.password`
4. `<project_root>/.password` (legacy)

First existing file wins. **Fail-closed** if none exist — all requests return 401.

### Live reload

The file's mtime is checked on every request. Editing `/root/.password` takes effect **immediately** — no restart needed.

### Behind Nginx

If you add Basic Auth in Nginx, disable Flask's auth to avoid double prompts:

1. Comment out `auth.register_auth(app)` in `features/__init__.py`
2. Add to Nginx config:
   ```nginx
   auth_basic "Restricted";
   auth_basic_user_file /etc/nginx/.htpasswd;
   ```

**Do not run both** unless you want the user to see two password prompts.

---

## ⚙️ Settings Panel

Click the ⚙ gear on the right edge of any page to open the settings panel.

### What you can configure live

| Section | Options |
|---|---|
| **Appearance** | UI theme (Classic / Modern) |
| **Telegram Logging** | Enable, channel, bot token, log level, test button |
| **Colab Coordinator** | Cross-drive dedup toggle |
| **Thumbnails** | Generate thumbnails on/off |

### Behavior

- Changes are **saved to `settings.json`** and applied immediately
- **No server restart required**
- **Bot token is masked** in API responses — never sent back in plaintext
- **Field-level validation** — bad values rejected with clear error messages
- **Failed saves** show exactly what went wrong (which field, which error)

### Debug endpoint

If something doesn't save, hit `/settings/debug`:

```bash
curl http://localhost:5000/settings/debug -u username:password
```

Returns:

```json
{
  "settings_file": "/root/ProjectCompress/settings.json",
  "file_exists": true,
  "dir_writable": true,
  "file_writable": true,
  "write_test": "ok",
  "contents": {"ui_theme": "modern", ...},
  "env": {"TG_LOG_CHANNEL": null, ...}
}
```

If `write_test` isn't `"ok"`, the exact failure is in the string.

---

## 🎨 UI Themes

Two complete stylesheets, **one active at a time**. No page reload when switching.

### Classic (default)

Original dark theme — same as previous versions. Fast, minimal, no animations.

### Modern (animated)

- Living aurora background
- Glassmorphic cards with gradient borders
- Mouse-following spotlight
- Click ripple effects
- Springy hover animations
- Animated gradient headings
- Moving-stripe progress bars

### How it works

Both CSS files load at startup but only one is `disabled` at a time:

```html
<link rel="stylesheet" href="style.css" id="theme-classic">
<link rel="stylesheet" href="style-modern.css" id="theme-modern">
```

Switching toggles the `disabled` property → instant, no re-fetch, no cascade conflicts.

### Persistence

- **localStorage** — instant restore on reload (no flash of wrong theme)
- **settings.json** — cross-device sync, source of truth

### Performance note

The Modern theme uses `backdrop-filter` and `filter: blur()` for glass effects. On low-power devices (Colab VMs, older laptops), these can eat GPU. If the UI feels slow:

1. Edit `static/css/style-modern.css` to remove those properties, OR
2. Add a perf-mode toggle (documented in `dev.md`)

Both themes respect `prefers-reduced-motion` — users with that preference get zero animations.

---

## 🔀 Workflow Automation

Build visual pipelines: **trigger → action → action → action**.

### The idea

A **trigger** provides a list of URLs. Each **action** node runs once per URL, passing context down the chain. Perfect for:

- Download video → send to Telegram → delete local file
- Download → compress → upload to Drive
- Download → webhook to Slack
- Batch-download with rate limiting between requests

### How to use

1. Open the **Workflows** tab
2. Click **URL List** in the left palette → appears on canvas
3. Click the node → paste URLs in the right inspector (one per line)
4. Click **Download Video** → appears on canvas
5. **Drag from the trigger's output ● to download's input ●** → wire connects, order badges appear
6. Add more nodes, connect them the same way
7. Click **Run** → watch live progress per node + execution log below

### Interaction reference

| Action | Gesture |
|---|---|
| Move node | Click + drag node body |
| Select node | Click (no drag) |
| Add node | Click palette item |
| Connect nodes | Drag from output ● → input ● |
| Delete wire | Click on the wire |
| Delete node | Delete key when selected · or inspector's Delete button |
| Pan canvas | Drag empty background |
| Zoom | Scroll wheel or +/−/⤢ buttons |

### Available node types

| Node | What it does |
|---|---|
| **URL List** (trigger) | Provides URLs, sets quality |
| **Download Video** | yt-dlp for the current URL |
| **Download File** | Raw HTTP download (PDF, ZIP, etc) |
| **Send to Telegram** | Upload file as document |
| **Upload to Drive** | Upload to Colab_Processing |
| **Compress Video** | Re-encode with x265 or AV1 |
| **Delete Local File** | Remove from disk |
| **Delete Drive File** | Remove from Drive |
| **Wait** | Pause N seconds (rate limiting) |
| **Webhook** | HTTP POST/GET with template body |

### Rules enforced by backend

- **Exactly one trigger** with 0 incoming wires
- **Every action node** must have exactly 1 incoming wire
- **No cycles** (topological sort check)
- **Trigger must be topologically first**

Invalid graphs produce a specific error explaining what to fix.

### Template placeholders

Use in captions, webhook bodies, etc:

| Placeholder | Value |
|---|---|
| `{url}` | Current URL |
| `{filename}` | Downloaded filename |
| `{basename}` | Filename without extension |
| `{ext}` | File extension |
| `{size}` | Human-readable size |
| `{index}` | Current item number (1-based) |
| `{total}` | Total item count |

Example caption: `📁 Downloaded {filename} ({size}) — item {index}/{total}`

### Storage

Workflows save to `workflows/<id>.json` at project root. Add to `.gitignore`:

```gitignore
workflows/
```

### Migration from v1

Old workflows (v1 linear array) auto-convert to v2 (positioned + wires) on first load. No manual intervention needed.

---

## 📱 Telegram Logging

Forward `WARNING+` logs and task lifecycle events to a Telegram channel.

### Bot mode (recommended)

1. Create bot via [@BotFather](https://t.me/BotFather) → copy token
2. Create or choose a channel
3. **Add the bot as administrator** with **"Post messages"** permission
4. Open Settings panel → **Telegram Logging** → enable
5. Paste channel (`@channel` or `-100...`) + bot token
6. Set log level (default: `WARNING`)
7. Click **Send test message** → confirm in channel

Uses the raw Telegram Bot HTTP API — **no `telegram_creds.json` needed, no session file, no Telethon** for the logger itself. Perfectly stateless.

### What you'll receive

**Task events** (always sent, regardless of level):

- `▶️ New task` — when a new task starts
- `✅ Task done` — with output filename
- `❌ Task error` — with error message
- `⛔ Task cancelled` — with task ID

**Log messages** (`WARNING+` by default):

- `⚠️ [WARNING] features.url_download` — failed fetch
- `❌ [ERROR] features.telegram` — send failure
- `🔥 [CRITICAL] __main__` — unhandled exception

### Rate limiting

- Batches messages in 3-second windows
- Minimum 3 seconds between sends
- ~20 messages/min maximum (well below Telegram limits)
- Retries failed sends `[5s, 15s, 60s]`

### Debug endpoint

```bash
curl http://localhost:5000/telegram/log/status -u username:password
```

Shows current state, queue size, dropped count.

```bash
curl http://localhost:5000/telegram/log/test -u username:password
```

Sends test message synchronously — returns `200` if sent, `502` with exact reason if Telegram rejected.

---

## 🏃 Running

### Foreground

```bash
python3 app.py
```

### Systemd (recommended)

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

### Non-root deployment

If running as a user other than root, `/root/.password` won't be readable. Move it:

```bash
sudo mv /root/.password /etc/projectcompress.password
sudo chown <app-user>:<app-user> /etc/projectcompress.password
sudo chmod 600 /etc/projectcompress.password
```

Then add to the service file:

```ini
Environment="AUTH_PASSWORD_FILE=/etc/projectcompress.password"
```

### Behind Nginx (HTTPS + SSE)

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
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # REQUIRED for SSE (server-sent events) to work
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

**Critical:** `proxy_buffering off` — otherwise the task panel silently fails to update.

---

## 📁 Project Structure

```
ProjectCompress/
├── app.py                    # Flask entry, core routes, SSE, system stats
├── config.py                 # Paths, secrets, folder IDs
├── tasks.py                  # Task state + cache + broadcaster
├── requirements.txt
├── install.sh
│
├── settings.json             # Runtime settings (auto-generated)
├── cookies.txt               # Site cookies
├── token.json                # Google Drive (default account)
├── token_<name>.json         # Additional Drive accounts
├── telegram_creds.json       # Telegram API id/hash
│
├── features/
│   ├── __init__.py
│   ├── auth.py               # Basic Auth via /root/.password
│   ├── settings.py           # settings.json API
│   ├── telegram_logger.py    # Log forwarding to Telegram
│   ├── workflow.py           # Node-based workflow engine
│   ├── url_download.py       # Universal downloader + cracker
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
│   ├── base.html             # Main shell + settings panel
│   ├── index.html
│   ├── _features_macro.html  # Tab + pane macros
│   └── features/
│       ├── url.html
│       ├── telegram.html
│       ├── drive.html
│       ├── local.html
│       ├── video_clipper.html
│       ├── workflow.html     # Node-based canvas
│       ├── proxy_fetcher.html
│       ├── subtitle_finder.html
│       ├── ocr.html
│       └── web_crawler.html
│
├── static/
│   ├── css/
│   │   ├── style.css         # Classic theme
│   │   └── style-modern.css  # Modern animated theme
│   └── js/
│       └── app.js            # Global SSE, tabs, helpers
│
├── downloads/                # User files
│   ├── .thumbnails/
│   ├── playlist_<id>_<q>/    # Resume-capable playlist folders
│   ├── clips_<task_id>/      # Temp clip fragments
│   ├── frames_<task_id>/     # Temp extracted frames
│   └── crack_debug/          # HTML snapshots from URL cracker
│
├── workflows/                # Saved workflows (v2 format)
├── tasks/                    # Task state JSON (auto-cleaned)
└── proxy_cache/              # Cached proxy results

External:
/root/.password               # Auth password
/root/.cache/ms-playwright/   # Playwright browsers
```

---

## 🎯 Usage Examples

<details>
<summary><b>Download a YouTube video at 1080p</b></summary>

1. Paste URL into **URL Download**
2. Select **1080p** from the quality dropdown
3. Click **Download** — live progress + speed shown
4. File appears in **My Files**

</details>

<details>
<summary><b>Download a full YouTube playlist (with resume)</b></summary>

1. Paste playlist URL — the range panel appears automatically
2. Choose **Full playlist** or **Range: X to Y**
3. Pick quality
4. Click **Download**

Same URL + same quality → same folder → **skips already-downloaded videos** on re-runs.

Files are named by original playlist position: `17 - Video Title [id].mp4`

</details>

<details>
<summary><b>Download any file (PDF, ZIP, EXE, APK)</b></summary>

1. Paste the direct URL
2. Click **Download** — auto-detects and uses the right method

</details>

<details>
<summary><b>Build a "download → telegram → delete" workflow</b></summary>

1. Open **Workflows** tab
2. Click **URL List** in the palette → paste URLs → save
3. Click **Download Video** → drag wire from trigger's output to download's input
4. Click **Send to Telegram** → connect from download → fill in chat link
5. Click **Delete Local File** → connect from telegram
6. Click **Run** → each URL flows through the chain automatically

Live progress shows per node. The execution log shows exactly what happened.

</details>

<details>
<summary><b>Extract frames from a video</b></summary>

1. Open **Video Clipper** → **Extract Frames**
2. Choose video, set interval (e.g., 5s), pick JPG/PNG
3. Click **Extract** → ZIP file created when done

</details>

<details>
<summary><b>Send files to Telegram as documents</b></summary>

1. Open **My Files**
2. Select files → **Send to Telegram**
3. Enter chat link → files uploaded (as documents, not video previews)

</details>

<details>
<summary><b>Forward error logs to a Telegram channel</b></summary>

1. Create a bot via `@BotFather`
2. Add it as admin of the channel with "Post messages" permission
3. Open Settings panel → **Telegram Logging**
4. Enable, paste channel + bot token, save
5. Click **Send test message** → confirm in channel
6. From now on: `WARNING+` logs + task events arrive in the channel

</details>

<details>
<summary><b>Pause thumbnail generation to save CPU</b></summary>

Settings panel → **Thumbnails** → uncheck "Generate thumbnails". Existing thumbnails keep showing; no new ones generate until you re-enable.

</details>

<details>
<summary><b>Switch between Classic and Modern UI</b></summary>

Settings panel → **Appearance** → select UI Theme → **Save**.

Or click the theme switcher pill in the top-right if you haven't removed it. The setting persists in localStorage (instant on reload) and `settings.json` (cross-device).

</details>

---

## 🔧 Troubleshooting

| Issue | Fix |
|-------|-----|
| `Sign in to confirm you're not a bot` | Export fresh `cookies.txt`, or use the browser extension |
| `ffmpeg not found` | Run `install.sh` or `sudo apt install ffmpeg` |
| `POT provider not responding` | `sudo systemctl restart bgutil-provider` |
| Telegram `Cannot get entity from a channel` | Auto-join handles this — if it fails, open the channel once in Telegram first |
| `database is locked` from Telethon | Only in user mode; switch logger to bot mode (uses `:memory:`) |
| `Cannot get entity by phone number as a bot` | Bot requires numeric channel IDs — use `-100...` not `@username` for private channels |
| Google Drive token expired | Delete `token.json` and re-authorize |
| Port 5000 already in use | `sudo fuser -k 5000/tcp` then restart |
| Thumbnails not showing | Ensure `ffprobe` is in `PATH`, check "Generate thumbnails" is enabled in Settings |
| `Requested format is not available` | Update yt-dlp: `pip install --break-system-packages -U yt-dlp` |
| High CPU after playlist finishes | Use Settings → **Thumbnails** → uncheck to pause generation |
| Settings panel won't save | Hit `/settings/debug` to diagnose — check `write_test: "ok"` |
| Telegram logging silent | `curl /telegram/log/test` for immediate status + `curl /telegram/log/status` for state |
| SSE connection errors in console | Nginx needs `proxy_buffering off` — check reverse proxy config |
| Wrong theme flashing on load | The early script should prevent it — clear localStorage and reload |
| Workflow won't run | Check all nodes are connected in one chain: exactly one incoming wire per action node |
| Task history cluttering list | Auto-cleaned daily (terminal tasks > 1 day old) |

### Debug commands

```bash
# Is the app running?
curl http://localhost:5000/ -u username:password

# Settings health
curl http://localhost:5000/settings/debug -u username:password

# Telegram logger state
curl http://localhost:5000/telegram/log/status -u username:password

# Test Telegram send
curl http://localhost:5000/telegram/log/test -u username:password

# Recent server logs
sudo journalctl -u projectcompress -n 200 --no-pager
```

### Silent log noise

To silence common non-error chatter:

```python
# Add to app.py after logging.basicConfig()
logging.getLogger('waitress').setLevel(logging.WARNING)
logging.getLogger('telethon.network.mtprotosender').setLevel(logging.WARNING)
logging.getLogger('telethon.network.connection').setLevel(logging.WARNING)
```

These are normal reconnect/heartbeat messages, not errors.

---

## 🔒 Security

⚠️ **This app runs code from external sources and has Basic Auth by default.** If exposing to the internet:

1. **Use HTTPS** — Basic Auth over HTTP is trivial to intercept
2. **Use a strong password** — `openssl rand -base64 32` for `/root/.password`
3. **Restrict access** via firewall or WireGuard VPN if possible
4. **Never commit** `cookies.txt`, `token*.json`, `telegram_creds.json`, `settings.json`, `telegram_*_session*`
5. **Keep yt-dlp updated** — it processes untrusted media URLs

### Recommended `.gitignore`

```gitignore
# Secrets
.password
cookies.txt
cookies.txt.bak
token*.json
credentials.json
telegram_creds.json
telegram_*_session*
telegram_session*
.env
settings.json

# Runtime
tasks/
downloads/
proxy_cache/
workflows/
venv/

# Python
__pycache__/
*.pyc
*.pyo
.pytest_cache/
```

### What runs with your permissions

- `yt-dlp` — downloads arbitrary URLs (safe but keep updated)
- `ffmpeg` — encodes media files (safe)
- `playwright` — headless Chromium (sandboxed)
- `tesseract` — OCR (safe)
- `telethon` — Telegram client (requires your session)
- `libtorrent` — P2P downloads (opens local ports)

If you're concerned about any of these, disable the corresponding feature by removing its `register_routes()` call in `features/__init__.py`.

---

## 🧰 Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Flask 3, Waitress |
| **Frontend** | Vanilla JS, SSE, CSS |
| **Media** | FFmpeg, yt-dlp, Deno |
| **Downloads** | requests, libtorrent, Playwright |
| **Storage** | Google Drive API, Telegram (Telethon) |
| **OCR** | Tesseract, pdf2image |
| **Monitoring** | psutil |
| **POT Provider** | bgutil-ytdlp-pot-provider (Node.js) |

---

## 📄 License

MIT License — see [LICENSE](LICENSE) file for details.

---

## 🙏 Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — Video downloader
- [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) — YouTube PO Token provider
- [Telethon](https://github.com/LonamiWebs/Telethon) — Telegram client
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) — OCR engine
- [BtbN FFmpeg Builds](https://github.com/BtbN/FFmpeg-Builds) — Static FFmpeg
- [Playwright](https://playwright.dev/) — Headless browser for URL cracking

---

<div align="center">

**⭐ Star this repo if you find it useful!**

*Use responsibly and respect copyright laws in your jurisdiction.*

</div>
