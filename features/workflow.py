# features/workflow.py
"""
Node-based workflow automation.

Each workflow is a linear chain: [Trigger] → [Action] → [Action] → ...
The trigger provides a list of items (URLs). Each downstream action runs
once per item.
"""

import os
import re
import json
import time
import uuid
import threading
import subprocess
import logging
import shutil
from flask import request, jsonify
from tasks import save_task, load_task, delete_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(PROJECT_ROOT, 'workflows')
os.makedirs(WORKFLOWS_DIR, exist_ok=True)


class _Cancelled(Exception):
    pass


# ============================================================
# Node types
# ============================================================
NODE_TYPES = {
    'trigger.url_list': {
        'label': 'URL List',
        'category': 'trigger',
        'icon': 'fa-list',
        'color': '#4ade80',
        'description': 'Provides a list of URLs. Each downstream action '
                       'runs once per URL.',
        'fields': [
            {'name': 'urls', 'type': 'textarea', 'label': 'URLs (one per line)',
             'required': True,
             'placeholder': 'https://youtube.com/watch?v=...\nhttps://example.com/file.mp4',
             'help': 'Blank lines and lines starting with # are ignored.'},
            {'name': 'quality', 'type': 'select', 'label': 'Download quality',
             'options': ['best', '2160p', '1440p', '1080p', '720p', '480p', 'audio'],
             'default': '1080p'},
        ],
    },
    'action.download': {
        'label': 'Download Video',
        'category': 'action',
        'icon': 'fa-download',
        'color': '#5f8fe0',
        'description': 'Downloads the current URL with yt-dlp. Handles '
                       'YouTube, Vimeo, Twitter, TikTok, and thousands more.',
        'fields': [],
    },
    'action.download_file': {
        'label': 'Download File',
        'category': 'action',
        'icon': 'fa-file-arrow-down',
        'color': '#5f8fe0',
        'description': 'Downloads the current URL as a raw file. Use for '
                       'PDFs, ZIPs, images, APKs.',
        'fields': [],
    },
    'action.telegram_send': {
        'label': 'Send to Telegram',
        'category': 'action',
        'icon': 'fa-paper-plane',
        'color': '#22d3ee',
        'description': 'Sends the current file to a Telegram chat as a '
                       'document (no compression).',
        'fields': [
            {'name': 'chat_link', 'type': 'text', 'label': 'Chat link',
             'required': True,
             'placeholder': '@channel or https://t.me/...'},
            {'name': 'caption', 'type': 'text', 'label': 'Caption',
             'default': '📁 {filename}',
             'help': 'Placeholders: {filename}, {url}, {index}, {total}'},
        ],
    },
    'action.drive_upload': {
        'label': 'Upload to Drive',
        'category': 'action',
        'icon': 'fa-cloud-upload-alt',
        'color': '#a78bfa',
        'description': 'Uploads the current file to Google Drive '
                       '(Colab_Processing folder).',
        'fields': [
            {'name': 'account', 'type': 'text', 'label': 'Drive account',
             'default': 'default',
             'help': 'Match token_<name>.json — "default" uses token.json'},
        ],
    },
    'action.compress': {
        'label': 'Compress Video',
        'category': 'action',
        'icon': 'fa-compress',
        'color': '#f59e0b',
        'description': 'Re-encodes the current video with H.265 or AV1. '
                       'Slower but much smaller.',
        'fields': [
            {'name': 'codec', 'type': 'select', 'label': 'Codec',
             'options': ['x265', 'av1'], 'default': 'x265'},
            {'name': 'resolution', 'type': 'select', 'label': 'Resolution',
             'options': ['360', '480', '720', '1080'], 'default': '720'},
            {'name': 'crf', 'type': 'number', 'label': 'CRF',
             'default': 27,
             'help': 'Lower = better quality, bigger file. x265: 24-30, AV1: 28-35'},
            {'name': 'preset', 'type': 'text', 'label': 'Preset',
             'default': 'veryfast',
             'help': 'x265: ultrafast…medium · AV1: 8 (best) … 13 (fastest)'},
        ],
    },
    'action.delete_local': {
        'label': 'Delete Local File',
        'category': 'action',
        'icon': 'fa-trash-alt',
        'color': '#ef4444',
        'description': 'Deletes the current file from local disk. Useful '
                       'as the last step after uploading.',
        'fields': [],
    },
    'action.delete_drive': {
        'label': 'Delete Drive File',
        'category': 'action',
        'icon': 'fa-trash-alt',
        'color': '#ef4444',
        'description': 'Deletes the file from Drive (requires Upload to '
                       'Drive earlier in the chain).',
        'fields': [
            {'name': 'account', 'type': 'text', 'label': 'Drive account',
             'default': 'default'},
        ],
    },
    'action.wait': {
        'label': 'Wait',
        'category': 'action',
        'icon': 'fa-clock',
        'color': '#8a9bc0',
        'description': 'Pauses the workflow for N seconds. Useful between '
                       'requests to avoid rate limits.',
        'fields': [
            {'name': 'seconds', 'type': 'number', 'label': 'Seconds',
             'default': 5},
        ],
    },
    'action.webhook': {
        'label': 'Webhook',
        'category': 'action',
        'icon': 'fa-globe',
        'color': '#4ade80',
        'description': 'Sends an HTTP request. Useful for Slack/Discord '
                       'notifications or triggering external tools.',
        'fields': [
            {'name': 'url', 'type': 'text', 'label': 'URL',
             'required': True,
             'placeholder': 'https://hooks.slack.com/services/...'},
            {'name': 'method', 'type': 'select', 'label': 'Method',
             'options': ['POST', 'GET'], 'default': 'POST'},
            {'name': 'body', 'type': 'textarea', 'label': 'Body',
             'default': '{"text": "Downloaded {filename} ({size}) from {url}"}',
             'help': 'Placeholders: {filename}, {url}, {size}, {index}, {total}'},
        ],
    },
}


