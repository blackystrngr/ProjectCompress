"""
url_download.py
===============
Universal downloader + smart video cracker with VDH-style network sniffing.

Handles:
  - Direct file URLs (.mp4, .pdf, .zip, etc.)
  - yt-dlp supported sites (YouTube, Vimeo, TikTok, etc.)
  - Playlists with resume
  - Torrents (magnet + .torrent)
  - Any media page via 15-strategy video cracking:
      1.  Direct URL detection
      2.  yt-dlp --get-url (native site extractors)
      3.  HTML tags (<video>, <source>, <object>, <embed>)
      4.  Meta tags (og:video, twitter:player, schema.org)
      5.  JSON-LD (contentUrl, embedUrl)
      6.  JSON script blocks (Next.js __NEXT_DATA__, Vidsonic)
      7.  Player configs (Plyr, Video.js, JW Player, DPlayer, etc.)
      8.  Inline JS patterns
      9.  Obfuscated payloads (Base64, hex, packed, ROT13)
      10. Raw URL regex anywhere
      11. Known streaming hosts (Lulustream, Doodstream, etc.)
      12. Iframes (recursive)
      13. API endpoint guessing (Vidsonic, Vixeo, Doodstream, etc.)
      14. ** Network sniffing with Playwright (Video DownloadHelper style) **
      15. Output verification via ffprobe
"""

import os
import re
import sys
import json
import uuid
import time
import base64
import asyncio
import threading
import logging
import hashlib
import requests
import subprocess
import shutil
import signal
from urllib.parse import urlparse, unquote, parse_qs, urljoin
from typing import List, Optional, Tuple

from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, PROXY_DICT

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# ---- Playwright is optional ----
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================
TORRENT_AVAILABLE = False
try:
    import libtorrent as lt
    TORRENT_AVAILABLE = True
except ImportError:
    logger.warning("libtorrent not installed. Torrent downloads disabled.")

COOKIES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'cookies.txt'
)

QUALITY_MAP = {
    'best':  'bv*+ba/b',
    '2160p': 'bv*[height<=2160]+ba/b[height<=2160]/b',
    '4k':    'bv*[height<=2160]+ba/b[height<=2160]/b',
    '1440p': 'bv*[height<=1440]+ba/b[height<=1440]/b',
    '1080p': 'bv*[height<=1080]+ba/b[height<=1080]/b',
    '720p':  'bv*[height<=720]+ba/b[height<=720]/b',
    '480p':  'bv*[height<=480]+ba/b[height<=480]/b',
    '360p':  'bv*[height<=360]+ba/b[height<=360]/b',
    'audio': 'ba/b',
}

CONCURRENT_FRAGMENTS = '1'

VIDEO_STREAM_EXTS = {'.m3u8', '.mpd', '.ts'}
MEDIA_EXTS = {
    '.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v', '.wmv', '.3gp',
    '.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma', '.opus',
}

VIDEO_SITES = [
    'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
    'twitter.com', 'x.com', 'facebook.com', 'instagram.com',
    'tiktok.com', 'twitch.tv', 'reddit.com', 'bitchute.com',
    'rumble.com', 'odysee.com', 'streamable.com',
]

SKIP_PATTERNS = [
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.bmp', '.ico',
    '.css', '.woff', '.woff2', '.ttf', '.eot', '.otf',
    '.js', '.mjs', '.jsx', '.map', '.ts',
    '.html', '.htm', '.php', '.aspx', '.jsp',
    'google-analytics', 'googletagmanager', 'gtag/js',
    'facebook.com/tr', 'plausible.', 'doubleclick',
    'analytics.', '/ads/', 'adservice', 'beacon',
    '/pixel', 'tracker', 'hotjar', 'sentry.io',
    'recaptcha', 'mixpanel', 'amplitude', 'segment.io',
    'cdn.jsdelivr', 'unpkg.com', 'cdnjs.cloudflare',
    '/js/', '/scripts/', '/assets/js/',
    'pa-', 'gtm.', 'fbevents',
]

# URLs that match these are preview thumbnails, not the main video
THUMBNAIL_PATTERNS = [
    '/thumbnails/', '/thumbs/', '/preview/', '/poster/',
    'thumb.jpg', 'preview.mp4', 'sprite', '/sprite/',
]

KNOWN_STREAM_HOSTS = [
    'lulustream', 'luluvdo', 'vidsonic', 'vixeo',
    'doodstream', 'dood.',
    'streamtape', 'streamsb', 'streamhide', 'mixdrop', 'filemoon',
    'voe.sx', 'voe-unblock', 'vidsrc', 'vidsrc.to', 'vidplay',
    'upstream', 'upstream.to', 'fastplay', 'vembed', 'vtube',
    'player4u', 'streamlare', 'streamwish', 'wishembed',
    's3.', '.amazonaws.com', 'cloudfront.net', 'akamaihd',
    'fastly.net', 'bunnycdn', 'b-cdn.net',
]

CHUNK_SIZE = 64 * 1024

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/120.0.0.0 Safari/537.36')

MIN_VIDEO_SIZE = 10_000  # 10 KB

# Content-Type markers that indicate a video stream
VIDEO_MIME_MARKERS = [
    'video/',
    'audio/',
    'application/vnd.apple.mpegurl',
    'application/x-mpegurl',
    'application/mpegurl',
    'application/dash+xml',
    'application/octet-stream',
]

SNIFF_TIMEOUT_SEC = 45
SNIFF_WAIT_AFTER_PLAY_MS = 6000


class DownloadCancelled(Exception):
    pass


# ============================================================
# PROCESS TRACKER
# ============================================================
_running_processes = {}
_processes_lock = threading.Lock()


def register_process(task_id, process):
    with _processes_lock:
        _running_processes[task_id] = process


def unregister_process(task_id):
    with _processes_lock:
        _running_processes.pop(task_id, None)


def kill_process(task_id):
    with _processes_lock:
        process = _running_processes.get(task_id)
    if process and process.poll() is None:
        try:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    process.kill()
            logger.info(f"Killed process for task {task_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to kill process for {task_id}: {e}")
    return False


# ============================================================
# URL CLASSIFICATION
# ============================================================
def _ext(url):
    try:
        name = os.path.basename(urlparse(url).path)
        return os.path.splitext(name)[1].lower()
    except Exception:
        return ''


def _filename_from_url(url):
    try:
        name = os.path.basename(urlparse(url).path)
        if name:
            return unquote(name)
    except Exception:
        pass
    return 'download'


def is_playlist_url(url):
    lower = url.lower()
    if 'youtube.com' not in lower and 'youtu.be' not in lower:
        return False
    if 'list=' in lower or '/playlist' in lower:
        return True
    if any(x in lower for x in ['/channel/', '/c/', '/user/', '/@']):
        return True
    return False


def needs_ytdlp(url):
    url_lower = url.lower()
    ext = _ext(url)
    if ext in VIDEO_STREAM_EXTS:
        return True
    if ext in MEDIA_EXTS:
        return True
    if any(site in url_lower for site in VIDEO_SITES):
        return True
    return False


