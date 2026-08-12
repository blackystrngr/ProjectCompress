import os
import re
import uuid
import threading
import time
import logging
import requests
import subprocess
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

class DownloadCancelled(Exception):
    pass


# ------------------------------------------------------------
# M3U8 download with yt‑dlp (supports HLS streams)
# ------------------------------------------------------------
def download_m3u8_with_ytdlp(url, task_id):
    """
    Download an m3u8 stream using yt-dlp, merge to mp4, and save to UPLOAD_FOLDER.
    """
    task = load_task(task_id)
    if not task:
        raise Exception("Task not found")
    task['status'] = 'downloading_m3u8'
    task['progress'] = 0
    save_task(task_id, task)

    output_template = os.path.join(UPLOAD_FOLDER, f"{task_id}_m3u8.%(ext)s")
    cmd = [
        'yt-dlp',
        '-o', output_template,
        '-f', 'bestvideo+bestaudio/best',
        '--merge-output-format', 'mp4',
        '--no-part',
        '--no-mtime',
        '--no-warnings',
        '--ignore-errors',
        url
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        line = process.stderr.readline()
        if not line and process.poll() is not None:
            break
        # yt-dlp progress output looks like: [download] 45.3% of ~ 50.23MiB at  2.31MiB/s ETA 00:23
        if '[download]' in line and '%' in line:
            match = re.search(r'(\d+(?:\.\d+)?)%', line)
            if match:
                pct = float(match.group(1))
                task = load_task(task_id)
                if task:
                    task['progress'] = int(pct)
                    save_task(task_id, task)
    process.wait()
    if process.returncode != 0:
        raise Exception(f"yt-dlp failed with code {process.returncode}")

    # Find the downloaded file
    output_files = [f for f in os.listdir(UPLOAD_FOLDER) if f.startswith(f"{task_id}_m3u8.") and f.endswith('.mp4')]
    if not output_files:
        raise Exception("No output file found")
    final_name = _get_unique_filename(output_files[0].replace(f"{task_id}_m3u8.", ""))
    final_path = os.path.join(UPLOAD_FOLDER, final_name)
    os.rename(os.path.join(UPLOAD_FOLDER, output_files[0]), final_path)

    task = load_task(task_id)
    task['status'] = 'done'
    task['progress'] = 100
    task['output_file'] = final_name
    save_task(task_id, task)


# ------------------------------------------------------------
# Existing download functions (unchanged)
# ------------------------------------------------------------
def download_with_requests(url, output_path, task_id):
    """Download with detailed progress and safe task access."""
    session = requests.Session()
    if PROXY_DICT:
        session.proxies = PROXY_DICT
    session.headers.update({
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'video/*,*/*;q=0.9',
        'Connection': 'keep-alive',
    })

    total = 0
    try:
        head_resp = session.head(url, allow_redirects=True, timeout=30)
        total = int(head_resp.headers.get('content-length', 0))
        logger.info(f"Content-Length: {total} bytes")
    except:
        logger.warning("Could not get content-length.")

    task = load_task(task_id)
    if task:
        task['status'] = 'downloading'
        task['download_progress'] = 0
        task['total_size'] = total
        task['downloaded_size'] = 0
        task['download_speed'] = 0
        task['elapsed_time'] = 0
        save_task(task_id, task)
    else:
        logger.error(f"Task {task_id} not found at start")
        return False

    retries = 3
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            downloaded = 0
            start_time = time.time()
            last_update = time.time()
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    task = load_task(task_id)
                    if task and task.get('cancelled', False):
                        raise DownloadCancelled("Cancelled by user")
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_update >= 1:
                            elapsed = now - start_time
                            speed = (downloaded / elapsed) / 1024
                            pct = int(100 * downloaded / total) if total > 0 else 0
                            task = load_task(task_id)
                            if task:
                                task['download_progress'] = pct
                                task['downloaded_size'] = downloaded
                                task['download_speed'] = int(speed)
                                task['elapsed_time'] = int(elapsed)
                                save_task(task_id, task)
                            last_update = now
            task = load_task(task_id)
            if task:
                task['download_progress'] = 100
                task['downloaded_size'] = downloaded
                task['download_speed'] = 0
                task['elapsed_time'] = int(time.time() - start_time)
                save_task(task_id, task)
            return True

        except DownloadCancelled:
            raise
        except Exception as e:
            last_error = e
            logger.warning(f"Download attempt {attempt}/{retries} failed: {e}")
            if attempt == retries:
                raise Exception(f"Download failed after {retries} attempts: {e}")
            time.sleep(2 ** attempt)

    if last_error:
        raise Exception(f"Download failed: {last_error}")
    return False


# ------------------------------------------------------------
# URL / Magnet / Torrent main entry
# ------------------------------------------------------------
def process_url_download(task_id, url):
    logger.info(f"process_url_download started for {task_id}")
    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    # ---- Detect m3u8 ----
    if '.m3u8' in url.lower():
        try:
            download_m3u8_with_ytdlp(url, task_id)
            return
        except Exception as e:
            logger.exception(f"M3U8 download failed for {task_id}")
            task = load_task(task_id)
            if task and not task.get('cancelled', False):
                task['status'] = 'error'
                task['error_msg'] = str(e)
                save_task(task_id, task)
            return

    # ---- Regular HTTP download ----
    temp = os.path.join(UPLOAD_FOLDER, f"{task_id}_raw.mp4")
    try:
        download_with_requests(url, temp, task_id)
        if load_task(task_id).get('cancelled', False):
            if os.path.exists(temp):
                os.remove(temp)
            return

        final_name = _get_unique_filename("downloaded_video.mp4")
        final_path = os.path.join(UPLOAD_FOLDER, final_name)
        os.rename(temp, final_path)

        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['output_file'] = final_name
            task['download_progress'] = 100
            task['download_speed'] = 0
            save_task(task_id, task)
        logger.info(f"Download completed: {final_name}")

    except Exception as e:
        logger.exception(f"Download failed for {task_id}")
        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        if os.path.exists(temp):
            os.remove(temp)


# ------------------------------------------------------------
# Torrent download (unchanged)
# ------------------------------------------------------------
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


def _get_unique_filename(filename):
    base, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(UPLOAD_FOLDER, new_name)):
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    return new_name


# ------------------------------------------------------------
# Flask routes
# ------------------------------------------------------------
def register_routes(app):
    @app.route('/start', methods=['POST'])
    def start():
        url = request.form.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL required'}), 400
        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'download_progress': 0,
            'created_at': time.time(),
            'cancelled': False,
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
                process_url_download(task_id, url)
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
