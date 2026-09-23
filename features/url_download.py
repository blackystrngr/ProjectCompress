import os
import re
import uuid
import threading
import time
import logging
import requests
import subprocess
import shutil
import signal
from urllib.parse import urlparse, unquote
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

# ---- Format selectors for yt-dlp ----
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

# ---- Video extensions (use yt-dlp) ----
VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v',
    '.m3u8', '.ts', '.mpd', '.mpg', '.mpeg', '.wmv', '.3gp'
}

# ---- Direct file extensions (use requests) ----
# Everything that isn't a video OR audio gets downloaded as-is
AUDIO_EXTS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma', '.opus'}

# Common direct-download extensions (for fast detection without HEAD request)
DIRECT_EXTS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tiff',
    '.exe', '.msi', '.deb', '.rpm', '.dmg', '.pkg',
    '.apk', '.ipa', '.appimage',
    '.iso', '.img',
    '.txt', '.csv', '.json', '.xml', '.yaml', '.yml', '.md', '.log',
    '.py', '.js', '.sh', '.c', '.cpp', '.h', '.java', '.go', '.rs',
    '.epub', '.mobi', '.azw3',
    '.ttf', '.otf', '.woff', '.woff2',
    '.sqlite', '.db',
}

CHUNK_SIZE = 64 * 1024   # 64 KB chunks


class DownloadCancelled(Exception):
    pass


# ============================================================
# GLOBAL PROCESS TRACKER (for yt-dlp cancellation)
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
# DETECT URL TYPE
# ============================================================
def _get_extension_from_url(url):
    """Try to guess the file extension from the URL path."""
    try:
        path = urlparse(url).path
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        return ext
    except Exception:
        return ''


def _guess_filename_from_url(url):
    """Return a clean filename from the URL path."""
    try:
        path = urlparse(url).path
        name = os.path.basename(path)
        if name:
            # URL-decode (handles %20 etc.)
            name = unquote(name)
            # Strip query-like tail if any
            name = re.sub(r'[?&].*$', '', name)
            return name
    except Exception:
        pass
    return 'download'


def is_direct_file_url(url):
    """
    Return True if the URL looks like a direct file (not a video page or stream).
    Uses URL extension as the primary hint.
    """
    ext = _get_extension_from_url(url)

    # Video URLs → use yt-dlp (handles m3u8, YouTube, etc.)
    if ext in VIDEO_EXTS:
        return False

    # Audio files → direct download (faster than yt-dlp)
    if ext in AUDIO_EXTS:
        return True

    # Known non-video extensions → direct
    if ext in DIRECT_EXTS:
        return True

    # Unknown extension with no extension → let yt-dlp try first
    # (it'll quickly fail for real files and we fall back)
    return False