def _playlist_folder_name(url, quality):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    if 'list' in qs and qs['list']:
        ident = qs['list'][0]
    elif '/@' in parsed.path:
        ident = parsed.path.split('/@')[1].split('/')[0]
    elif '/channel/' in parsed.path:
        ident = parsed.path.split('/channel/')[1].split('/')[0]
    elif '/c/' in parsed.path:
        ident = parsed.path.split('/c/')[1].split('/')[0]
    else:
        ident = hashlib.md5(url.encode()).hexdigest()[:12]

    ident = re.sub(r'[^\w\-]', '_', ident)[:50]
    return f"playlist_{ident}_{quality}"


# ============================================================
# HELPERS: validators
# ============================================================
def _is_video_url(url):
    if not url or not isinstance(url, str):
        return False
    low = url.lower()
    if any(skip in low for skip in SKIP_PATTERNS):
        return False
    try:
        path = urlparse(url).path.lower()
        ext = os.path.splitext(path)[1]
        return ext in MEDIA_EXTS or ext in VIDEO_STREAM_EXTS
    except Exception:
        return False


def _is_thumbnail(url):
    if not url:
        return False
    low = url.lower()
    return any(pat in low for pat in THUMBNAIL_PATTERNS)


def _is_probable_video(url, allow_thumbnail=False):
    if not url:
        return False
    low = url.lower()

    for skip in SKIP_PATTERNS:
        if skip in low:
            return False

    if not allow_thumbnail and _is_thumbnail(url):
        return False

    if _is_video_url(url):
        return True

    for h in KNOWN_STREAM_HOSTS:
        if h in low:
            path = urlparse(url).path.lower()
            for bad in ['.js', '.css', '.json', '.map', '.html', '.htm']:
                if path.endswith(bad):
                    return False
            if any(seg in low for seg in [
                '/video', '/media', '/stream', '/play/', '/hls/',
                '/dash/', '/master', '/index.m3u8', '/manifest',
                '.mp4', '.m3u8', '.mpd', '/s3/', '/get/',
                '/source', '/download', '/file/',
            ]):
                return True
            return False

    return False


def _normalize(url, base):
    if not url or not isinstance(url, str):
        return None
    url = url.strip().rstrip('.,;:!?)]}')
    if url.startswith('//'):
        url = 'https:' + url
    elif url.startswith('/'):
        url = urljoin(base, url)
    elif not url.startswith(('http://', 'https://')):
        return None
    if any(s in url.lower() for s in SKIP_PATTERNS):
        return None
    return url


def _dedupe(urls):
    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _verify_has_video_stream(file_path):
    ffprobe = shutil.which('ffprobe') or '/usr/local/bin/ffprobe'
    if not os.path.exists(ffprobe):
        return True
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_type',
             '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
            capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0 and r.stdout.strip() == 'video'
    except Exception:
        return False


# ============================================================
# HTML FETCHER
# ============================================================
def _fetch_page_html(url, timeout=20):
    session = requests.Session()
    if PROXY_DICT:
        session.proxies = PROXY_DICT
    session.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': url,
        'Connection': 'keep-alive',
    })

    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        domain, _, path_, _, _, name, value = parts[:7]
                        session.cookies.set(name, value,
                                            domain=domain.lstrip('.'),
                                            path=path_)
        except Exception:
            pass

    r = session.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text, r.url


# ============================================================
# CRACKER: #3 HTML tags
# ============================================================
def _crack_html_tags(html, base):
    if not BeautifulSoup:
        return []
    out = []
    soup = BeautifulSoup(html, 'html.parser')

    for tag in soup.find_all(['video', 'audio']):
        src = tag.get('src')
        if src:
            u = _normalize(src, base)
            if u:
                out.append(u)
        for source in tag.find_all('source'):
            src = source.get('src')
            if src:
                u = _normalize(src, base)
                if u:
                    out.append(u)

    for tag in soup.find_all(['object', 'embed']):
        data = tag.get('data') or tag.get('src')
        if data:
            u = _normalize(data, base)
            if u:
                out.append(u)

    for tag in soup.find_all('source'):
        src = tag.get('src')
        if src:
            u = _normalize(src, base)
            if u:
                out.append(u)

    return out


# ============================================================
# CRACKER: #4 Meta tags
# ============================================================
def _crack_meta(html, base):
    if not BeautifulSoup:
        return []
    out = []
    soup = BeautifulSoup(html, 'html.parser')
    META_KEYS = {
        'og:video', 'og:video:url', 'og:video:secure_url',
        'twitter:player', 'twitter:player:stream',
        'video:url', 'video:secure_url', 'media:content',
    }
    for meta in soup.find_all('meta'):
        prop = (meta.get('property') or meta.get('name') or '').lower()
        content = meta.get('content', '')
        if prop in META_KEYS and content:
            u = _normalize(content, base)
            if u:
                out.append(u)
    return out


# ============================================================
# CRACKER: #5 JSON-LD
# ============================================================
def _crack_jsonld(html, base):
    if not BeautifulSoup:
        return []
    out = []
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script', type='application/ld+json'):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except Exception:
            for m in re.finditer(r'"(?:contentUrl|embedUrl)"\s*:\s*"([^"]+)"',
                                  script.string):
                u = _normalize(m.group(1), base)
                if u:
                    out.append(u)
            continue

        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ('contentUrl', 'embedUrl', 'url') and isinstance(v, str):
                        u = _normalize(v, base)
                        if u:
                            out.append(u)
                    else:
                        walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(data)
    return out


# ============================================================
# CRACKER: #6 JSON script blocks
# ============================================================
def _crack_json_scripts(html, base):
    if not BeautifulSoup:
        return []
    out = []
    soup = BeautifulSoup(html, 'html.parser')

    candidate_scripts = []
    for script in soup.find_all('script'):
        stype = (script.get('type') or '').lower()
        sid = (script.get('id') or '').lower()
        if stype in ('application/json', 'application/ld+json'):
            candidate_scripts.append(script.string or '')
        elif sid in ('__next_data__', 'initial-state', '__init_data__',
                     '__nuxt_data__'):
            candidate_scripts.append(script.string or '')

    for text in candidate_scripts:
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            for u in _crack_raw_urls(text, base):
                out.append(u)
            continue

        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str) and _is_probable_video(v):
                        u = _normalize(v, base)
                        if u:
                            out.append(u)
                    else:
                        walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(data)
    return out


# ============================================================
# CRACKER: #7 Player configs
# ============================================================
def _crack_players(html, base):
    out = []
    patterns = [
        r'sources?\s*:\s*\[\s*\{[^}]*?src\s*:\s*[\'"]([^\'"]+)[\'"]',
        r'source\s*:\s*\{[^}]*?src\s*:\s*[\'"]([^\'"]+)[\'"]',
        r'file\s*:\s*[\'"]([^\'"]+\.(?:mp4|m3u8|mpd|webm|mov)[^\'"]*)[\'"]',
        r'"file"\s*:\s*"([^"]+)"',
        r'new\s+DPlayer\(\{[^}]*?url\s*:\s*[\'"]([^\'"]+)[\'"]',
        r'fluidPlayer\([^)]*,\s*\{[^}]*?video[\'"]?\s*:\s*[\'"]([^\'"]+)',
        r'new\s+Clappr\.Player\(\{[^}]*?source\s*:\s*[\'"]([^\'"]+)',
        r'hls\.loadSource\(\s*[\'"]([^\'"]+)',
        r'loadSource\(\s*[\'"]([^\'"]+)',
        r'(?:videoUrl|video_url|hlsUrl|hls_url|dashUrl|dash_url|'
        r'playlistUrl|playlist_url|streamUrl|stream_url|'
        r'mp4Url|mp4_url|fileUrl|file_url|mediaUrl|media_url)'
        r'\s*[:=]\s*[\'"]([^\'"]+)[\'"]',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE | re.DOTALL):
            raw = m.group(1).replace('\\/', '/').replace('\\"', '"')
            u = _normalize(raw, base)
            if u:
                out.append(u)
    return out