# ============================================================
# Throttled task updates
# ============================================================
_last_save = {}
_last_save_lock = threading.Lock()


def _update_node_progress(task_id, node_id, progress, status=None):
    """Update a node's progress with 0.5s throttling."""
    with _last_save_lock:
        now = time.time()
        if now - _last_save.get(task_id, 0) < 0.5 and (progress or 0) < 100:
            return
        _last_save[task_id] = now

    task = load_task(task_id)
    if not task:
        return
    statuses = task.get('node_statuses', {})
    entry = statuses.setdefault(node_id, {})
    entry['progress'] = int(progress or 0)
    if status:
        entry['status'] = status
    task['node_statuses'] = statuses
    save_task(task_id, task)


def _log(task_id, node_id, level, msg):
    """Append a log entry to the task."""
    task = load_task(task_id)
    if not task:
        return
    logs = task.get('logs', [])
    logs.append({
        'ts': time.time(),
        'node_id': node_id,
        'level': level,
        'msg': str(msg),
    })
    task['logs'] = logs[-500:]
    save_task(task_id, task)


def _is_cancelled(task_id):
    task = load_task(task_id)
    return bool(task and task.get('cancelled'))


# ============================================================
# Helpers
# ============================================================
def _safe_name(name, default='download'):
    name = (name or '').strip().rstrip('.,;:!?)')
    name = re.sub(r'[^\w\-\. ]', '_', name)
    return name or default


def _unique_filename(name, folder=None):
    folder = folder or UPLOAD_FOLDER
    base, ext = os.path.splitext(name)
    out = name
    counter = 1
    while os.path.exists(os.path.join(folder, out)):
        out = f"{base}_{counter}{ext}"
        counter += 1
    return out


def _filename_from_url(url):
    try:
        from urllib.parse import urlparse, unquote
        name = os.path.basename(urlparse(url).path)
        if name:
            return unquote(name)
    except Exception:
        pass
    return ''


def _format_size(n):
    try:
        n = int(n)
    except Exception:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def _expand_template(text, ctx):
    """Replace {key} placeholders in a template string."""
    if not text:
        return ''
    values = {
        'url':      ctx.get('url', ''),
        'filename': ctx.get('filename', ''),
        'basename': ctx.get('basename', ''),
        'size':     _format_size(ctx.get('size', 0)),
        'index':    str(ctx.get('index', '')),
        'total':    str(ctx.get('total', '')),
        'ext':      os.path.splitext(ctx.get('filename', ''))[1].lstrip('.'),
    }
    def replace(match):
        key = match.group(1)
        return values.get(key, match.group(0))
    return re.sub(r'\{(\w+)\}', replace, text)


