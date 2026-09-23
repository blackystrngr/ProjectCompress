<div align="center">

# 🎬 ProjectCompress

**A self-hosted media downloader, compressor, and organizer built with Flask.**

Download from any URL · Extract frames · Swap faces · Scrape subtitles · Crawl domains · Monitor system stats — all from one dashboard.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-latest-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-latest-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

</div>

---

## 📖 Table of Contents

- [Features](#-features)
- [Screenshots](#-screenshots)
- [Quick Start](#-quick-start)
- [Configuration](#%EF%B8%8F-configuration)
- [Running the App](#-running-the-app)
- [Project Structure](#-project-structure)
- [Usage Examples](#-usage-examples)
- [Troubleshooting](#-troubleshooting)
- [Security](#-security)
- [Tech Stack](#-tech-stack)
- [Contributing](#-contributing)
- [License](#-license)

---

## ✨ Features

### 📥 Universal Downloads
| Feature | Description |
|---------|-------------|
| **Any file type** | Download videos, PDFs, ZIPs, images, audio, executables, APKs, and any direct URL |
| **Video & streams** | YouTube, Vimeo, TikTok, m3u8/HLS, DASH via `yt-dlp` |
| **Quality control** | 360p → **4K (2160p)** + audio-only |
| **Torrents** | Magnet links & `.torrent` files via `libtorrent` |
| **Torrent search** | Built-in 1337x / PirateBay / Zooqle search |
| **Bot bypass** | Chrome impersonation + cookies + [bgutil POT provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) |

### 🎬 Video Tools
- **Random clip generator** – split video, pick random clips, merge into one
- **AI summarizer** – evenly spaced clips into a short summary
- **Frame extractor** – extract one frame every N seconds into a ZIP
- **Face swap** – photo → video face replacement (via Colab)

### 📤 Uploads & Storage
- **Telegram** – scan chats, download videos, send files
- **Google Drive** – list, download, upload, delete
- **Local file manager** – browse, preview, download, delete files

### 🛠 Utilities
- **OCR** – extract text from images/PDFs (Tesseract, 20+ languages)
- **Subtitles** – search & download from OpenSubtitles
- **Proxy fetcher** – scrape and test HTTP/SOCKS proxies
- **Web crawler** – recursive domain/URL discovery with live streaming

### 📊 Monitoring
- **Live stats bar** – CPU, RAM, Disk, Upload/Download speed (2s refresh)
- **Real-time tasks** – Server-Sent Events (no polling)
- **Auto thumbnails** – generated from the last 30s of each video

---

## 📸 Screenshots

> Add your screenshots here — recommended: 1200×700 PNG files in `docs/`.

| Dashboard | Video Clipper |
|-----------|---------------|
| ![Dashboard](docs/dashboard.png) | ![Clipper](docs/clipper.png) |

---

## 🚀 Quick Start

### One-command install (Ubuntu / Debian)

```bash
git clone https://github.com/blackystrngr/ProjectCompress.git
cd ProjectCompress
chmod +x install.sh
sudo ./install.sh
```

The installer sets up:
- ✅ ffmpeg (static build)
- ✅ Node.js 20.x
- ✅ Deno (for yt-dlp challenges)
- ✅ yt-dlp + yt-dlp-ejs + curl_cffi
- ✅ bgutil POT provider (as systemd service)
- ✅ All Python dependencies

### Manual install

```bash
# System dependencies
sudo apt update
sudo apt install -y python3-pip ffmpeg wget curl git nodejs npm

# Python dependencies
pip install --break-system-packages -r requirements.txt
pip install --break-system-packages yt-dlp yt-dlp-ejs curl_cffi bgutil-ytdlp-pot-provider

# Optional: torrent support
sudo apt install -y python3-libtorrent
```

---

## ⚙️ Configuration

### 1️⃣ YouTube cookies — `cookies.txt`

For age-restricted or login-only videos:

1. Install the **[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** extension
2. Log in to YouTube
3. Export cookies for `youtube.com`
4. Save as `cookies.txt` in the project root

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

Get credentials from [my.telegram.org](https://my.telegram.org/apps).

### 4️⃣ Environment variables — `.env` (optional)

```env
SECRET_KEY=change-me-to-a-long-random-string
PROXY_URL=http://user:pass@proxy.example.com:8080
DRIVE_FOLDER_ID=1abc...xyz
```

---

## 🏃 Running the App

```bash
python3 app.py
```

Open **http://YOUR_SERVER_IP:5000** in a browser.

### Run as a systemd service (recommended)

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
├── app.py                    # Flask entry · SSE · system stats
├── config.py                 # Paths, secrets, folder IDs
├── tasks.py                  # Task state + SSE broadcaster
├── install.sh                # Full installation script
├── requirements.txt
├── cookies.txt               # YouTube cookies (you provide)
├── token.json                # Google Drive OAuth (generated)
├── telegram_creds.json       # Telegram credentials (you provide)
│
├── features/
│   ├── url_download.py       # Universal downloader (videos + files + torrents)
│   ├── video_extractor.py    # Scrape video links from webpages
│   ├── video_clipper.py      # Random clips · summarizer · frame extractor
│   ├── face_swap.py          # Face swap via Colab
│   ├── ocr.py                # Tesseract OCR
│   ├── subtitle_finder.py    # OpenSubtitles search
│   ├── proxy_fetcher.py      # Proxy scraping + testing
│   ├── telegram.py           # Telegram chat scanner
│   ├── google_drive.py       # Google Drive integration
│   ├── local_files.py        # File browser
│   ├── torrent_search.py     # Torrent search engines
│   ├── web_crawler.py        # Domain crawler
│   └── thumbnails.py         # Video thumbnail generator
│
├── templates/                # Jinja2 templates
│   ├── base.html
│   ├── index.html
│   ├── _features_macro.html
│   └── features/
│
├── static/
│   ├── css/style.css
│   └── js/app.js
│
├── downloads/                # All downloaded/processed files
│   ├── .thumbnails/          # Cached thumbnails (auto)
│   ├── clips_<task_id>/      # Temp clip fragments (auto-cleanup)
│   └── frames_<task_id>/     # Temp extracted frames (auto-cleanup)
│
├── tasks/                    # Task state JSON (auto)
└── proxy_cache/              # Cached proxy results (auto)
```

---

## 🎯 Usage Examples

<details>
<summary><b>Download a YouTube video at 1080p</b></summary>

1. Paste URL into **URL Download**
2. Select **1080p** from the quality dropdown
3. Click **Download** – live progress, speed, and size shown
4. Final file appears in **My Files**

</details>

<details>
<summary><b>Download any file (PDF, ZIP, EXE)</b></summary>

1. Paste the direct file URL
2. Click **Download** – auto-detects and uses the right method
3. Done

</details>

<details>
<summary><b>Extract frames from a video</b></summary>

1. Open **Video Clipper** → **Extract Frames**
2. Choose video, set interval (e.g. 5s), pick JPG/PNG
3. Click **Extract** – a ZIP is created when done

</details>

<details>
<summary><b>Search and download a torrent</b></summary>

1. Enter query in **Torrent Searcher**
2. Click **Download** on any result → magnet link is queued

</details>

<details>
<summary><b>Send files to Telegram</b></summary>

1. Open **My Files**
2. Select files → click **Send to Telegram**
3. Enter chat link → files uploaded

</details>

---

## 🔧 Troubleshooting

| Issue | Fix |
|-------|-----|
| `Sign in to confirm you're not a bot` | Export fresh `cookies.txt` from a logged-in YouTube session |
| `ffmpeg not found` | Run `install.sh` or `sudo apt install ffmpeg` |
| `POT provider not responding` | `sudo systemctl restart bgutil-provider` |
| Telegram `Event loop is closed` | Fixed in latest `telegram.py` (single persistent loop) |
| Google Drive token expired | Delete `token.json` and re-authorize |
| Port 5000 already in use | `sudo fuser -k 5000/tcp` then restart |
| Thumbnails not showing | Ensure `ffprobe` is in `PATH` |
| `Requested format is not available` | Update yt-dlp: `pip install -U yt-dlp` |

---

## 🔐 Security

⚠️ **This app has no authentication by default.** If exposing to the internet:

1. **Put it behind Nginx with HTTP Basic Auth** or **Authelia**
2. **Use HTTPS** (Let's Encrypt / Caddy)
3. **Restrict access** via firewall or VPN
4. **Never commit** `cookies.txt`, `token.json`, or `telegram_creds.json`

### Recommended `.gitignore`

```gitignore
# Secrets
cookies.txt
token.json
credentials.json
telegram_creds.json
telegram_session*
.env

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
| **Backend** | Flask, Waitress |
| **Frontend** | Vanilla JS, SSE, CSS |
| **Media** | FFmpeg, yt-dlp, Deno |
| **Downloads** | requests, libtorrent |
| **Storage** | Google Drive API, Telegram (Telethon) |
| **OCR** | Tesseract, pdf2image |
| **Monitoring** | psutil |
| **POT Provider** | bgutil-ytdlp-pot-provider (Node.js) |

---

## 🤝 Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feature/my-feature`
3. Commit: `git commit -m 'Add my feature'`
4. Push: `git push origin feature/my-feature`
5. Open a Pull Request

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

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

Made with ❤️ for personal media management.

*Use responsibly and respect copyright laws in your jurisdiction.*

</div>