# ============================================================
# DIRECT FILE DOWNLOAD (requests)
# ============================================================
def download_direct_file(url, task_id):
    """
    Download a direct file (PDF, ZIP, image, audio, etc.) with requests.
    Reports progress, size, and speed. Supports cancellation.
    """
    task = load_task(task_id)
    if not task:
        raise Exception("Task not found")

    filename = _guess_filename_from_url(url) or f"{task_id}_file"
    final_name = _get_unique_filename(filename)
    output_path = os.path.join(UPLOAD_FOLDER, final_name)

    session = requests.Session()
    if PROXY_DICT:
        session.proxies = PROXY_DICT
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
    })

    # ---- Get total size ----
    total = 0
    try:
        head = session.head(url, allow_redirects=True, timeout=30)
        total = int(head.headers.get('content-length', 0))
        # Get real filename from Content-Disposition if present
        cd = head.headers.get('content-disposition', '')
        if cd and 'filename=' in cd:
            fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', cd)
            if fn_match:
                suggested = unquote(fn_match.group(1)).strip()
                if suggested:
                    final_name = _get_unique_filename(suggested)
                    output_path = os.path.join(UPLOAD_FOLDER, final_name)
    except Exception as e:
        logger.warning(f"HEAD request failed for {url}: {e}")

    # ---- Update task ----
    task = load_task(task_id)
    task['status'] = 'downloading'
    task['download_progress'] = 0
    task['progress'] = 0
    task['total_size'] = total
    task['downloaded_size'] = 0
    task['download_speed'] = 0
    task['elapsed_time'] = 0
    save_task(task_id, task)

    # ---- Stream download ----
    start_time = time.time()
    last_update = 0
    downloaded = 0
    temp_path = output_path + '.part'

    try:
        resp = session.get(url, stream=True, timeout=(15, 120), allow_redirects=True)
        resp.raise_for_status()

        # Re-check content-length from GET (HEAD might have failed)
        if total == 0:
            total = int(resp.headers.get('content-length', 0))
            task = load_task(task_id)
            if task:
                task['total_size'] = total
                save_task(task_id, task)

        with open(temp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                # ---- Cancellation check ----
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

        # ---- Move .part → final ----
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

        logger.info(f"Direct file download complete: {final_name} "
                    f"({final_size / 1024 / 1024:.1f} MB in {elapsed:.1f}s)")

    except DownloadCancelled:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


# ============================================================
# YT-DLP DOWNLOADER (videos)
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

    ffmpeg_path = shutil.which('ffmpeg')
    if not ffmpeg_path:
        for p in ['/usr/local/bin/ffmpeg', '/usr/bin/ffmpeg']:
            if os.path.exists(p) and os.access(p, os.X_OK):
                ffmpeg_path = p
                break
    if not ffmpeg_path:
        raise Exception("ffmpeg not found. Install: sudo apt install ffmpeg")

    if not shutil.which('yt-dlp'):
        raise Exception("yt-dlp not installed. Run: pip install -U yt-dlp")

    format_choice = QUALITY_MAP.get(quality, QUALITY_MAP['best'])
    output_template = os.path.join(UPLOAD_FOLDER, f"{task_id}_dl.%(ext)s")

    cmd = [
        'yt-dlp',
        '-o', output_template,
        '-f', format_choice,
        '--merge-output-format', 'mp4',
        '--no-part',
        '--no-mtime',
        '--no-warnings',
        '--ignore-errors',
        '--impersonate', 'chrome',
        '--extractor-args', 'youtube:player_client=mweb',
        '--ffmpeg-location', ffmpeg_path,
        '--newline',
        '--progress',
        '--no-colors',
        '--concurrent-fragments', '4',
        '--retries', '5',
        '--fragment-retries', '5',
    ]

    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]
        logger.info(f"Using cookies from {COOKIES_FILE}")
    else:
        logger.warning(f"No cookies.txt found at {COOKIES_FILE}")

    cmd.append(url)
    logger.info(f"yt-dlp command: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        preexec_fn=os.setsid,
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
        factors = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3, 'TB': 1024 ** 4}
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
                logger.info(f"Task {task_id} cancelled – killing yt-dlp")
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
                if now - last_update_time >= 0.5:
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
    logger.info(f"Download completed: {final_name} ({final_size / 1024 / 1024:.1f} MB)")


# ============================================================
# MAIN ENTRY (chooses direct vs yt-dlp)
# ============================================================
def process_url_download(task_id, url, quality='best'):
    logger.info(f"process_url_download started for {task_id}: {url} (quality={quality})")
    task = load_task(task_id)
    if not task:
        return

    try:
        # ---- Route to the right downloader ----
        if is_direct_file_url(url):
            logger.info(f"Detected direct file → using requests downloader")
            download_direct_file(url, task_id)
        else:
            logger.info(f"Detected video/stream → using yt-dlp")
            try:
                download_with_ytdlp(url, task_id, quality)
            except Exception as ytdlp_err:
                # Fallback: yt-dlp can't handle it, try direct download
                err_text = str(ytdlp_err).lower()
                non_video_errors = [
                    'unsupported url', 'no video formats', 'requested format is not available',
                    'not a valid url', 'is not a valid url', 'no suitable'
                ]
                if any(e in err_text for e in non_video_errors):
                    logger.warning(f"yt-dlp rejected URL, falling back to direct download")
                    download_direct_file(url, task_id)
                else:
                    raise
    except DownloadCancelled:
        logger.info(f"Download cancelled for {task_id}")
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
# TORRENT SUPPORT (unchanged)
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
                process_url_download(task_id, url, quality)
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