# ============================================================
# Action handlers
# ============================================================
def _handle_download(task_id, node_id, config, ctx):
    url = ctx.get('url')
    if not url:
        raise Exception('No URL in context')

    from features.url_download import QUALITY_MAP, COOKIES_FILE
    quality = ctx.get('quality', 'best')
    format_choice = QUALITY_MAP.get(quality, QUALITY_MAP['best'])

    base_name = _filename_from_url(url)
    if not base_name or not os.path.splitext(base_name)[1]:
        base_name = _safe_name(ctx.get('basename') or f'video_{uuid.uuid4().hex[:8]}') + '.mp4'
    out_name = _unique_filename(base_name)
    out_path = os.path.join(UPLOAD_FOLDER, out_name)
    template = os.path.join(UPLOAD_FOLDER, f"_wf_{task_id}_{node_id}.%(ext)s")

    cmd = [
        'yt-dlp', '-o', template,
        '-f', format_choice,
        '--merge-output-format', 'mp4',
        '--no-part', '--no-mtime', '--no-warnings', '--ignore-errors',
        '--no-playlist',
        '--impersonate', 'chrome',
        '--extractor-args', 'youtube:player_client=mweb',
        '--newline', '--no-colors',
        '--retries', '3',
    ]
    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]
    cmd.append(url)

    _log(task_id, node_id, 'info', f'yt-dlp: {url[:80]}')

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, preexec_fn=os.setsid,
    )
    try:
        from features.url_download import register_process, unregister_process, kill_process
        register_process(task_id, proc)
        try:
            for line in proc.stdout:
                if _is_cancelled(task_id):
                    kill_process(task_id)
                    raise _Cancelled()
                m = re.search(r'\[download\]\s+(\d+(?:\.\d+)?)%', line)
                if m:
                    _update_node_progress(task_id, node_id, float(m.group(1)))
        finally:
            unregister_process(task_id)
        proc.wait()
    except _Cancelled:
        raise
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    if proc.returncode != 0:
        raise Exception(f'yt-dlp exited with code {proc.returncode}')

    # Find output file
    prefix = f"_wf_{task_id}_{node_id}."
    candidates = [f for f in os.listdir(UPLOAD_FOLDER) if f.startswith(prefix)]
    if not candidates:
        raise Exception('Download produced no file')
    mp4s = [f for f in candidates if f.endswith('.mp4')]
    chosen = mp4s[0] if mp4s else candidates[0]
    actual_path = os.path.join(UPLOAD_FOLDER, chosen)
    final_path = os.path.join(UPLOAD_FOLDER, out_name)
    if actual_path != final_path:
        try:
            os.rename(actual_path, final_path)
        except Exception:
            final_path = actual_path

    size = os.path.getsize(final_path)
    ctx['file_path'] = final_path
    ctx['filename'] = os.path.basename(final_path)
    ctx['basename'] = os.path.splitext(ctx['filename'])[0]
    ctx['size'] = size

    _update_node_progress(task_id, node_id, 100, 'done')
    _log(task_id, node_id, 'success', f'Downloaded {ctx["filename"]} ({_format_size(size)})')
    return ctx