# ============================================================
# CRACKER: #8 Inline JS URLs
# ============================================================
def _crack_inline_js(html, base):
    out = []
    regex = re.compile(
        r'[\'"](https?://[^\s\'"<>\\]+?\.(?:mp4|m3u8|mpd|webm|mov|mkv|ts|m4v)(?:\?[^\s\'"<>\\]*)?)[\'"]',
        re.IGNORECASE
    )
    for m in regex.finditer(html):
        raw = m.group(1).replace('\\/', '/').replace('\\"', '"')
        u = _normalize(raw, base)
        if u:
            out.append(u)
    return out


# ============================================================
# CRACKER: #9 Obfuscated payloads
# ============================================================
def _crack_obfuscated(html, base):
    out = []

    for m in re.finditer(r'[\'"]([A-Za-z0-9+/=]{40,})[\'"]', html):
        try:
            decoded = base64.b64decode(m.group(1) + '===').decode(
                'utf-8', errors='ignore'
            )
            out.extend(_crack_raw_urls(decoded, base))
        except Exception:
            pass

    for m in re.finditer(r'[\'"]([0-9a-fA-F]{60,})[\'"]', html):
        try:
            raw = bytes.fromhex(m.group(1)).decode('utf-8', errors='ignore')
            out.extend(_crack_raw_urls(raw, base))
        except Exception:
            pass

    for m in re.finditer(
        r"eval\(function\(p,a,c,k,e,[dr]\)\{.*?\}\('(.+?)',(\d+),(\d+),'(.+?)'\.split",
        html, re.DOTALL
    ):
        try:
            p = m.group(1)
            k = m.group(4).split('|')

            def repl(tok):
                try:
                    idx = int(tok, 36)
                except ValueError:
                    return tok
                return k[idx] if 0 <= idx < len(k) and k[idx] else tok

            unpacked = re.sub(r'\b[0-9a-z]+\b', repl, p)
            out.extend(_crack_raw_urls(unpacked, base))
        except Exception:
            pass

    return out


# ============================================================
# CRACKER: #10 Raw URL regex
# ============================================================
def _crack_raw_urls(text, base):
    out = []
    regex = re.compile(
        r'https?://[^\s\'"<>\\]+?\.(?:mp4|m3u8|mpd|webm|mov|mkv|ts|m4v)(?:\?[^\s\'"<>\\]*)?',
        re.IGNORECASE
    )
    for m in regex.finditer(text):
        raw = m.group(0).replace('\\/', '/')
        u = _normalize(raw, base)
        if u:
            out.append(u)
    return out


# ============================================================
# CRACKER: #11 Known streaming hosts
# ============================================================
def _crack_known_hosts(html, base):
    out = []
    for host in KNOWN_STREAM_HOSTS:
        pattern = re.compile(
            rf'https?://[^\s\'"<>]*?{re.escape(host)}[^\s\'"<>]*',
            re.IGNORECASE
        )
        for m in pattern.finditer(html):
            raw = m.group(0).replace('\\/', '/').rstrip('.,;:!?)]}')
            u = _normalize(raw, base)
            if u and not any(s in u.lower() for s in SKIP_PATTERNS):
                path = urlparse(u).path.lower()
                if any(path.endswith(bad) for bad in
                       ['.js', '.css', '.json', '.map', '.html', '.htm']):
                    continue
                out.append(u)
    return out


# ============================================================
# CRACKER: #12 Iframes
# ============================================================
def _crack_iframes(html, base):
    if not BeautifulSoup:
        return []
    out = []
    soup = BeautifulSoup(html, 'html.parser')
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src') or iframe.get('data-src')
        if src:
            u = _normalize(src, base)
            if u:
                out.append(u)
    return out


