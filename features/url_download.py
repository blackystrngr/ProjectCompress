import os
import re
import uuid
import threading
import time
import logging
import requests
import subprocess
import shutil
from urllib.parse import urlparse
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

# Path to cookies file (in project root)
COOKIES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cookies.txt')


class DownloadCancelled(Exception):
    pass


# ============================================================
# YT-DLP DOWNLOADER (chrome impersonation + cookies)
# ============================================================
def download_with_ytdlp(url, task_id, format_spec=None):
    """
    Download any supported URL with yt-dlp, using:
      - chrome impersonation (bypasses Cloudflare / anti-bot)
      - cookies.txt (for logged-in content)
      - auto-merge to mp4
    """
    task = load_task(task_id)
    if not task:
        raise Exception("Task not found")
    task['status'] = 'downloading'
    task['progress'] = 0
    save_task(task_id, task)

    # ---- Ensure /usr/local/bin is in PATH ----
    os.environ['PATH'] = '/usr/local/bin:' + os.environ.get('PATH', '')

    # ---- Find ffmpeg ----
    ffmpeg_path = shutil.which('ffmpeg')
    if not ffmpeg_path:
        for p in ['/usr/local/bin/ffmpeg', '/usr/bin/ffmpeg']:
            if os.path.exists(p) and os.access(p, os.X_OK):
                ffmpeg_path = p
                break
    if not ffmpeg_path:
        raise Exception("ffmpeg not found. Please install: sudo apt install ffmpeg")

    if not shutil.which('yt-dlp'):
        raise Exception("yt-dlp not installed. Run: pip install -U yt-dlp")

    # ---- Build output template ----
    output_template = os.path.join(UPLOAD_FOLDER, f"{task_id}_dl.%(ext)s")

    # ---- Build yt-dlp command ----
    # Format: user-specified (e.g. "720p") or fallback to best
    format_choice = format_spec or 'bestvideo+bestaudio/best'

    cmd = [
        'yt-dlp',
        '-o', output_template,
        '-f', format_choice,
        '--merge-output-format', 'mp4',
        '--no-part',
        '--no-mtime',
        '--no-warnings',
        '--ignore-errors',
        '--impersonate', 'chrome',              # <-- bypass anti-bot
        '--ffmpeg-location', ffmpeg_path,
        '--newline',                            # <-- so we get one line per update
    ]

    # Add cookies if present
    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]
        logger.info(f"Using cookies from {COOKIES_FILE}")
    else:
        logger.warning(f"No cookies.txt found at {COOKIES_FILE} – downloads may fail for login-only content.")

    cmd.append(url)

    logger.info(f"yt-dlp command: {' '.join(cmd)}")

    # ---- Run yt-dlp ----
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # merge so we can read progress from stdout
        text=True,
        bufsize=1,
    )

    output_lines = []
    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        if line:
            output_lines.append(line)
            # Progress lines: [download]  45.3% of ~ 50.23MiB at  2.31MiB/s ETA 00:23
            if '[download]' in line and '%' in line:
                match = re.search(r'(\d+(?:\.\d+)?)%', line)
                if match:
                    pct = float(match.group(1))
                    task = load_task(task_id)
                    if task:
                        task['progress'] = int(pct)
                        task['status'] = 'downloading'
                        save_task(task_id, task)
            # Log errors as they come
            if 'ERROR' in line:
                logger.error(f"yt-dlp: {line.strip()}")

    process.wait()

    if process.returncode != 0:
        full_output = ''.join(output_lines)
        logger.error(f"yt-dlp failed (code {process.returncode}):\n{full_output[-2000:]}")
        raise Exception(f"yt-dlp failed: {full_output[-500:]}")

    # ---- Find output file ----
    files = [f for f in os.listdir(UPLOAD_FOLDER) if f.startswith(f"{task_id}_dl.")]
    if not files:
        raise Exception("No output file found. Check yt-dlp logs.")

    # Prefer mp4, else use whatever yt-dlp produced
    mp4_files = [f for f in files if f.endswith('.mp4')]
    chosen = mp4_files[0] if mp4_files else files[0]
    src = os.path.join(UPLOAD_FOLDER, chosen)

    # Clean filename from URL
    base_name = os.path.basename(urlparse(url).path.rstrip('/')) or 'video'
    base_name = re.sub(r'[^\w\-]', '_', base_name)[:80]
    final_name = _get_unique_filename(f"{base_name}.mp4")
    dst = os.path.join(UPLOAD_FOLDER, final_name)
    os.rename(src, dst)

    task = load_task(task_id)
    task['status'] = 'done'
    task['progress'] = 100
    task['output_file'] = final_name
    task['download_progress'] = 100
    save_task(task_id, task)
    logger.info(f"Download completed: {final_name}")


# ============================================================
# ROUTE HANDLER – dispatch based on URL type
# ============================================================
def process_url_download(task_id, url, format_spec=None):
    logger.info(f"process_url_download started for {task_id}: {url}")
    task = load_task(task_id)
    if not task:
        return

    try:
        # Use yt-dlp for everything (handles m3u8, video pages, direct URLs)
        download_with_ytdlp(url, task_id, format_spec)
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
            raise Exception("Cancelled")
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

        # Optional format (e.g. "720p", "1080p", "best")
        format_spec = request.form.get('format', '').strip() or None

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'download_progress': 0,
            'created_at': time.time(),
            'cancelled': False,
            'url': url,
            'format': format_spec or 'best',
        }
        save_task(task_id, task_data)

        # Torrent handling
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
            # Everything else (m3u8, video pages, direct URLs) → yt-dlp
            def run():
                process_url_download(task_id, url, format_spec)
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