def _handle_download_file(task_id, node_id, config, ctx):
    import requests
    url = ctx.get('url')
    if not url:
        raise Exception('No URL in context')

    name = _filename_from_url(url)
    if not name:
        name = f'file_{uuid.uuid4().hex[:8]}'
    out_name = _unique_filename(name)
    out_path = os.path.join(UPLOAD_FOLDER, out_name)
    tmp_path = out_path + '.part'

    _log(task_id, node_id, 'info', f'GET {url[:80]}')

    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(tmp_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if _is_cancelled(task_id):
                        raise _Cancelled()
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        _update_node_progress(task_id, node_id,
                                              int(100 * downloaded / total))
    except _Cancelled:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    os.rename(tmp_path, out_path)
    size = os.path.getsize(out_path)
    ctx['file_path'] = out_path
    ctx['filename'] = out_name
    ctx['basename'] = os.path.splitext(out_name)[0]
    ctx['size'] = size

    _update_node_progress(task_id, node_id, 100, 'done')
    _log(task_id, node_id, 'success', f'Downloaded {out_name} ({_format_size(size)})')
    return ctx


def _handle_telegram_send(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file in context — add a Download node first')

    chat_link = (config.get('chat_link') or '').strip()
    if not chat_link:
        raise Exception('Chat link is required')

    caption = _expand_template(config.get('caption') or '', ctx) or None

    from features.telegram import (
        _run_telegram_job, send_file_to_telegram_async, start_tg_loop_if_needed,
    )
    start_tg_loop_if_needed()

    sub_id = str(uuid.uuid4())
    save_task(sub_id, {
        'task_id': sub_id,
        'status': 'queued',
        'upload_progress': 0,
        'created_at': time.time(),
        'cancelled': False,
        '_hidden': True,
        'parent_workflow': task_id,
    })

    def _run():
        try:
            _run_telegram_job(
                send_file_to_telegram_async,
                sub_id, chat_link, file_path,
                os.path.basename(file_path), caption,
            )
        except Exception as e:
            t = load_task(sub_id)
            if t:
                t['status'] = 'error'
                t['error_msg'] = str(e)
                save_task(sub_id, t)

    threading.Thread(target=_run, daemon=True).start()

    deadline = time.time() + 3600
    while time.time() < deadline:
        if _is_cancelled(task_id):
            try:
                from features.telegram import cancel_telegram_task
                cancel_telegram_task(sub_id)
            except Exception:
                pass
            delete_task(sub_id)
            raise _Cancelled()

        sub = load_task(sub_id)
        if not sub:
            time.sleep(0.3)
            continue

        if sub.get('status') == 'done':
            _update_node_progress(task_id, node_id, 100, 'done')
            _log(task_id, node_id, 'success',
                 f'Sent to {chat_link}')
            delete_task(sub_id)
            return ctx

        if sub.get('status') == 'error':
            err = sub.get('error_msg', 'Telegram send failed')
            delete_task(sub_id)
            raise Exception(err)

        _update_node_progress(task_id, node_id, sub.get('upload_progress', 0))
        time.sleep(0.5)

    delete_task(sub_id)
    raise Exception('Telegram send timed out (1 hour)')


def _handle_drive_upload(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file in context — add a Download node first')

    account = (config.get('account') or 'default').strip()

    from features.google_drive import get_drive_service, get_colab_folder_id
    from googleapiclient.http import MediaFileUpload

    service = get_drive_service(account)
    folder_id = get_colab_folder_id(account)
    if not folder_id:
        raise Exception(f'Cannot resolve Colab_Processing for {account}')

    filename = os.path.basename(file_path)
    media = MediaFileUpload(file_path, resumable=True, chunksize=10 * 1024 * 1024)
    req = service.files().create(
        body={'name': filename, 'parents': [folder_id]},
        media_body=media,
        fields='id, name, webViewLink',
    )

    response = None
    while response is None:
        if _is_cancelled(task_id):
            raise _Cancelled()
        status, response = req.next_chunk()
        if status:
            _update_node_progress(task_id, node_id,
                                  int(status.progress() * 100))

    ctx['drive_file_id'] = response['id']
    ctx['drive_link'] = response.get('webViewLink')
    _update_node_progress(task_id, node_id, 100, 'done')
    _log(task_id, node_id, 'success',
         f'Uploaded to Drive ({account}): {filename}')
    return ctx


def _handle_compress(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file to compress')

    codec = (config.get('codec') or 'x265').lower()
    resolution = str(config.get('resolution') or '720')
    try:
        crf = int(config.get('crf') or 27)
    except (TypeError, ValueError):
        crf = 27
    preset = config.get('preset') or ('veryfast' if codec == 'x265' else '12')

    base = os.path.splitext(os.path.basename(file_path))[0]
    out_name = _unique_filename(f'{base}_{codec}.mp4')
    out_path = os.path.join(UPLOAD_FOLDER, out_name)

    vf = f'scale=-2:{resolution}:flags=lanczos,fps=30'
    if codec == 'x265':
        vcodec = ['-c:v', 'libx265', '-preset', str(preset), '-crf', str(crf),
                  '-pix_fmt', 'yuv420p']
    else:
        vcodec = ['-c:v', 'libsvtav1', '-preset', str(preset), '-crf', str(crf),
                  '-pix_fmt', 'yuv420p10le']

    # Get source duration for progress
    duration = 0
    try:
        ffprobe = shutil.which('ffprobe') or 'ffprobe'
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(r.stdout.strip()) if r.stdout.strip() else 0
    except Exception:
        pass

    cmd = [
        'ffmpeg', '-y',
        '-progress', 'pipe:2', '-stats_period', '0.5',
        '-i', file_path,
        '-vf', vf, *vcodec,
        '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
        '-af', 'aresample=async=1:first_pts=0',
        '-movflags', '+faststart',
        out_path,
    ]

    _log(task_id, node_id, 'info',
         f'Compressing ({codec} {resolution}p crf={crf} preset={preset})')

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, bufsize=1, preexec_fn=os.setsid,
    )
    try:
        from features.url_download import register_process, unregister_process, kill_process
        register_process(task_id, proc)
        try:
            for line in proc.stderr:
                if _is_cancelled(task_id):
                    kill_process(task_id)
                    raise _Cancelled()
                m = re.search(r'out_time_us=(\d+)', line)
                if m and duration > 0:
                    cur = int(m.group(1)) / 1_000_000
                    _update_node_progress(task_id, node_id,
                                          min(100, int(100 * cur / duration)))
        finally:
            unregister_process(task_id)
        proc.wait()
    except _Cancelled:
        raise
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    if proc.returncode != 0 or not os.path.exists(out_path):
        raise Exception(f'ffmpeg exited with code {proc.returncode}')

    orig_size = os.path.getsize(file_path)
    new_size = os.path.getsize(out_path)
    savings = (1 - new_size / orig_size) * 100 if orig_size else 0

    ctx['original_file_path'] = file_path
    ctx['file_path'] = out_path
    ctx['filename'] = out_name
    ctx['basename'] = os.path.splitext(out_name)[0]
    ctx['size'] = new_size

    _update_node_progress(task_id, node_id, 100, 'done')
    _log(task_id, node_id, 'success',
         f'Compressed: {_format_size(orig_size)} → {_format_size(new_size)} '
         f'({savings:.0f}% smaller)')
    return ctx


def _handle_delete_local(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path:
        _log(task_id, node_id, 'warning', 'No file in context — skipping')
        _update_node_progress(task_id, node_id, 100, 'done')
        return ctx

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            _log(task_id, node_id, 'success',
                 f'Deleted {os.path.basename(file_path)}')
        except Exception as e:
            raise Exception(f'Delete failed: {e}')
    else:
        _log(task_id, node_id, 'warning', 'File already missing')

    # Also delete original if compress produced one and user asked
    orig = ctx.get('original_file_path')
    if orig and orig != file_path and os.path.exists(orig):
        try:
            os.remove(orig)
            _log(task_id, node_id, 'info',
                 f'Also deleted original {os.path.basename(orig)}')
        except Exception:
            pass

    ctx.pop('file_path', None)
    ctx.pop('filename', None)
    ctx.pop('basename', None)
    ctx.pop('size', None)
    ctx.pop('original_file_path', None)

    _update_node_progress(task_id, node_id, 100, 'done')
    return ctx


def _handle_delete_drive(task_id, node_id, config, ctx):
    file_id = ctx.get('drive_file_id')
    if not file_id:
        _log(task_id, node_id, 'warning', 'No Drive file in context — skipping')
        _update_node_progress(task_id, node_id, 100, 'done')
        return ctx

    account = (config.get('account') or 'default').strip()
    from features.google_drive import get_drive_service
    service = get_drive_service(account)
    try:
        service.files().delete(fileId=file_id).execute()
        _log(task_id, node_id, 'success', f'Deleted Drive file ({account})')
    except Exception as e:
        _log(task_id, node_id, 'warning', f'Drive delete failed: {e}')

    ctx.pop('drive_file_id', None)
    ctx.pop('drive_link', None)
    _update_node_progress(task_id, node_id, 100, 'done')
    return ctx


def _handle_wait(task_id, node_id, config, ctx):
    try:
        seconds = float(config.get('seconds') or 5)
    except (TypeError, ValueError):
        seconds = 5
    seconds = max(0, min(seconds, 600))
    _log(task_id, node_id, 'info', f'Waiting {seconds}s')
    end = time.time() + seconds
    while time.time() < end:
        if _is_cancelled(task_id):
            raise _Cancelled()
        time.sleep(0.2)
    _update_node_progress(task_id, node_id, 100, 'done')
    return ctx


def _handle_webhook(task_id, node_id, config, ctx):
    import requests
    url = (config.get('url') or '').strip()
    if not url:
        raise Exception('Webhook URL required')

    method = (config.get('method') or 'POST').upper()
    body_template = config.get('body') or ''
    body = _expand_template(body_template, ctx)

    _log(task_id, node_id, 'info', f'{method} {url[:80]}')
    try:
        if method == 'GET':
            r = requests.get(url, timeout=30)
        else:
            r = requests.post(url, data=body, timeout=30,
                              headers={'Content-Type': 'application/json'})
        _update_node_progress(task_id, node_id, 100, 'done')
        if 200 <= r.status_code < 300:
            _log(task_id, node_id, 'success', f'HTTP {r.status_code}')
        else:
            _log(task_id, node_id, 'warning', f'HTTP {r.status_code}')
        return ctx
    except Exception as e:
        raise Exception(f'Webhook failed: {e}')


NODE_HANDLERS = {
    'action.download':      _handle_download,
    'action.download_file': _handle_download_file,
    'action.telegram_send': _handle_telegram_send,
    'action.drive_upload':  _handle_drive_upload,
    'action.compress':      _handle_compress,
    'action.delete_local':  _handle_delete_local,
    'action.delete_drive':  _handle_delete_drive,
    'action.wait':          _handle_wait,
    'action.webhook':       _handle_webhook,
}


# ============================================================
# Workflow storage
# ============================================================
def _wf_path(wf_id):
    return os.path.join(WORKFLOWS_DIR, f'{wf_id}.json')


def _load_workflow(wf_id):
    path = _wf_path(wf_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f'Failed to load workflow {wf_id}: {e}')
        return None


def _save_workflow(data):
    wf_id = data.get('id') or uuid.uuid4().hex[:12]
    data['id'] = wf_id
    now = time.time()
    data.setdefault('created_at', now)
    data['updated_at'] = now
    try:
        tmp = _wf_path(wf_id) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _wf_path(wf_id))
        return wf_id
    except Exception as e:
        logger.error(f'Failed to save workflow: {e}')
        return None


def _list_workflows():
    out = []
    try:
        for f in sorted(os.listdir(WORKFLOWS_DIR)):
            if not f.endswith('.json'):
                continue
            data = _load_workflow(f[:-5])
            if not data:
                continue
            out.append({
                'id': data['id'],
                'name': data.get('name', 'Untitled'),
                'node_count': len(data.get('nodes', [])),
                'updated_at': data.get('updated_at', 0),
            })
    except Exception as e:
        logger.warning(f'Failed to list workflows: {e}')
    out.sort(key=lambda x: x['updated_at'], reverse=True)
    return out


# ============================================================
# Validation
# ============================================================
def _validate_workflow(workflow):
    if not isinstance(workflow, dict):
        return 'Workflow must be an object'
    nodes = workflow.get('nodes')
    if not isinstance(nodes, list) or not nodes:
        return 'Workflow must have at least one node'

    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            return f'Node {i} must be an object'
        if not node.get('id') or not isinstance(node['id'], str):
            return f'Node {i} missing id'
        ntype = node.get('type')
        if ntype not in NODE_TYPES:
            return f'Node {i} has unknown type: {ntype}'
        if i == 0 and not ntype.startswith('trigger.'):
            return 'First node must be a trigger'
        if i > 0 and ntype.startswith('trigger.'):
            return 'Only the first node can be a trigger'

        config = node.get('config') or {}
        for field in NODE_TYPES[ntype].get('fields', []):
            if field.get('required') and not str(config.get(field['name'], '')).strip():
                return f'{NODE_TYPES[ntype]["label"]}: {field["label"]} is required'

    return None


# ============================================================
# Executor
# ============================================================
def _run_workflow(task_id, workflow):
    nodes = workflow['nodes']
    trigger = nodes[0]
    action_nodes = nodes[1:]

    try:
        trigger_config = trigger.get('config') or {}
        urls_raw = str(trigger_config.get('urls', '')).strip()
        items = [
            ln.strip() for ln in urls_raw.split('\n')
            if ln.strip() and not ln.strip().startswith('#')
        ]
        if not items:
            raise Exception('URL list is empty')

        task = load_task(task_id)
        task['items'] = items
        task['total_items'] = len(items)
        save_task(task_id, task)

        _log(task_id, None, 'info',
             f'Starting workflow "{workflow.get("name", "Untitled")}" — '
             f'{len(items)} item(s), {len(action_nodes)} action(s)')

        for idx, item in enumerate(items):
            if _is_cancelled(task_id):
                raise _Cancelled()

            task = load_task(task_id)
            task['current_item_index'] = idx
            task['current_item'] = item
            task['progress'] = int(100 * idx / len(items))
            task['node_statuses'] = {}
            save_task(task_id, task)

            _log(task_id, None, 'info',
                 f'▶ Item {idx + 1}/{len(items)}: {item[:100]}')

            ctx = {
                'item': item,
                'url': item,
                'index': idx + 1,
                'total': len(items),
                'quality': trigger_config.get('quality', 'best'),
            }

            for node in action_nodes:
                if _is_cancelled(task_id):
                    raise _Cancelled()

                node_id = node['id']
                node_type = node['type']
                node_config = node.get('config') or {}
                meta = NODE_TYPES[node_type]

                task = load_task(task_id)
                task['current_node_id'] = node_id
                task['node_statuses'][node_id] = {'status': 'running', 'progress': 0}
                save_task(task_id, task)

                _log(task_id, node_id, 'info', f'Starting {meta["label"]}')

                handler = NODE_HANDLERS.get(node_type)
                if not handler:
                    raise Exception(f'No handler for {node_type}')

                try:
                    result = handler(task_id, node_id, node_config, ctx)
                    if result:
                        ctx = result
                except _Cancelled:
                    raise
                except Exception as e:
                    logger.exception(f'Node {node_id} ({node_type}) failed')
                    raise Exception(f'{meta["label"]}: {e}')

                _update_node_progress(task_id, node_id, 100, 'done')

        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            save_task(task_id, task)

        _log(task_id, None, 'success',
             f'✅ All {len(items)} item(s) processed successfully')

    except _Cancelled:
        task = load_task(task_id)
        if task:
            task['status'] = 'cancelled'
            save_task(task_id, task)
        _log(task_id, None, 'warning', '⛔ Cancelled')
    except Exception as e:
        logger.exception(f'Workflow {task_id} failed')
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        _log(task_id, None, 'error', f'❌ {e}')


# ============================================================
# Routes
# ============================================================
def register_routes(app):

    @app.route('/workflow/node-types')
    def workflow_node_types():
        return jsonify(NODE_TYPES)

    @app.route('/workflow/list')
    def workflow_list():
        return jsonify(_list_workflows())

    @app.route('/workflow/get/<wf_id>')
    def workflow_get(wf_id):
        wf = _load_workflow(wf_id)
        if not wf:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(wf)

    @app.route('/workflow/save', methods=['POST'])
    def workflow_save_route():
        data = request.get_json(silent=True) or {}
        err = _validate_workflow(data)
        if err:
            return jsonify({'error': err}), 400
        wf_id = _save_workflow(data)
        if not wf_id:
            return jsonify({'error': 'Failed to save workflow'}), 500
        return jsonify({'id': wf_id})

    @app.route('/workflow/delete/<wf_id>', methods=['DELETE'])
    def workflow_delete(wf_id):
        path = _wf_path(wf_id)
        if not os.path.exists(path):
            return jsonify({'error': 'Not found'}), 404
        try:
            os.remove(path)
            return jsonify({'status': 'deleted'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/workflow/run', methods=['POST'])
    def workflow_run():
        data = request.get_json(silent=True) or {}
        err = _validate_workflow(data)
        if err:
            return jsonify({'error': err}), 400

        nodes = data['nodes']
        # Quick sanity: at least one action node
        if len(nodes) < 2:
            return jsonify({'error': 'Add at least one action node'}), 400

        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id,
            'status': 'running',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
            'workflow_name': data.get('name', 'Untitled Workflow'),
            'items': [],
            'total_items': 0,
            'current_item_index': 0,
            'current_item': '',
            'current_node_id': None,
            'node_statuses': {},
            'logs': [],
            'type': 'workflow',
        })

        threading.Thread(
            target=_run_workflow,
            args=(task_id, data),
            daemon=True,
        ).start()

        return jsonify({'task_id': task_id})
