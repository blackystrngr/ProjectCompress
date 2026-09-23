📁 ProjectCompress
A self‑hosted media downloader, compressor, and organizer built with Flask. Download from any URL, extract video frames, swap faces, scrape subtitles, crawl domains, and more – all from a single web dashboard with live system stats.

✨ Features
📥 Downloads
Universal URL downloader – videos, PDFs, ZIPs, images, audio, executables, and any direct file link

Video/stream support via yt-dlp – YouTube, Vimeo, TikTok, m3u8/HLS, DASH, and 1000+ sites

Quality selection – 360p, 480p, 720p, 1080p, 1440p, 4K (2160p), or best available

Torrent support – magnet links and .torrent files (via libtorrent)

Torrent search – integrated 1337x / PirateBay / Zooqle search

YouTube bot bypass – uses --impersonate chrome, cookies, and the bgutil POT provider

🎬 Video Tools
Random clip generator – divide video into segments, pick random clips, merge into one

AI summarizer – evenly spaced clips into a short summary video

Frame extractor – extract one frame every N seconds into a ZIP file

Face swap – photo → video face replacement (via Colab integration)

📤 Uploads & Storage
Telegram – scan chats for videos, download selected, send files to chats

Google Drive – list, download, upload, delete files (uses OAuth)

Local file manager – browse, preview, download, delete, send to Telegram/Colab

🛠 Utilities
OCR – extract text from images and PDFs (Tesseract, 20+ languages)

Subtitle finder – search and download subtitles from OpenSubtitles

Proxy fetcher – scrape and test live HTTP/SOCKS proxies

Web crawler – recursive domain/URL discovery with live streaming results

📊 Monitoring
Live system stats bar – CPU, RAM, Disk usage + Upload/Download speed (updates every 2 seconds)

Real‑time task panel – Server‑Sent Events (SSE) push updates instantly (no polling)

Thumbnail previews – auto‑generated from the last 30 seconds of each video

🚀 Installation
Prerequisites
Debian 12 / Ubuntu 22.04+ (or similar)

Root access for installing system packages

~2 GB free disk space (for ffmpeg + Node.js + Python packages)

One‑command install
bash
git clone https://github.com/blackystrngr/ProjectCompress.git
cd ProjectCompress
chmod +x install.sh
sudo ./install.sh
The script installs and configures:

ffmpeg (static build from BtbN)

Node.js 20.x (for the POT provider)

Deno (JavaScript runtime for yt‑dlp challenges)

yt-dlp + yt-dlp-ejs + curl_cffi

bgutil-ytdlp-pot-provider (as a systemd service)

All Python dependencies from requirements.txt

Manual install
bash
# System packages
sudo apt update
sudo apt install -y python3-pip ffmpeg wget curl git nodejs npm

# Python packages
pip install --break-system-packages -r requirements.txt
pip install --break-system-packages yt-dlp yt-dlp-ejs curl_cffi bgutil-ytdlp-pot-provider

# Optional: torrent support
sudo apt install -y python3-libtorrent
⚙️ Configuration
1. YouTube cookies (cookies.txt)
For age‑restricted or login‑only videos:

Install the "Get cookies.txt LOCALLY" extension in Chrome/Firefox

Log in to YouTube

Export cookies for youtube.com → save as cookies.txt in the project root

2. Google Drive (token.json)
Only needed for Drive features:

bash
# Place credentials.json (OAuth client) in project root, then:
python3 -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file('credentials.json',
    ['https://www.googleapis.com/auth/drive'])
creds = flow.run_console()
open('token.json', 'w').write(creds.to_json())
"
3. Telegram (telegram_creds.json)
json
{
  "api_id": 12345678,
  "api_hash": "your_api_hash_here"
}
Get these from https://my.telegram.org/apps

4. Environment variables (optional)
Create a .env file:

env
SECRET_KEY=change-me-to-something-random
PROXY_URL=http://user:pass@proxy.example.com:8080
DRIVE_FOLDER_ID=1abc...xyz
🏃 Running
bash
python3 app.py
Open http://YOUR_SERVER_IP:5000 in a browser.

The app runs on port 5000 using Waitress (production WSGI server).

