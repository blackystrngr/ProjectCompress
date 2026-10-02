"""
url_download.py
===============
Universal downloader with noise-free network sniffing + user media selection.

Strategies:
  1. Direct file URL  → download immediately
  2. Known video site → yt-dlp
  3. Playlist         → yt-dlp with resume
  4. Torrent          → libtorrent
  5. Unknown page     → yt-dlp --get-url
                        ├─ Success → download the URL
                        └─ Failure → network sniff (VDH-style, leak-proof,
                                     ad-filtered, duration-ranked)
                                    → return candidates
                                    → wait for user choice
                                    → download chosen media
"""

import os
import re
import sys
import json
import uuid
import time
import threading
import logging
import hashlib
import requests
import subprocess
import shutil
import signal
from urllib.parse import urlparse, unquote, parse_qs, urljoin
from typing import List, Optional

from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, PROXY_DICT

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

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
    '.riv', '.webmanifest', '.json',
    'google-analytics', 'googletagmanager', 'gtag/js',
    'facebook.com/tr', 'plausible.', 'doubleclick',
    'analytics.', '/ads/', 'adservice', 'beacon',
    '/pixel', 'tracker', 'hotjar', 'sentry.io',
    'recaptcha', 'mixpanel', 'amplitude', 'segment.io',
    'cdn.jsdelivr', 'unpkg.com', 'cdnjs.cloudflare',
    '/js/', '/scripts/', '/assets/js/',
    'pa-', 'gtm.', 'fbevents',
]

# Ad CDNs and known junk hosts — reject these URLs at capture time
AD_DOMAINS = [
    'bxcdn.net',
    'doubleclick.net',
    'googlesyndication',
    'adnxs.com',
    'popads.net',
    'exoclick.com',
    'adservice.google',
    'chapturist.com',
    'adcolony.com',
    'applovin.com',
    'unityads.unity3d.com',
    'vungle.com',
    'chartboost.com',
]

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
    'fastly.net', 'bunnycdn', 'b-cdn.net', 'doppiocdn',
]

CHUNK_SIZE = 64 * 1024

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/120.0.0.0 Safari/537.36')

MIN_VIDEO_SIZE = 10_000

VIDEO_MIME_MARKERS = [
    'video/',
    'audio/',
    'application/vnd.apple.mpegurl',
    'application/x-mpegurl',
    'application/mpegurl',
    'application/dash+xml',
    'application/octet-stream',
]

# Sniff timings
SNIFF_TIMEOUT_SEC = 75                     # total budget
SNIFF_WAIT_AFTER_PLAY_MS = 15000           # wait up to 15s after clicking play (ads run first)
SNIFF_WAIT_BEFORE_PLAY_MS = 5000           # wait after page load, before play click


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
# GLOBAL SNIFF LOCK + ORPHAN CLEANUP
# ============================================================
_sniff_lock = threading.Lock()


def _cleanup_playwright_processes(verbose=False):
    """Kill any orphan Chromium/Playwright processes."""
    try:
        import psutil
    except ImportError:
        return 0

    killed = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            name = (proc.info.get('name') or '').lower()
            cmdline_list = proc.info.get('cmdline') or []
            cmdline = ' '.join(cmdline_list).lower()

            is_playwright_browser = (
                'ms-playwright' in cmdline
                or ('chrome' in name and 'headless' in cmdline)
                or ('chrome' in name and 'playwright' in cmdline)
                or ('chromium' in name and 'headless' in cmdline)
            )
            if is_playwright_browser:
                try:
                    proc.terminate()
                    killed.append(proc.info['pid'])
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if killed:
        logger.warning(f"[Sniff] Cleaned up {len(killed)} orphan browser process(es)")
        if verbose:
            logger.info(f"[Sniff] Killed PIDs: {killed}")

    try:
        import subprocess as _sp
        _sp.run(['pkill', '-f', 'ms-playwright.*chromium'],
                capture_output=True, timeout=5)
        _sp.run(['pkill', '-f', 'playwright.*driver'],
                capture_output=True, timeout=5)
    except Exception:
        pass

    return len(killed)


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
# VALIDATORS
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


def _is_ad_url(url):
    if not url:
        return False
    low = url.lower()
    return any(ad in low for ad in AD_DOMAINS)


def _is_probable_video(url, allow_thumbnail=False):
    if not url:
        return False
    low = url.lower()
    for skip in SKIP_PATTERNS:
        if skip in low:
            return False
    if _is_ad_url(url):
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


def _fmt_bytes(n):
    try:
        n = int(n)
    except Exception:
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n/1024:.1f} KB"
    if n < 1024**3:
        return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"


