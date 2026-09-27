# 🎬 ProjectCompress

**A self-hosted media downloader, converter, and organizer built with Flask.**

Download from any URL · Extract frames · Swap faces · Scrape subtitles · Crawl domains · Monitor system stats — all from one dashboard.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-latest-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-latest-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

---

## 📖 Table of Contents

- [Features](#-features)
- [Quick Start](#-quick-start)
- [Configuration](#%EF%B8%8F-configuration)
- [Running](#-running)
- [Project Structure](#-project-structure)
- [Usage Examples](#-usage-examples)
- [Troubleshooting](#-troubleshooting)
- [Security](#-security)
- [Tech Stack](#-tech-stack)
- [License](#-license)

---

## ✨ Features

### 📥 Universal Downloads
- **Any file type** — videos, PDFs, ZIPs, images, audio, executables, APKs
- **Video sites** — YouTube, Vimeo, TikTok, Twitter/X, Instagram, and 1000+ more via `yt-dlp`
- **Streams** — HLS (`.m3u8`), DASH (`.mpd`)
- **Torrents** — magnet links & `.torrent` files (with `libtorrent`)
- **Playlists** — full or ranged download with resume support
- **Quality control** — 360p → **4K (2160p)**, plus audio-only
- **Chrome impersonation + cookies** for Cloudflare/age-restricted content
- **Automatic folder reuse** — same playlist URL resumes from where it stopped

### 🎬 Video Tools
- **Random clip generator** — split into segments, pick random clips, merge
- **AI summarizer** — evenly spaced clips into a short summary
- **Frame extractor** — extract frames every N seconds → ZIP
- **Face swap** — photo → video via Colab integration

### 📤 Uploads & Storage
- **Telegram** — scan chats, download videos, send files (as documents)
- **Google Drive** — list, download, upload, delete files
- **Local file browser** — preview, download, delete, batch-send files

### 🛠 Utilities
- **OCR** — extract text from images/PDFs (Tesseract, 20+ languages)
- **Subtitles** — search & download from OpenSubtitles
- **Proxy fetcher** — scrape and test live HTTP/SOCKS proxies
- **Web crawler** — recursive domain discovery with SSE streaming

### 📊 Monitoring
- **Live stats bar** — CPU, RAM, Disk, Upload/Download speed
- **Thumbnail toggle** — pause thumbnail generation to save CPU
- **Real-time task panel** — Server-Sent Events (no polling)
- **Auto thumbnails** — extracted from the last 30 seconds of each video

---

## 🚀 Quick Start

### One-Command Install (Ubuntu/Debian)

```bash
git clone https://github.com/blackystrngr/ProjectCompress.git
cd ProjectCompress
chmod +x install.sh
sudo ./install.sh
```

The script installs and configures:
- ✅ ffmpeg (static build from BtbN)
- ✅ Node.js 20.x (for POT provider)
- ✅ Deno (JS runtime for yt-dlp challenges)
- ✅ yt-dlp + yt-dlp-ejs + curl_cffi
- ✅ bgutil-ytdlp-pot-provider (as systemd service)
- ✅ All Python dependencies with `--break-system-packages`

### Manual Install

```bash
# System packages
sudo apt update
sudo apt install -y python3-pip ffmpeg wget curl git nodejs npm

# Python packages
pip install --break-system-packages -r requirements.txt
pip install --break-system-packages yt-dlp yt-dlp-ejs curl_cffi bgutil-ytdlp-pot-provider

# Optional: torrent support
sudo apt install -y python3-libtorrent
```

---

## ⚙️ Configuration

### 1️⃣ YouTube Cookies — `cookies.txt`

For age-restricted or login-only videos:

1. Install the **"[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)"** extension
2. Log in to YouTube
3. Export cookies for `youtube.com`
4. Save as `cookies.txt` in the project root

**Better:** use the built-in **browser extension** to auto-upload cookies daily. Set `COOKIE_UPLOAD_TOKEN` env var and configure the extension with your VPS URL.

### 2️⃣ Google Drive — `token.json`

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

### 3️⃣ Telegram — `telegram_creds.json`

```json
{
  "api_id": 12345678,
  "api_hash": "your_api_hash_here"
}
```

Get credentials from [my.telegram.org/apps](https://my.telegram.org/apps).

### 4️⃣ Environment Variables — `.env` (optional)

```env
SECRET_KEY=change-me-to-a-long-random-string
PROXY_URL=http://user:pass@proxy.example.com:8080
DRIVE_FOLDER_ID=1abc...xyz
COOKIE_UPLOAD_TOKEN=generate-with-openssl-rand-hex-32
```

---

## 🏃 Running

```bash
python3 app.py
```

Open **http://YOUR_SERVER_IP:5000** in a browser.

### Run as systemd Service (recommended)

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

---

## 📁 Project Structure

```
ProjectCompress/
├── app.py                    # Flask entry + SSE + system stats + cookie upload
├── config.py                 # Paths, secrets, folder IDs
├── tasks.py                  # Task state + in-memory cache + SSE broadcaster
├── install.sh                # Full installation script
├── requirements.txt
├── settings.json             # Runtime settings (auto-generated)
├── cookies.txt               # YouTube cookies (auto-updated by extension)
├── token.json                # Google Drive OAuth (generated)
├── telegram_creds.json       # Telegram credentials (you provide)
│
├── features/
│   ├── __init__.py
│   ├── url_download.py       # Universal downloader (videos + files + torrents + playlists)
│   ├── video_extractor.py    # Scrape video links from webpages
│   ├── video_clipper.py      # Random clips · summarizer · frame extractor
│   ├── face_swap.py          # Face swap via Colab
│   ├── ocr.py                # Tesseract OCR
│   ├── subtitle_finder.py    # OpenSubtitles search
│   ├── proxy_fetcher.py      # Proxy scraping + testing
│   ├── telegram.py           # Telegram chat scanner + uploads
│   ├── google_drive.py       # Google Drive integration
│   ├── local_files.py        # File browser + streaming
│   ├── torrent_search.py     # Torrent search engines
│   ├── web_crawler.py        # Domain crawler with SSE
│   └── thumbnails.py         # Video thumbnail generator + pause toggle
│
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── _features_macro.html
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
│   └── js/app.js
│
├── downloads/                # Downloaded files
│   ├── .thumbnails/          # Cached thumbnails (auto)
│   ├── playlist_<id>_<q>/    # Stable playlist folders (resume-capable)
│   ├── clips_<task_id>/      # Temp clip fragments (auto-cleanup)
│   └── frames_<task_id>/     # Temp extracted frames (auto-cleanup)
│
├── tasks/                    # Task state JSON (auto-cleaned daily)
└── proxy_cache/              # Cached proxy results
```

---

## 🎯 Usage Examples

<details>
<summary><b>Download YouTube video at 1080p</b></summary>

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

Files are named by **original playlist position**: `17 - Video Title [id].mp4`

</details>

<details>
<summary><b>Download any file (PDF, ZIP, EXE, APK)</b></summary>

1. Paste the direct URL
2. Click **Download** — auto-detects and uses the right method

</details>

<details>
<summary><b>Extract frames from a video</b></summary>

1. Open **Video Clipper** → **Extract Frames**
2. Choose video, set interval (e.g. 5s), pick JPG/PNG
3. Click **Extract** → ZIP file created when done

</details>

<details>
<summary><b>Send files to Telegram as documents</b></summary>

1. Open **My Files**
2. Select files → **Send to Telegram**
3. Enter chat link → files uploaded (as documents, not video previews)

</details>

<details>
<summary><b>Pause thumbnail generation to save CPU</b></summary>

Tick the **Pause Thumbs** checkbox in the top stats bar. Existing thumbnails keep showing; no new ones generate until you untick.

</details>

---

## 🔧 Troubleshooting

| Issue | Fix |
|-------|-----|
| `Sign in to confirm you're not a bot` | Export fresh `cookies.txt`, or use the browser extension |
| `ffmpeg not found` | Run `install.sh` or `sudo apt install ffmpeg` |
| `POT provider not responding` | `sudo systemctl restart bgutil-provider` |
| Telegram `Event loop is closed` | Fixed — uses single persistent loop |
| Telegram uploads feel slow | Check `psutil.net_io_counters()`; system CPU may be saturated |
| Google Drive token expired | Delete `token.json` and re-authorize |
| Port 5000 already in use | `sudo fuser -k 5000/tcp` then restart |
| Thumbnails not showing | Ensure `ffprobe` is in `PATH`, check **Pause Thumbs** is unticked |
| `Requested format is not available` | Update yt-dlp: `pip install --break-system-packages -U yt-dlp` |
| High CPU after playlist finishes | Opening a folder triggers all thumbnails at once → use the **Pause Thumbs** toggle |
| Task history cluttering list | Auto-cleaned daily (terminal tasks > 1 day old) |

---

## 🔐 Security

⚠️ **This app has no authentication by default.** If exposing to the internet:

1. **Put it behind Nginx with HTTP Basic Auth** or **Authelia**
2. **Use HTTPS** (Let's Encrypt / Caddy)
3. **Restrict access** via firewall or WireGuard VPN
4. **Never commit** `cookies.txt`, `token.json`, or `telegram_creds.json`

### Recommended `.gitignore`

```gitignore
# Secrets
cookies.txt
cookies.txt.bak
token.json
credentials.json
telegram_creds.json
telegram_session*
.env
settings.json

# Runtime
tasks/
downloads/
proxy_cache/
venv/

# Python
__pycache__/
*.pyc
*.pyo
.pytest_cache/
```

---

## 🧰 Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Flask 3, Waitress |
| **Frontend** | Vanilla JS, SSE, CSS |
| **Media** | FFmpeg, yt-dlp, Deno |
| **Downloads** | requests, libtorrent |
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
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)
- [BtbN FFmpeg Builds](https://github.com/BtbN/FFmpeg-Builds)

---

<div align="center">

**⭐ Star this repo if you find it useful!**

*Use responsibly and respect copyright laws in your jurisdiction.*

</div>
