import os
import re
import uuid
import threading
import time
import logging
import hashlib
import requests
import subprocess
import shutil
import signal
from urllib.parse import urlparse, unquote, parse_qs
from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, PROXY_DICT

logger = logging.getLogger(__name__)

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

CHUNK_SIZE = 64 * 1024


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

    # ---- Full browser-like headers ----
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,'
                  'image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
    })

    # ---- Referer ----
    try:
        parsed = urlparse(url)
        session.headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
    except Exception:
        pass

    # ---- Get total size & real filename ----
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
# SINGLE VIDEO DOWNLOADER (yt-dlp)
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
                    total_size = to_bytes(float(total_match.group(1)), total_match.group(2))
                speed_match = RE_SPEED.search(line)
                speed_kbps = 0
                if speed_match:
                    speed_bytes = to_bytes(float(speed_match.group(1)), speed_match.group(2))
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

    base_name = os.path.basename(urlparse(url).path.rstrip('/')) or 'video'
    base_name = re.sub(r'[^\w\-]', '_', base_name)[:80]
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
        if range_end > 0:
            items = f"{range_start}-{range_end}"
        else:
            items = f"{range_start}-"
        cmd += ['--playlist-items', items]
        logger.info(f"Playlist range: {items}")
    else:
        logger.info("Downloading full playlist")

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
                logger.info(f"Playlist {task_id} cancelled – killing yt-dlp")
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
                file_path = m.group(1).strip()
                current_title = extract_title(file_path)
                current_video_size = 0
                current_video_downloaded = 0
                logger.info(f"Playlist {task_id}: starting '{current_title}'")

            pct_m = RE_PCT.search(line)
            if pct_m:
                pct = float(pct_m.group(1))
                total_m = RE_TOTAL.search(line)
                if total_m:
                    current_video_size = to_bytes(
                        float(total_m.group(1)), total_m.group(2)
                    )
                if current_video_size > 0:
                    current_video_downloaded = int(current_video_size * pct / 100)

                speed_m = RE_SPEED.search(line)
                speed_kbps = 0
                if speed_m:
                    speed_bytes = to_bytes(float(speed_m.group(1)), speed_m.group(2))
                    speed_kbps = int(speed_bytes / 1024)

                if total_items > 0:
                    overall = int(((current_item - 1) * 100 + pct) / total_items)
                    overall = min(99, overall)
                else:
                    overall = int(pct)

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

    total_size = sum(
        os.path.getsize(os.path.join(playlist_dir, f))
        for f in video_files
    )

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

    logger.info(f"Playlist {task_id} finished: {len(video_files)} videos, "
                f"{total_size / 1024 / 1024:.1f} MB total in {folder_name}/")


# ============================================================
# MAIN ENTRY (smart routing)
# ============================================================
def process_url_download(task_id, url, quality='best', range_start=1, range_end=0):
    logger.info(f"process_url_download: {task_id} → {url} (quality={quality}, "
                f"range={range_start}-{range_end or 'end'})")
    task = load_task(task_id)
    if not task:
        return

    try:
        # ---- 1. Playlist ----
        if is_playlist_url(url):
            logger.info("→ Playlist")
            download_playlist(url, task_id, quality, range_start, range_end)
            return

        # ---- 2. Known video site or stream ----
        if needs_ytdlp(url):
            logger.info("→ yt-dlp (known video site or stream)")
            download_with_ytdlp(url, task_id, quality)
            return

        # ---- 3. URL with a real file extension (pdf, zip, mp4, etc.) ----
        ext = _ext(url)
        if ext and ext not in VIDEO_STREAM_EXTS:
            logger.info(f"→ Direct file (extension: {ext})")
            download_direct_file(url, task_id)
            return

        # ---- 4. Unknown URL – try yt-dlp first, fallback to direct ----
        logger.info("→ Unknown URL – trying yt-dlp first")
        try:
            download_with_ytdlp(url, task_id, quality)
        except Exception as ytdlp_err:
            err_text = str(ytdlp_err).lower()
            if any(kw in err_text for kw in [
                'unsupported url', 'no suitable', 'not a valid url',
                'unable to extract', 'no video formats', 'no video formats found'
            ]):
                logger.warning(f"yt-dlp can't handle it, falling back to direct: {ytdlp_err}")
                download_direct_file(url, task_id)
            else:
                raise

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

    if files.num_files() == 1:
        output_filename = files.file_path(0)
    else:
        output_filename = torrent_name + '.mp4'
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

        if url.startswith('magnet:') or (url.endswith('.torrent') and url.startswith(('http://', 'https://'))):
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
                        temp_torrent = os.path.join(UPLOAD_FOLDER, f"{task_id}_temp.torrent")
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
        threading.Thread(target=process_torrent_download, args=(task_id, temp_path), daemon=True).start()
        return jsonify({'task_id': task_id})