# ============================================================
# YT-DLP: --get-url
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
# HLS DURATION PROBE (via yt-dlp --dump-json)
# ============================================================
def _probe_hls_duration(url):
    """
    Ask yt-dlp for metadata on a stream URL.
    Returns duration in seconds (0 if unknown).
    Used to distinguish ads (15-45s) from real videos (minutes+).
    """
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        return 0
    cmd = [
        ytdlp, '--dump-json', '--no-warnings', '--ignore-errors',
        '--impersonate', 'chrome',
        '--extractor-args', 'generic:impersonate',
    ]
    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            return 0
        for line in (r.stdout or '').splitlines():
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                data = json.loads(line)
                return int(data.get('duration') or 0)
            except Exception:
                continue
    except Exception:
        return 0
    return 0


# ============================================================
# NETWORK SNIFFER — clean shutdown + duration-based ranking
# ============================================================
def _sniff_network(page_url, timeout=SNIFF_TIMEOUT_SEC):
    """
    Launch headless Chromium, navigate, intercept responses.
    - Filters junk (ads, fragments, low-latency rolls, pings)
    - Captures across the entire ad→video transition
    - Skips ads when possible
    - Shuts down the browser gracefully (no asyncio noise)
    - Ranks by duration so the real video wins over the ad
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.info("[Sniff] Playwright not installed — skipping")
        return []

    if not _sniff_lock.acquire(timeout=60):
        logger.warning("[Sniff] Another sniff is already running — skipping")
        return []

    captured = []
    stop_event = threading.Event()

    def run_sync():
        pw = None
        browser = None
        context = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--autoplay-policy=no-user-gesture-required',
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-background-timer-throttling',
                    '--disable-renderer-backgrounding',
                    '--disable-features=TranslateUI',
                    '--disable-features=site-per-process',
                    '--js-flags=--max-old-space-size=256',
                    '--window-size=1280,720',
                ]
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 720},
                extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
            )

            # ---- Cookies ----
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
                                'domain': dom,
                                'path': pth or '/',
                                'secure': sec.upper() == 'TRUE',
                                'expires': exp_val,
                            })
                    if cookies:
                        context.add_cookies(cookies)
                except Exception as e:
                    logger.debug(f"[Sniff] Cookie load failed: {e}")

            page = context.new_page()
            order_counter = [0]

            def is_junk_url(url):
                low = url.lower()
                path = urlparse(url).path.lower()
                for skip in SKIP_PATTERNS:
                    if skip in low:
                        return True
                for ad in AD_DOMAINS:
                    if ad in low:
                        return True
                if '_hls_msn' in low or '_hls_part' in low:
                    return True
                if re.search(r'_part\d+\.mp4', path):
                    return True
                if 'init_' in path and path.endswith('.mp4'):
                    return True
                if 'ping.m3u8' in low:
                    return True
                for bad in ['.riv', '.svg', '.json', '.webmanifest',
                            '.woff2', '.woff', '.ttf', '.eot', '.otf']:
                    if path.endswith(bad):
                        return True
                return False

            def on_response(response):
                try:
                    url = response.url
                    mime = (response.headers.get('content-type', '') or '').lower()
                    if not (any(m in mime for m in VIDEO_MIME_MARKERS)
                            or _is_video_url(url)):
                        return
                    if is_junk_url(url):
                        return
                    try:
                        cl = response.headers.get('content-length')
                        size = int(cl) if cl else 0
                    except Exception:
                        size = 0
                    if 0 < size < 100:
                        return
                    order_counter[0] += 1
                    captured.append({
                        'url': url,
                        'mime': mime,
                        'size': size,
                        'is_thumbnail': _is_thumbnail(url),
                        'order': order_counter[0],
                    })
                    logger.info(f"[Sniff] 📼 {url[:110]}")
                except Exception:
                    pass

            page.on('response', on_response)

            logger.info(f"[Sniff] Navigating to {page_url}")
            try:
                page.goto(page_url, timeout=30000, wait_until='domcontentloaded')
            except Exception as e:
                logger.warning(f"[Sniff] goto failed: {e}")

            page.wait_for_timeout(SNIFF_WAIT_BEFORE_PLAY_MS)

            # Click play buttons
            for sel in [
                'button[aria-label*="play" i]',
                'button[title*="play" i]',
                '.play-button',
                '.vjs-big-play-button',
                '.plyr__control--overlaid',
                'button:has-text("Play")',
                'video',
            ]:
                try:
                    page.click(sel, timeout=1500)
                    logger.info(f"[Sniff] Clicked: {sel}")
                    page.wait_for_timeout(1500)
                    break
                except Exception:
                    pass

            # Force <video> tags to play
            try:
                page.evaluate("""
                    document.querySelectorAll('video').forEach(v => {
                        try { v.muted = true; v.play(); } catch (e) {}
                    });
                """)
            except Exception:
                pass

            # Auto-skip ads
            def try_skip_ads():
                for sel in [
                    '.skip-button',
                    '.skip-ad',
                    '.videoAdUiSkipButton',
                    'button[aria-label*="skip" i]',
                    'button:has-text("Skip Ad")',
                    'button:has-text("Skip")',
                    '.ytp-ad-skip-button',
                ]:
                    try:
                        page.click(sel, timeout=300)
                        logger.info(f"[Sniff] Skipped ad: {sel}")
                        return True
                    except Exception:
                        pass
                return False

            # Wait in 2s slices, trying to skip ads each time
            total_wait = SNIFF_WAIT_AFTER_PLAY_MS
            slice_ms = 2000
            elapsed = 0
            while elapsed < total_wait:
                if stop_event.is_set():
                    logger.info("[Sniff] Stop requested — shutting down early")
                    break
                page.wait_for_timeout(slice_ms)
                elapsed += slice_ms
                try_skip_ads()

            if not stop_event.is_set():
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[Sniff] Fatal error: {e}")
        finally:
            # Graceful shutdown
            for closer, name in ((context, 'context'), (browser, 'browser'), (pw, 'playwright')):
                if closer is None:
                    continue
                try:
                    if name == 'playwright':
                        closer.stop()
                    else:
                        closer.close()
                except Exception:
                    pass

    t = threading.Thread(target=run_sync, daemon=True, name="sniff-thread")
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        logger.warning("[Sniff] Timed out — requesting clean shutdown")
        stop_event.set()
        t.join(timeout=10)

    if t.is_alive():
        logger.warning("[Sniff] Thread still alive — force killing processes")
        _cleanup_playwright_processes()
        t.join(timeout=5)

    try:
        _sniff_lock.release()
    except Exception:
        pass

    _cleanup_playwright_processes()

    if not captured:
        logger.info("[Sniff] No media streams detected")
        return []

    # ---- Dedupe by normalizing low-latency rolling URLs ----
    def normalize_key(url):
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query, keep_blank_values=True)
            for k in list(qs.keys()):
                if k.lower() in ('_hls_msn', '_hls_part'):
                    del qs[k]
            new_q = '&'.join(f"{k}={v[0]}" for k, v in qs.items())
            base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            return base + (f"?{new_q}" if new_q else "")
        except Exception:
            return url

    deduped = []
    seen_keys = set()
    for item in captured:
        key = normalize_key(item['url'])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(item)

    # ---- Probe durations for m3u8/mpd candidates (top 5) ----
    hls_candidates = [c for c in deduped
                      if '.m3u8' in c['url'].lower()
                      or '.mpd' in c['url'].lower()]
    duration_map = {}
    for c in hls_candidates[:5]:
        try:
            dur = _probe_hls_duration(c['url'])
            duration_map[c['url']] = dur
            if dur:
                logger.info(f"[Sniff] Duration {dur}s for {c['url'][:90]}")
        except Exception:
            pass

    # ---- Rank ----
    def score(item):
        s = 0
        u = item['url'].lower()
        dur = duration_map.get(item['url'], 0)

        if item['is_thumbnail']:
            s -= 100

        # Duration is the strongest signal
        if dur >= 120:
            s += 200
        elif dur >= 60:
            s += 100
        elif dur > 0 and dur < 45:
            s -= 150

        if '/master/' in u or '/master.' in u:
            s += 60
        if 'auto.m3u8' in u:
            s += 40
        if '.m3u8' in u:
            s += 30
        if '.mpd' in u:
            s += 25

        if u.endswith('.mp4'):
            if item['size'] > 10_000_000:
                s += 60
            elif item['size'] > 1_000_000:
                s += 20
            else:
                s -= 40

        if 'video/mp4' in item['mime']:
            s += 10
        elif 'mpegurl' in item['mime']:
            s += 15

        s += min(item['order'], 5)

        return s

    ranked = sorted(deduped, key=score, reverse=True)

    out = []
    for item in ranked:
        out.append({
            'url': item['url'],
            'mime': item['mime'],
            'size': item['size'],
            'is_thumbnail': item['is_thumbnail'],
            'duration': duration_map.get(item['url'], 0),
        })

    MAX_CANDIDATES = 6
    out = out[:MAX_CANDIDATES]
    logger.info(f"[Sniff] {len(captured)} raw → {len(deduped)} deduped → {len(out)} clean candidate(s)")
    return out


# ============================================================
# MEDIA PROBE
# ============================================================
def _probe_media(url):
    info = {'url': url, 'mime': '', 'size': 0}
    try:
        session = requests.Session()
        if PROXY_DICT:
            session.proxies = PROXY_DICT
        session.headers.update({
            'User-Agent': USER_AGENT,
            'Referer': url,
        })
        r = session.head(url, allow_redirects=True, timeout=10)
        info['mime'] = (r.headers.get('content-type', '') or '').lower()
        cl = r.headers.get('content-length')
        info['size'] = int(cl) if cl else 0
    except Exception:
        pass
    return info


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
        raise Exception(f"Downloaded file is not a video ({src_size} bytes).")

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

    logger.info(f"Playlist folder: {folder_name}")

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
                continue

            m = RE_DEST.search(line)
            if m:
                current_title = extract_title(m.group(1).strip())
                current_video_size = 0
                current_video_downloaded = 0

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


# ============================================================
# MAIN ROUTER
# ============================================================
def process_url_download(task_id, url, quality='best', range_start=1, range_end=0):
    logger.info(f"process_url_download: {task_id} → {url}")
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

        logger.info("→ Unknown URL — trying yt-dlp --get-url")
        ytdlp_urls = _crack_ytdlp_geturl(url)
        if ytdlp_urls:
            logger.info(f"→ yt-dlp found {len(ytdlp_urls)} stream URL(s). Downloading first...")
            download_with_ytdlp(ytdlp_urls[0], task_id, quality)
            return

        logger.info("→ yt-dlp failed. Sniffing network for media streams...")
        task = load_task(task_id)
        if task:
            task['status'] = 'sniffing'
            task['progress'] = 0
            task['download_progress'] = 0
            save_task(task_id, task)

        candidates = _sniff_network(url)

        probed = []
        for c in candidates[:30]:
            if not c.get('size') or not c.get('mime'):
                info = _probe_media(c['url'])
                c['size'] = c.get('size') or info.get('size', 0)
                c['mime'] = c.get('mime') or info.get('mime', '')
            c['size_str'] = _fmt_bytes(c.get('size', 0))
            probed.append(c)

        task = load_task(task_id)
        if not task:
            return

        if not probed:
            task['status'] = 'error'
            task['error_msg'] = (
                "No media found on this page. The player may need a login "
                "or special headers. Try uploading fresh cookies via the browser extension."
            )
            save_task(task_id, task)
            return

        task['status'] = 'awaiting_choice'
        task['candidates'] = probed
        task['progress'] = 0
        task['download_progress'] = 0
        save_task(task_id, task)
        logger.info(f"→ {len(probed)} media candidate(s) ready — waiting for user choice")

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
# DOWNLOAD CHOSEN MEDIA
# ============================================================
def download_chosen_media(task_id, chosen_url, quality='best'):
    logger.info(f"download_chosen_media: {task_id} → {chosen_url[:120]}")
    task = load_task(task_id)
    if not task:
        return

    try:
        task['status'] = 'downloading'
        task['progress'] = 0
        task['download_progress'] = 0
        task['chosen_url'] = chosen_url
        task['candidates'] = []
        save_task(task_id, task)

        try:
            download_with_ytdlp(chosen_url, task_id, quality)
            return
        except Exception as e:
            logger.warning(f"yt-dlp on chosen URL failed: {e}. Trying direct download...")

        download_direct_file(chosen_url, task_id)

    except DownloadCancelled:
        task = load_task(task_id)
        if task:
            task['status'] = 'cancelled'
            task['error_msg'] = 'Cancelled by user'
            save_task(task_id, task)
    except Exception as e:
        logger.exception(f"Chosen download failed for {task_id}")
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
            'candidates': [],
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

    @app.route('/choose_media', methods=['POST'])
    def choose_media():
        task_id = request.form.get('task_id', '').strip()
        chosen_url = request.form.get('url', '').strip()
        quality = request.form.get('quality', 'best').strip().lower()
        if quality not in QUALITY_MAP:
            quality = 'best'

        if not task_id or not chosen_url:
            return jsonify({'error': 'task_id and url required'}), 400

        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        if task.get('status') != 'awaiting_choice':
            return jsonify({'error': f"Task is not awaiting choice (status: {task.get('status')})"}), 400

        candidates = task.get('candidates', [])
        valid_urls = {c['url'] for c in candidates}
        if chosen_url not in valid_urls:
            return jsonify({'error': 'Chosen URL is not in the candidate list'}), 400

        threading.Thread(
            target=download_chosen_media,
            args=(task_id, chosen_url, quality),
            daemon=True,
        ).start()

        return jsonify({'status': 'started'})

    @app.route('/cleanup_browsers', methods=['POST'])
    def cleanup_browsers():
        """Manually kill any orphan browser processes."""
        killed = _cleanup_playwright_processes(verbose=True)
        return jsonify({'killed': killed})

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