Run as a systemd service (recommended)
Create /etc/systemd/system/projectcompress.service:

ini
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
Enable and start:

bash
sudo systemctl daemon-reload
sudo systemctl enable projectcompress
sudo systemctl start projectcompress
sudo systemctl status projectcompress
📁 Project Structure
text
ProjectCompress/
├── app.py                    # Flask entry point + SSE + system stats
├── config.py                 # Paths, secrets, folder IDs
├── tasks.py                  # Task state + SSE broadcaster
├── install.sh                # Full installation script
├── requirements.txt
├── cookies.txt               # YouTube cookies (you provide)
├── token.json                # Google Drive OAuth (generated)
├── telegram_creds.json       # Telegram API credentials (you provide)
│
├── features/
│   ├── __init__.py
│   ├── url_download.py       # Universal downloader (videos + files + torrents)
│   ├── video_extractor.py    # Scrape video links from webpages
│   ├── video_clipper.py      # Random clips, summarizer, frame extractor
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
├── downloads/                # All downloaded/processed files
│   ├── .thumbnails/          # Cached video thumbnails (auto)
│   ├── clips_<task_id>/      # Temp folder for clip fragments (auto-cleanup)
│   └── frames_<task_id>/     # Temp folder for extracted frames (auto-cleanup)
│
├── tasks/                    # Task state JSON files (auto)
└── proxy_cache/              # Cached proxy results (auto)
🎯 Usage Examples
Download a YouTube video at 1080p
Paste the URL into URL Download

Select 1080p from the quality dropdown

Click Download – live progress, speed, and size shown

Final file appears in My Files

Download any file (PDF, ZIP, EXE, etc.)
Paste the direct URL

Click Download – it auto‑detects and uses the correct method

Done

Extract frames from a video
Open Video Clipper → Extract Frames

Choose video, set interval (e.g. 5 seconds), pick JPG/PNG

Click Extract – a ZIP file is created when done

Search and download a torrent
Enter query in Torrent Searcher

Click Download on any result → magnet link is queued

Send files to Telegram
Open My Files

Select files → click Send to Telegram

Enter chat link → files are uploaded

🔧 Troubleshooting
Issue	Fix
Sign in to confirm you're not a bot	Export fresh cookies.txt from a logged‑in YouTube session
ffmpeg not found	Run install.sh or sudo apt install ffmpeg
POT provider not responding	sudo systemctl restart bgutil-provider
Telegram event loop errors	Already fixed in latest telegram.py (single persistent loop)
Token expired (Google Drive)	Delete token.json and re‑authorise
Port 5000 already in use	sudo fuser -k 5000/tcp then restart
Thumbnails not showing	Ensure ffprobe is in PATH
🔐 Security Notes
⚠️ This app has no authentication by default. If exposing to the internet:

Put it behind Nginx with HTTP Basic Auth or Authelia

Use HTTPS (Let's Encrypt)

Restrict access via firewall/VPN

Never commit cookies.txt, token.json, or telegram_creds.json

Add a .gitignore for secrets and runtime folders

Example .gitignore:

gitignore
cookies.txt
token.json
credentials.json
telegram_creds.json
telegram_session*
tasks/
downloads/
proxy_cache/
__pycache__/
*.pyc
.env
venv/
📜 Dependencies
Python (from requirements.txt):

flask, waitress – web framework

requests, beautifulsoup4 – HTTP & HTML parsing

yt-dlp, yt-dlp-ejs, curl_cffi – video downloads + Chrome impersonation

google-api-python-client, google-auth-oauthlib – Drive

telethon – Telegram

psutil – system stats

Pillow, pytesseract, pdf2image – OCR

System:

ffmpeg, ffprobe – video processing

node (≥18), deno – POT provider + JS challenges

tesseract-ocr, poppler-utils – OCR

🤝 Contributing
Fork the repo

Create a feature branch

Commit your changes

Push and open a Pull Request

📄 License
MIT License – see LICENSE file for details.

🙏 Credits
yt-dlp – video downloader

bgutil-ytdlp-pot-provider – YouTube PO Token provider

Telethon – Telegram client

Tesseract OCR

BtbN FFmpeg Builds