# ============================================================
# CRACKER: #13 API guessing
# ============================================================
def _crack_apis(page_url):
    parsed = urlparse(page_url)
    host = parsed.netloc.lower()
    slug = parsed.path.strip('/').split('/')[-1]
    if not slug:
        return []

    paths = []
    if any(h in host for h in ['vidsonic', 'lulu', 'vixeo', 'vix']):
        paths += [
            f"/api/video/{slug}", f"/api/video?id={slug}",
            f"/api/get/{slug}", f"/api/media/{slug}",
            f"/api/player/{slug}", f"/api/source/{slug}",
            f"/api/stream/{slug}", f"/api/file/{slug}",
            f"/v1/video/{slug}", f"/v1/media/{slug}",
            f"/v1/source/{slug}", f"/v1/poster/{slug}",
            f"/v1/file/{slug}", f"/get_video/{slug}",
            f"/play/{slug}.json", f"/e/{slug}.json",
        ]
    if 'dood' in host:
        paths += [f"/pass_md5/{slug}", f"/api/source/{slug}"]
    if 'streamtape' in host:
        paths += [f"/get_video?id={slug}", f"/api/get_video?id={slug}"]
    if 'mixdrop' in host:
        paths += [f"/api/media/{slug}", f"/api/v1/media/{slug}"]

    paths += [f"/api/video/{slug}", f"/api/media/{slug}",
              f"/video/{slug}.json", f"/api/source/{slug}"]

    found = []
    session = requests.Session()
    if PROXY_DICT:
        session.proxies = PROXY_DICT
    session.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': 'application/json,*/*',
        'Referer': page_url,
    })

    for p in paths:
        url = f"{parsed.scheme}://{parsed.netloc}{p}"
        try:
            r = session.get(url, timeout=8, allow_redirects=True)
            if r.status_code != 200:
                continue
            text = r.text
            found.extend(_crack_raw_urls(text, page_url))
            found.extend(_crack_known_hosts(text, page_url))
            for m in re.finditer(r'"(?:url|file|src|source|link)"\s*:\s*"([^"]+)"',
                                  text):
                u = _normalize(m.group(1).replace('\\/', '/'), page_url)
                if u and _is_probable_video(u):
                    found.append(u)
            if found:
                logger.info(f"[Crack] API hit: {url}")
                break
        except Exception:
            continue
    return found


# ============================================================
# CRACKER: #14 Network Sniffing (Video DownloadHelper style)
# ============================================================
def _sniff_network(page_url, timeout=SNIFF_TIMEOUT_SEC):
    """
    Launch a headless Chromium via Playwright, navigate to the page,
    and intercept every network response that looks like a video stream.

    Mirrors how Video DownloadHelper detects streams.
    Returns a list of candidate URLs, ranked by likelihood of being the main video.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.info("[Sniff] Playwright not installed — skipping network sniff")
        return []

    logger.info("[Sniff] Launching headless Chromium...")
    captured = []  # list of dicts {url, mime, size, order}

    def run_sync():
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-blink-features=AutomationControlled',
                        '--autoplay-policy=no-user-gesture-required',
                    ]
                )
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={'width': 1280, 'height': 720},
                    # Accept all media
                    extra_http_headers={
                        'Accept-Language': 'en-US,en;q=0.9',
                    }
                )

                # Load cookies
                if os.path.exists(COOKIES_FILE):
                    try:
                        cookies = []
                        with open(COOKIES_FILE, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if not line or line.startswith('#'):
                                    continue
                                parts = line.split('\t')
                                if len(parts) < 7:
                                    continue
                                dom, _, pth, sec, exp, name, value = parts[:7]
                                try:
                                    exp_val = int(exp) if exp and exp != '0' else -1
                                except ValueError:
                                    exp_val = -1
                                cookies.append({
                                    'name': name,
                                    'value': value,
                                    'domain': dom.lstrip('.') if not dom.startswith('.') else dom,
                                    'path': pth or '/',
                                    'secure': sec.upper() == 'TRUE',
                                    'expires': exp_val,
                                })
                        if cookies:
                            context.add_cookies(cookies)
                            logger.info(f"[Sniff] Loaded {len(cookies)} cookies")
                    except Exception as e:
                        logger.debug(f"[Sniff] Cookie load failed: {e}")

                page = context.new_page()

                order_counter = [0]  # mutable counter

                def on_response(response):
                    try:
                        url = response.url
                        mime = (response.headers.get('content-type', '') or '').lower()
                        # Reject obvious junk
                        if any(skip in url.lower() for skip in SKIP_PATTERNS):
                            return
                        if not (any(m in mime for m in VIDEO_MIME_MARKERS)
                                or _is_video_url(url)):
                            return
                        # Reject thumbnails unless they're a m3u8 preview
                        # (Video DownloadHelper also shows these; keep them but rank low)
                        is_thumb = _is_thumbnail(url)
                        try:
                            cl = response.headers.get('content-length')
                            size = int(cl) if cl else 0
                        except Exception:
                            size = 0
                        order_counter[0] += 1
                        captured.append({
                            'url': url,
                            'mime': mime,
                            'size': size,
                            'thumb': is_thumb,
                            'order': order_counter[0],
                        })
                        tag = "📼" if not is_thumb else "🖼️"
                        logger.info(f"[Sniff] {tag} {url[:110]}")
                    except Exception:
                        pass

                page.on('response', on_response)

                logger.info(f"[Sniff] Navigating to {page_url}")
                try:
                    page.goto(page_url, timeout=30000,
                              wait_until='domcontentloaded')
                except Exception as e:
                    logger.warning(f"[Sniff] goto failed: {e}")

                # Give the page time to load + fetch initial assets
                page.wait_for_timeout(4000)

                # Try clicking play buttons
                play_selectors = [
                    'button[aria-label*="play" i]',
                    'button[title*="play" i]',
                    '.play-button',
                    '.vjs-big-play-button',
                    '.plyr__control--overlaid',
                    '[class*="play" i]:not(script)',
                    'button:has-text("Play")',
                    'video',
                ]
                for sel in play_selectors:
                    try:
                        page.click(sel, timeout=1500)
                        logger.info(f"[Sniff] Clicked: {sel}")
                        page.wait_for_timeout(1500)
                        break
                    except Exception:
                        pass

                # Try to force-play any <video> tags
                try:
                    page.evaluate("""
                        document.querySelectorAll('video').forEach(v => {
                            try { v.muted = true; v.play(); } catch (e) {}
                        });
                    """)
                except Exception:
                    pass

                # Wait for the player to spin up streams
                page.wait_for_timeout(SNIFF_WAIT_AFTER_PLAY_MS)

                # Scroll a bit to trigger lazy-loaded players
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

                browser.close()
        except Exception as e:
            logger.error(f"[Sniff] Fatal error: {e}")

    # Run Playwright's sync API in a dedicated thread so we don't fight
    # Flask's event loop or need asyncio.run here
    t = threading.Thread(target=run_sync, daemon=True)
    t.start()
    t.join(timeout=timeout + 10)

    if not captured:
        logger.info("[Sniff] No video streams detected")
        return []

    # ---- Rank captured URLs ----
    # Higher score = more likely the main video
    def score(item):
        s = 0
        u = item['url'].lower()
        if item['thumb']:
            s -= 100
        if u.endswith('.m3u8') or 'm3u8' in u:
            s += 50
        if 'master' in u or 'index' in u or 'manifest' in u:
            s += 20
        if u.endswith('.mp4'):
            s += 10
        if u.endswith('.mpd'):
            s += 40
        if item['size'] > 1_000_000:
            s += 30
        elif item['size'] > 100_000:
            s += 10
        if 'video' in item['mime']:
            s += 15
        # Later requests are usually the real stream (after teaser)
        s += min(item['order'], 10)
        # Prefer URLs NOT on the page host (real stream is usually on a CDN)
        try:
            if urlparse(item['url']).netloc != urlparse(page_url).netloc:
                s += 5
        except Exception:
            pass
        return s

    ranked = sorted(captured, key=score, reverse=True)
    urls = _dedupe([item['url'] for item in ranked])
    logger.info(f"[Sniff] Ranked {len(urls)} stream(s)")
    return urls


# ============================================================
# CRACKER: yt-dlp --get-url
# ============================================================
def _crack_ytdlp_geturl(url):
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        return []

    cmd = [
        ytdlp, '--get-url',
        '--no-warnings', '--ignore-errors',
        '--impersonate', 'chrome',
        '--extractor-args', 'generic:impersonate',
        url,
    ]
    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except Exception:
        return []

    urls = []
    for line in (r.stdout or '').splitlines():
        line = line.strip()
        if line.startswith(('http://', 'https://')):
            urls.append(line)
    return urls


# ============================================================
# MASTER CRACK FUNCTION
# ============================================================
def crack_video_urls(page_url, deep=True, save_debug_to=None):
    candidates = []
    seen = set()

    def add(urls, source="", allow_thumbnail=False):
        for u in urls or []:
            if not u or u in seen:
                continue
            if not allow_thumbnail and not _is_probable_video(u):
                low = u.lower()
                if any(skip in low for skip in SKIP_PATTERNS):
                    continue
                if _is_thumbnail(u):
                    continue
            seen.add(u)
            candidates.append(u)
            if source:
                logger.debug(f"[Crack] +{source}: {u[:100]}")

    logger.info(f"[Crack] Analyzing: {page_url}")

    if _is_video_url(page_url):
        logger.info("[Crack] Input is already a direct video URL")
        return [page_url]

    # Strategy 2: yt-dlp
    try:
        ytdlp_urls = _crack_ytdlp_geturl(page_url)
        if ytdlp_urls:
            logger.info(f"[Crack] yt-dlp: {len(ytdlp_urls)} URL(s)")
            add(ytdlp_urls, "ytdlp")
            if not deep:
                return _dedupe(candidates)
    except Exception as e:
        logger.debug(f"[Crack] yt-dlp failed: {e}")

    # Fetch HTML
    html, final_url = None, page_url
    try:
        html, final_url = _fetch_page_html(page_url)
        logger.info(f"[Crack] Fetched {len(html)} bytes from {final_url}")
    except Exception as e:
        logger.warning(f"[Crack] Fetch failed: {e}")

    if html:
        add(_crack_html_tags(html, final_url), "html-tags")
        add(_crack_meta(html, final_url), "meta")
        add(_crack_jsonld(html, final_url), "json-ld")
        add(_crack_json_scripts(html, final_url), "json-script")
        add(_crack_players(html, final_url), "player-config")
        add(_crack_inline_js(html, final_url), "inline-js")
        add(_crack_obfuscated(html, final_url), "obfuscated")
        add(_crack_raw_urls(html, final_url), "raw-scan")
        add(_crack_known_hosts(html, final_url), "known-hosts")

        iframe_urls = _crack_iframes(html, final_url)
        add(iframe_urls, "iframes")

        if deep and iframe_urls:
            for iframe in iframe_urls[:3]:
                if _is_video_url(iframe):
                    add([iframe], "iframe-direct")
                    continue
                try:
                    logger.info(f"[Crack] Following iframe: {iframe[:80]}")
                    nested = crack_video_urls(iframe, deep=True)
                    add(nested, "iframe-nested")
                except Exception as e:
                    logger.debug(f"[Crack] Iframe failed: {e}")

        if save_debug_to:
            try:
                os.makedirs(save_debug_to, exist_ok=True)
                safe = re.sub(r'[^\w\-]', '_', urlparse(final_url).netloc)[:40]
                path = os.path.join(save_debug_to, f"crack_{safe}.html")
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(html)
                logger.info(f"[Crack] Debug HTML: {path}")
            except Exception:
                pass

    # Strategy 13: API guessing
    try:
        add(_crack_apis(final_url), "api-guess")
    except Exception as e:
        logger.debug(f"[Crack] API guess failed: {e}")

    # ---- Strategy 14: Network sniffing (only if nothing useful found) ----
    if not candidates and deep:
        try:
            logger.info("[Crack] Falling back to network sniffing...")
            sniffed = _sniff_network(page_url)
            add(sniffed, "sniff", allow_thumbnail=True)
        except Exception as e:
            logger.warning(f"[Crack] Sniff failed: {e}")

    final = _dedupe(candidates)
    logger.info(f"[Crack] {len(final)} unique candidate(s)")
    return final


# ============================================================
# DIRECT FILE DOWNLOAD
# ============================================================
def download_direct_file(url, task_id):
    task = load_task(task_id)
    if not task:
        raise Exception("Task not found")

    filename = _filename_from_url(url) or f"{task_id}_file"
    final_name = _get_unique_filename(filename)
    output_path = os.path.join(UPLOAD_FOLDER, final_name)

    session = requests.Session()
    if PROXY_DICT:
        session.proxies = PROXY_DICT
    session.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
    })
    try:
        parsed = urlparse(url)
        session.headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
    except Exception:
        pass

    total = 0
    try:
        head = session.head(url, allow_redirects=True, timeout=30)
        total = int(head.headers.get('content-length', 0))
        cd = head.headers.get('content-disposition', '')
        if cd and 'filename=' in cd:
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', cd)
            if m:
                suggested = unquote(m.group(1)).strip()
                if suggested:
                    final_name = _get_unique_filename(suggested)
                    output_path = os.path.join(UPLOAD_FOLDER, final_name)
    except Exception as e:
        logger.warning(f"HEAD failed for {url}: {e}")

    task = load_task(task_id)
    task['status'] = 'downloading'
    task['download_progress'] = 0
    task['progress'] = 0
    task['total_size'] = total
    task['downloaded_size'] = 0
    task['download_speed'] = 0
    task['elapsed_time'] = 0
    save_task(task_id, task)

    start_time = time.time()
    last_update = 0
    downloaded = 0
    temp_path = output_path + '.part'

    try:
        resp = session.get(url, stream=True, timeout=(15, 120), allow_redirects=True)
        resp.raise_for_status()

        if total == 0:
            total = int(resp.headers.get('content-length', 0))
            task = load_task(task_id)
            if task:
                task['total_size'] = total
                save_task(task_id, task)

        with open(temp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                task = load_task(task_id)
                if task and task.get('cancelled', False):
                    raise DownloadCancelled("Cancelled by user")
                if not chunk:
                    continue

                f.write(chunk)
                downloaded += len(chunk)

                now = time.time()
                if now - last_update >= 0.5:
                    elapsed = now - start_time
                    speed_kbps = int((downloaded / elapsed) / 1024) if elapsed > 0 else 0
                    pct = int(100 * downloaded / total) if total > 0 else 0
                    task = load_task(task_id)
                    if task:
                        task['download_progress'] = pct
                        task['progress'] = pct
                        task['downloaded_size'] = downloaded
                        task['download_speed'] = speed_kbps
                        task['elapsed_time'] = int(elapsed)
                        save_task(task_id, task)
                    last_update = now

        # ---- Verify ----
        final_ext = os.path.splitext(output_path)[1].lower()
        if final_ext in MEDIA_EXTS or final_ext in VIDEO_STREAM_EXTS:
            final_size = os.path.getsize(temp_path)
            if final_size < MIN_VIDEO_SIZE:
                os.remove(temp_path)
                raise Exception(f"File too small ({final_size} bytes) — not a video.")
            if final_ext not in VIDEO_STREAM_EXTS:
                if not _verify_has_video_stream(temp_path):
                    os.remove(temp_path)
                    raise Exception("Downloaded file has no video stream.")

        os.rename(temp_path, output_path)
        final_size = os.path.getsize(output_path)
        elapsed = time.time() - start_time

        task = load_task(task_id)
        task['status'] = 'done'
        task['download_progress'] = 100
        task['progress'] = 100
        task['total_size'] = final_size
        task['downloaded_size'] = final_size
        task['download_speed'] = 0
        task['elapsed_time'] = int(elapsed)
        task['output_file'] = final_name
        save_task(task_id, task)
        logger.info(f"File downloaded: {final_name} ({final_size / 1024 / 1024:.1f} MB)")
    except DownloadCancelled:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


# ============================================================
# YT-DLP DOWNLOADER
# ============================================================
def download_with_ytdlp(url, task_id, quality='best'):
    task = load_task(task_id)
    if not task:
        raise Exception("Task not found")
    task['status'] = 'downloading'
    task['progress'] = 0
    task['total_size'] = 0
    task['downloaded_size'] = 0
    task['download_speed'] = 0
    save_task(task_id, task)

    os.environ['PATH'] = '/usr/local/bin:' + os.environ.get('PATH', '')

    ffmpeg_path = shutil.which('ffmpeg') or '/usr/local/bin/ffmpeg'
    if not os.path.exists(ffmpeg_path):
        raise Exception("ffmpeg not found")
    if not shutil.which('yt-dlp'):
        raise Exception("yt-dlp not installed")

    format_choice = QUALITY_MAP.get(quality, QUALITY_MAP['best'])
    output_template = os.path.join(UPLOAD_FOLDER, f"{task_id}_dl.%(ext)s")

    cmd = [
        'yt-dlp',
        '-o', output_template,
        '-f', format_choice,
        '--merge-output-format', 'mp4',
        '--no-part', '--no-mtime', '--no-warnings', '--ignore-errors',
        '--impersonate', 'chrome',
        '--extractor-args', 'youtube:player_client=mweb',
        '--extractor-args', 'generic:impersonate',
        '--ffmpeg-location', ffmpeg_path,
        '--newline', '--progress', '--no-colors',
        '--concurrent-fragments', CONCURRENT_FRAGMENTS,
        '--retries', '3', '--fragment-retries', '3',
        '--no-playlist',
    ]

    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]

    cmd.append(url)
    logger.info(f"yt-dlp command: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, preexec_fn=os.setsid,
    )
    register_process(task_id, process)

    output_lines = []
    total_size = 0
    last_update_time = 0

    RE_PERCENT = re.compile(r'\[download\]\s+(\d+(?:\.\d+)?)%')
    RE_TOTAL   = re.compile(r'of\s+~?\s*([\d.]+)\s*([KMGT]?i?B)')
    RE_SPEED   = re.compile(r'at\s+([\d.]+)\s*([KMGT]?i?B)/s')

    def to_bytes(value, unit):
        unit = unit.upper().replace('IB', 'B')
        factors = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
        return int(value * factors.get(unit, 1))

    try:
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if not line:
                continue
            output_lines.append(line)

            task = load_task(task_id)
            if task and task.get('cancelled', False):
                kill_process(task_id)
                raise DownloadCancelled("Cancelled by user")

            pct_match = RE_PERCENT.search(line)
            if pct_match:
                pct = float(pct_match.group(1))
                total_match = RE_TOTAL.search(line)
                if total_match:
                    total_size = to_bytes(float(total_match.group(1)),
                                           total_match.group(2))
                speed_match = RE_SPEED.search(line)
                speed_kbps = 0
                if speed_match:
                    speed_bytes = to_bytes(float(speed_match.group(1)),
                                            speed_match.group(2))
                    speed_kbps = int(speed_bytes / 1024)
                downloaded = int(total_size * pct / 100) if total_size > 0 else 0

                now = time.time()
                if now - last_update_time >= 1.0:
                    task = load_task(task_id)
                    if task:
                        task['progress'] = int(pct)
                        task['download_progress'] = int(pct)
                        task['total_size'] = total_size
                        task['downloaded_size'] = downloaded
                        task['download_speed'] = speed_kbps
                        save_task(task_id, task)
                    last_update_time = now

            if 'ERROR' in line:
                logger.error(f"yt-dlp: {line.strip()}")

        process.wait()
    finally:
        unregister_process(task_id)

    if process.returncode != 0:
        full_output = ''.join(output_lines)
        logger.error(f"yt-dlp failed (code {process.returncode}):\n{full_output[-3000:]}")
        raise Exception(f"yt-dlp failed: {full_output[-500:]}")

    files = [f for f in os.listdir(UPLOAD_FOLDER) if f.startswith(f"{task_id}_dl.")]
    if not files:
        raise Exception("No output file found.")

    mp4_files = [f for f in files if f.endswith('.mp4')]
    chosen = mp4_files[0] if mp4_files else files[0]
    src = os.path.join(UPLOAD_FOLDER, chosen)

    src_size = os.path.getsize(src)
    if src_size < MIN_VIDEO_SIZE:
        try:
            os.remove(src)
        except Exception:
            pass
        raise Exception(
            f"Downloaded file is not a video ({src_size} bytes). "
            f"Likely a script or error page."
        )

    if not _verify_has_video_stream(src):
        try:
            os.remove(src)
        except Exception:
            pass
        raise Exception("Downloaded file has no video stream.")

    base_name = os.path.basename(urlparse(url).path.rstrip('/')) or 'video'
    base_name = re.sub(r'[^\w\-]', '_', base_name)[:80]
    base_name = re.sub(r'\.(js|css|map|json)$', '', base_name, flags=re.IGNORECASE)
    if quality not in ('best', 'audio'):
        base_name = f"{base_name}_{quality}"
    final_name = _get_unique_filename(f"{base_name}.mp4")
    dst = os.path.join(UPLOAD_FOLDER, final_name)
    os.rename(src, dst)

    final_size = os.path.getsize(dst)

    task = load_task(task_id)
    task['status'] = 'done'
    task['progress'] = 100
    task['download_progress'] = 100
    task['total_size'] = final_size
    task['downloaded_size'] = final_size
    task['download_speed'] = 0
    task['output_file'] = final_name
    save_task(task_id, task)
    logger.info(f"Video downloaded: {final_name} ({final_size / 1024 / 1024:.1f} MB)")


# ============================================================
# PLAYLIST DOWNLOADER
# ============================================================
def download_playlist(url, task_id, quality='best', range_start=1, range_end=0):
    task = load_task(task_id)
    if not task:
        raise Exception("Task not found")

    task['status'] = 'downloading_playlist'
    task['progress'] = 0
    task['download_progress'] = 0
    task['current_item'] = 0
    task['total_items'] = 0
    task['current_title'] = ''
    task['current_video_size'] = 0
    task['current_video_downloaded'] = 0
    task['download_speed'] = 0
    save_task(task_id, task)

    os.environ['PATH'] = '/usr/local/bin:' + os.environ.get('PATH', '')

    ffmpeg_path = shutil.which('ffmpeg') or '/usr/local/bin/ffmpeg'
    if not os.path.exists(ffmpeg_path):
        raise Exception("ffmpeg not found")
    if not shutil.which('yt-dlp'):
        raise Exception("yt-dlp not installed")

    format_choice = QUALITY_MAP.get(quality, QUALITY_MAP['best'])

    folder_name = _playlist_folder_name(url, quality)
    playlist_dir = os.path.join(UPLOAD_FOLDER, folder_name)
    os.makedirs(playlist_dir, exist_ok=True)
    archive_file = os.path.join(playlist_dir, '.downloaded.txt')

    logger.info(f"Playlist folder: {folder_name} (resume via {archive_file})")

    output_template = os.path.join(
        playlist_dir,
        '%(playlist_index)s - %(title).80B [%(id)s].%(ext)s'
    )

    cmd = [
        'yt-dlp',
        '-o', output_template,
        '-f', format_choice,
        '--merge-output-format', 'mp4',
        '--no-part', '--no-mtime', '--no-warnings', '--ignore-errors',
        '--impersonate', 'chrome',
        '--extractor-args', 'youtube:player_client=mweb',
        '--extractor-args', 'generic:impersonate',
        '--ffmpeg-location', ffmpeg_path,
        '--newline', '--progress', '--no-colors',
        '--concurrent-fragments', CONCURRENT_FRAGMENTS,
        '--retries', '3', '--fragment-retries', '3',
        '--yes-playlist',
        '--download-archive', archive_file,
    ]

    if range_start > 1 or range_end > 0:
        items = f"{range_start}-{range_end}" if range_end > 0 else f"{range_start}-"
        cmd += ['--playlist-items', items]
        logger.info(f"Playlist range: {items}")

    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]

    cmd.append(url)
    logger.info(f"yt-dlp playlist command: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, preexec_fn=os.setsid,
    )
    register_process(task_id, process)

    output_lines = []
    last_update_time = 0

    RE_ITEM     = re.compile(r'\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)')
    RE_DEST     = re.compile(r'\[download\]\s+Destination:\s+(.+)$')
    RE_PCT      = re.compile(r'\[download\]\s+(\d+(?:\.\d+)?)%')
    RE_TOTAL    = re.compile(r'of\s+~?\s*([\d.]+)\s*([KMGT]?i?B)')
    RE_SPEED    = re.compile(r'at\s+([\d.]+)\s*([KMGT]?i?B)/s')
    RE_ALREADY  = re.compile(r'has already been recorded in the archive')

    current_item = 0
    total_items = 0
    current_title = ''
    current_video_size = 0
    current_video_downloaded = 0

    def to_bytes(value, unit):
        unit = unit.upper().replace('IB', 'B')
        factors = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
        return int(value * factors.get(unit, 1))

    def extract_title(path):
        base = os.path.basename(path)
        base = os.path.splitext(base)[0]
        base = re.sub(r'^\d+\s*-\s*', '', base)
        base = re.sub(r'\s*\[[^\]]+\]$', '', base)
        return base

    try:
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if not line:
                continue
            output_lines.append(line)

            task = load_task(task_id)
            if task and task.get('cancelled', False):
                kill_process(task_id)
                raise DownloadCancelled("Cancelled by user")

            m = RE_ITEM.search(line)
            if m:
                current_item = int(m.group(1))
                total_items = int(m.group(2))
                continue

            if RE_ALREADY.search(line):
                logger.info(f"Playlist {task_id}: skipping already-downloaded")
                continue

            m = RE_DEST.search(line)
            if m:
                current_title = extract_title(m.group(1).strip())
                current_video_size = 0
                current_video_downloaded = 0
                logger.info(f"Playlist {task_id}: starting '{current_title}'")

            pct_m = RE_PCT.search(line)
            if pct_m:
                pct = float(pct_m.group(1))
                total_m = RE_TOTAL.search(line)
                if total_m:
                    current_video_size = to_bytes(float(total_m.group(1)),
                                                   total_m.group(2))
                if current_video_size > 0:
                    current_video_downloaded = int(current_video_size * pct / 100)

                speed_m = RE_SPEED.search(line)
                speed_kbps = 0
                if speed_m:
                    speed_bytes = to_bytes(float(speed_m.group(1)),
                                            speed_m.group(2))
                    speed_kbps = int(speed_bytes / 1024)

                overall = (int(((current_item - 1) * 100 + pct) / total_items)
                           if total_items > 0 else int(pct))
                overall = min(99, overall)

                now = time.time()
                if now - last_update_time >= 1.0:
                    task = load_task(task_id)
                    if task:
                        task['progress'] = overall
                        task['download_progress'] = overall
                        task['download_speed'] = speed_kbps
                        task['current_item'] = current_item
                        task['total_items'] = total_items
                        task['current_title'] = current_title
                        task['current_video_size'] = current_video_size
                        task['current_video_downloaded'] = current_video_downloaded
                        save_task(task_id, task)
                    last_update_time = now

            if 'ERROR' in line:
                logger.error(f"yt-dlp: {line.strip()}")

        process.wait()
    finally:
        unregister_process(task_id)

    video_files = [f for f in os.listdir(playlist_dir) if f.endswith('.mp4')]

    if not video_files:
        full_output = ''.join(output_lines)
        if 'has already been recorded' in full_output:
            task = load_task(task_id)
            if task:
                task['status'] = 'done'
                task['progress'] = 100
                task['download_progress'] = 100
                task['output_file'] = folder_name + '/'
                task['error_msg'] = 'All videos already downloaded'
                save_task(task_id, task)
            return
        raise Exception("No videos were downloaded from the playlist.")

    total_size = sum(os.path.getsize(os.path.join(playlist_dir, f))
                     for f in video_files)

    task = load_task(task_id)
    task['status'] = 'done'
    task['progress'] = 100
    task['download_progress'] = 100
    task['total_size'] = total_size
    task['downloaded_size'] = total_size
    task['download_speed'] = 0
    task['output_file'] = folder_name + '/'
    task['total_items'] = len(video_files)
    save_task(task_id, task)

    logger.info(f"Playlist {task_id}: {len(video_files)} videos, "
                f"{total_size / 1024 / 1024:.1f} MB in {folder_name}/")


# ============================================================
# MAIN ROUTER
# ============================================================
def process_url_download(task_id, url, quality='best', range_start=1, range_end=0):
    logger.info(f"process_url_download: {task_id} → {url} "
                f"(quality={quality}, range={range_start}-{range_end or 'end'})")
    task = load_task(task_id)
    if not task:
        return

    try:
        if is_playlist_url(url):
            logger.info("→ Playlist")
            download_playlist(url, task_id, quality, range_start, range_end)
            return

        if needs_ytdlp(url):
            logger.info("→ yt-dlp (known site / stream)")
            download_with_ytdlp(url, task_id, quality)
            return

        ext = _ext(url)
        if ext and ext not in VIDEO_STREAM_EXTS:
            logger.info(f"→ Direct file (ext: {ext})")
            download_direct_file(url, task_id)
            return

        logger.info("→ Unknown URL — trying yt-dlp first")
        try:
            download_with_ytdlp(url, task_id, quality)
            return
        except Exception as ytdlp_err:
            err_text = str(ytdlp_err).lower()
            known_failures = [
                'unsupported url', 'no suitable', 'not a valid url',
                'unable to extract', 'no video formats',
                'no video formats found', 'this video is not available',
                'unable to download webpage', 'not a video',
            ]
            if not any(kw in err_text for kw in known_failures):
                raise
            logger.warning(f"yt-dlp failed. Trying cracker. Reason: "
                           f"{str(ytdlp_err)[:200]}")

        debug_dir = os.path.join(UPLOAD_FOLDER, 'crack_debug')
        candidates = crack_video_urls(url, deep=True, save_debug_to=debug_dir)

        task = load_task(task_id)
        if task:
            task['candidates'] = candidates[:20]
            save_task(task_id, task)

        if not candidates:
            raise Exception(
                "No video URL found on this page. The player may require "
                "JavaScript or login. Try uploading cookies via the browser "
                "extension, or check downloads/crack_debug/ for the HTML."
            )

        logger.info(f"Found {len(candidates)} candidate(s). Trying each...")

        last_err = None
        for i, candidate in enumerate(candidates, 1):
            if not _is_probable_video(candidate, allow_thumbnail=True):
                logger.info(f"→ Skipping non-video candidate {i}: {candidate[:100]}")
                continue
            try:
                logger.info(f"→ yt-dlp candidate {i}/{len(candidates)}: "
                            f"{candidate[:120]}")
                download_with_ytdlp(candidate, task_id, quality)
                return
            except DownloadCancelled:
                raise
            except Exception as e:
                logger.warning(f"Candidate {i} failed: {str(e)[:150]}")
                last_err = e
                continue

        for i, candidate in enumerate(candidates, 1):
            if not _is_probable_video(candidate, allow_thumbnail=True):
                continue
            try:
                logger.info(f"→ Direct candidate {i}: {candidate[:120]}")
                download_direct_file(candidate, task_id)
                return
            except DownloadCancelled:
                raise
            except Exception as e:
                logger.warning(f"Direct {i} failed: {str(e)[:150]}")
                last_err = e
                continue

        raise Exception(f"All {len(candidates)} candidates failed. "
                        f"Last: {last_err}")

    except DownloadCancelled:
        task = load_task(task_id)
        if task:
            task['status'] = 'cancelled'
            task['error_msg'] = 'Cancelled by user'
            save_task(task_id, task)
    except Exception as e:
        logger.exception(f"Download failed for {task_id}")
        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)


# ============================================================
# TORRENT
# ============================================================
def download_torrent(torrent_input, task_id, save_path):
    if not TORRENT_AVAILABLE:
        raise Exception("libtorrent not installed")
    ses = lt.session()
    ses.listen_on(6881, 6891)
    atp = lt.add_torrent_params()
    atp.save_path = save_path
    if torrent_input.startswith('magnet:'):
        atp.url = torrent_input
    else:
        atp.ti = lt.torrent_info(torrent_input)
    handle = ses.add_torrent(atp)
    task = load_task(task_id)
    if task:
        task['status'] = 'downloading'
        save_task(task_id, task)

    while not handle.has_metadata():
        if load_task(task_id).get('cancelled', False):
            ses.remove_torrent(handle)
            raise DownloadCancelled("Cancelled")
        time.sleep(1)

    torrent_name = handle.name()
    files = handle.get_torrent_info().files()
    total_size = sum(f.size for f in files)
    task = load_task(task_id)
    if task:
        task['total_size'] = total_size
        save_task(task_id, task)

    output_filename = (files.file_path(0) if files.num_files() == 1
                       else torrent_name + '.mp4')
    full_output_path = os.path.join(save_path, output_filename)

    while not handle.is_seed():
        if load_task(task_id).get('cancelled', False):
            ses.remove_torrent(handle)
            raise DownloadCancelled("Cancelled")
        status = handle.status()
        progress = int(status.progress * 100)
        downloaded = status.total_download
        speed = int(status.download_rate / 1024)
        task = load_task(task_id)
        if task:
            task['progress'] = progress
            task['downloaded_size'] = downloaded
            task['download_speed'] = speed
            task['download_progress'] = progress
            save_task(task_id, task)
        time.sleep(1)

    ses.remove_torrent(handle)
    if not os.path.exists(full_output_path):
        for root, _, files in os.walk(save_path):
            for f in files:
                if torrent_name in f:
                    full_output_path = os.path.join(root, f)
                    break
    final_name = _get_unique_filename(os.path.basename(full_output_path))
    final_path = os.path.join(UPLOAD_FOLDER, final_name)
    if full_output_path != final_path:
        os.rename(full_output_path, final_path)
    task = load_task(task_id)
    if task:
        task['status'] = 'done'
        task['output_file'] = final_name
        task['download_progress'] = 100
        task['download_speed'] = 0
        save_task(task_id, task)


def process_torrent_download(task_id, torrent_input):
    try:
        download_torrent(torrent_input, task_id, UPLOAD_FOLDER)
    except DownloadCancelled:
        task = load_task(task_id)
        if task:
            task['status'] = 'cancelled'
            save_task(task_id, task)
    except Exception as e:
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)


# ============================================================
# UTILITY
# ============================================================
def _get_unique_filename(filename):
    base, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(UPLOAD_FOLDER, new_name)):
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    return new_name


# ============================================================
# FLASK ROUTES
# ============================================================
def register_routes(app):
    @app.route('/start', methods=['POST'])
    def start():
        url = request.form.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL required'}), 400

        quality = request.form.get('quality', 'best').strip().lower()
        if quality not in QUALITY_MAP:
            quality = 'best'

        try:
            range_start = int(request.form.get('range_start', 1) or 1)
        except ValueError:
            range_start = 1
        try:
            range_end = int(request.form.get('range_end', 0) or 0)
        except ValueError:
            range_end = 0
        if range_start < 1:
            range_start = 1

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'download_progress': 0,
            'progress': 0,
            'total_size': 0,
            'downloaded_size': 0,
            'download_speed': 0,
            'created_at': time.time(),
            'cancelled': False,
            'url': url,
            'quality': quality,
            'range_start': range_start,
            'range_end': range_end,
        }
        save_task(task_id, task_data)

        if url.startswith('magnet:') or (url.endswith('.torrent')
                                         and url.startswith(('http://', 'https://'))):
            if not TORRENT_AVAILABLE:
                task_data['status'] = 'error'
                task_data['error_msg'] = 'libtorrent not installed'
                save_task(task_id, task_data)
                return jsonify({'task_id': task_id, 'error': 'libtorrent missing'}), 500

            def fetch_torrent():
                if url.startswith('magnet:'):
                    process_torrent_download(task_id, url)
                else:
                    try:
                        resp = requests.get(url, timeout=30)
                        resp.raise_for_status()
                        temp_torrent = os.path.join(UPLOAD_FOLDER,
                                                     f"{task_id}_temp.torrent")
                        with open(temp_torrent, 'wb') as f:
                            f.write(resp.content)
                        process_torrent_download(task_id, temp_torrent)
                        os.remove(temp_torrent)
                    except Exception as e:
                        task = load_task(task_id)
                        if task:
                            task['status'] = 'error'
                            task['error_msg'] = str(e)
                            save_task(task_id, task)
            threading.Thread(target=fetch_torrent, daemon=True).start()
        else:
            def run():
                process_url_download(task_id, url, quality, range_start, range_end)
            threading.Thread(target=run, daemon=True).start()

        return jsonify({'task_id': task_id})

    @app.route('/crack/url', methods=['POST'])
    def crack_url():
        page_url = request.form.get('url', '').strip()
        if not page_url:
            return jsonify({'error': 'URL required'}), 400

        debug_dir = os.path.join(UPLOAD_FOLDER, 'crack_debug')
        try:
            candidates = crack_video_urls(page_url, deep=True,
                                           save_debug_to=debug_dir)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        return jsonify({
            'page_url': page_url,
            'count': len(candidates),
            'candidates': candidates,
        })

    @app.route('/sniff/url', methods=['POST'])
    def sniff_url():
        """Network-sniffing only endpoint (VDH-style)."""
        page_url = request.form.get('url', '').strip()
        if not page_url:
            return jsonify({'error': 'URL required'}), 400
        if not PLAYWRIGHT_AVAILABLE:
            return jsonify({
                'error': 'Playwright not installed. Run: '
                         'pip install playwright && playwright install chromium'
            }), 500
        urls = _sniff_network(page_url)
        return jsonify({
            'page_url': page_url,
            'count': len(urls),
            'streams': urls,
        })

    @app.route('/start_upload_torrent', methods=['POST'])
    def start_upload_torrent():
        if not TORRENT_AVAILABLE:
            return jsonify({'error': 'libtorrent not installed'}), 500
        if 'torrent_file' not in request.files:
            return jsonify({'error': 'No file'}), 400
        file = request.files['torrent_file']
        if file.filename == '' or not file.filename.endswith('.torrent'):
            return jsonify({'error': 'Invalid .torrent file'}), 400
        task_id = str(uuid.uuid4())
        temp_path = os.path.join(UPLOAD_FOLDER, f"{task_id}_uploaded.torrent")
        file.save(temp_path)
        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'created_at': time.time(),
            'cancelled': False,
        }
        save_task(task_id, task_data)
        threading.Thread(target=process_torrent_download,
                         args=(task_id, temp_path), daemon=True).start()
        return jsonify({'task_id': task_id})


# ============================================================
# STANDALONE CLI
# ============================================================
def _cli_main():
    if len(sys.argv) < 2:
        print("Usage: python url_download.py <url> [--sniff]")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )

    url = sys.argv[1]
    use_sniff = '--sniff' in sys.argv

    if use_sniff:
        urls = _sniff_network(url)
    else:
        urls = crack_video_urls(url, deep=True,
                                 save_debug_to='/tmp/crack_debug')

    print()
    print("=" * 70)
    print(f"  Found {len(urls)} candidate(s)")
    print("=" * 70)
    for i, u in enumerate(urls, 1):
        print(f"  {i}. {u}")
    print()


if __name__ == '__main__' and __name__ != 'features.url_download':
    _cli_main()
