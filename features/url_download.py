import os
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

def download_with_requests(url, output_path, task_id):
    """Download a file using requests with progress updates."""
    session = requests.Session()
    if PROXY_DICT:
        session.proxies = PROXY_DICT
    session.timeout = (30, 60)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'video/*,*/*;q=0.9',
    })
    try:
        # Try to get file size
        head_resp = session.head(url, allow_redirects=True, timeout=30)
        total = int(head_resp.headers.get('content-length', 0))
    except:
        total = 0
        logger.warning(f"Could not get content length for {url}")

    try:
        resp = session.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        downloaded = 0
        with open(output_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if load_task(task_id).get('cancelled', False):
                    raise Exception("Cancelled by user")
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(100 * downloaded / total)
                        task = load_task(task_id)
                        if task and task.get('download_progress') != pct:
                            task['download_progress'] = pct
                            save_task(task_id, task)
        logger.info(f"Download completed for task {task_id}")
        return True
    except Exception as e:
        logger.exception(f"Download error for {task_id}: {e}")
        raise

def process_url_download(task_id, url):
    """Background task for direct HTTP/HTTPS download."""
    logger.info(f"process_url_download started for {task_id} with URL {url}")
    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found at start of download")
        return
    task['status'] = 'downloading'
    task['download_progress'] = 0
    save_task(task_id, task)

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
            save_task(task_id, task)
        logger.info(f"Download finished: {final_name}")
    except Exception as e:
        logger.exception(f"Download failed for {task_id}")
        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        if os.path.exists(temp):
            os.remove(temp)

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
    task['status'] = 'downloading'
    save_task(task_id, task)
    while not handle.has_metadata():
        time.sleep(1)
    torrent_name = handle.name()
    files = handle.get_torrent_info().files()
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
        task = load_task(task_id)
        task['progress'] = progress
        task['download_speed'] = int(status.download_rate / 1000)
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
    task['status'] = 'done'
    task['output_file'] = final_name
    save_task(task_id, task)

def process_torrent_download(task_id, torrent_input):
    try:
        download_torrent(torrent_input, task_id, UPLOAD_FOLDER)
    except Exception as e:
        task = load_task(task_id)
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
        logger.info(f"New download task {task_id} for URL {url}")

        # Direct HTTP/HTTPS download
        if not (url.startswith('magnet:') or (url.endswith('.torrent') and url.startswith(('http://', 'https://')))):
            def run():
                process_url_download(task_id, url)
            threading.Thread(target=run, daemon=True).start()
        else:
            # Torrent
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
