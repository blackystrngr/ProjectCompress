import os
import json
import uuid
import asyncio
import threading
import time
import logging
from flask import request, jsonify
from telethon import TelegramClient, errors
from telethon.tl.types import DocumentAttributeVideo
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, TELEGRAM_SESSION_FILE, TELEGRAM_CREDS_FILE, TELEGRAM_PROXY

logger = logging.getLogger(__name__)

# ============================================================
# GLOBAL EVENT LOOP (single, persistent) + LOCK
# ============================================================
_tg_loop = None
_tg_loop_lock = threading.Lock()
_tg_operation_lock = threading.Lock()   # only one telegram op at a time


def get_tg_loop():
    """Return the single global asyncio loop, creating it once."""
    global _tg_loop
    with _tg_loop_lock:
        if _tg_loop is None or _tg_loop.is_closed():
            _tg_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_tg_loop)
            logger.info("Created global Telegram event loop")
        return _tg_loop


def run_in_tg_loop(coro):
    """Run a coroutine on the global loop from a worker thread."""
    loop = get_tg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()   # blocks until done, propagates exceptions


def start_tg_loop_if_needed():
    """Ensure the loop is running in a dedicated background thread."""
    global _tg_loop
    with _tg_loop_lock:
        if _tg_loop is None or _tg_loop.is_closed():
            _tg_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_tg_loop)
            t = threading.Thread(target=_tg_loop.run_forever, daemon=True, name="tg-eventloop")
            t.start()
            logger.info("Started global Telegram event loop thread")


# ============================================================
# TELEGRAM HELPERS
# ============================================================
def get_telegram_creds():
    if os.path.exists(TELEGRAM_CREDS_FILE):
        with open(TELEGRAM_CREDS_FILE, 'r') as f:
            creds = json.load(f)
        return creds['api_id'], creds['api_hash']
    else:
        # DO NOT block on input() on a headless server
        raise Exception(
            f"Telegram credentials not found at {TELEGRAM_CREDS_FILE}. "
            "Please create the file with {\"api_id\": ..., \"api_hash\": ...}"
        )


async def get_telegram_client():
    api_id, api_hash = get_telegram_creds()
    if TELEGRAM_PROXY:
        client = TelegramClient(TELEGRAM_SESSION_FILE, int(api_id), api_hash, proxy=TELEGRAM_PROXY)
    else:
        client = TelegramClient(TELEGRAM_SESSION_FILE, int(api_id), api_hash)
    await client.start()
    return client


# ============================================================
# ASYNC OPERATIONS
# ============================================================
async def scan_chat_async(task_id, chat_link, limit):
    client = await get_telegram_client()
    try:
        entity = await client.get_entity(chat_link)
        videos = []
        async for msg in client.iter_messages(entity, limit=limit):
            task = load_task(task_id)
            if task and task.get('cancelled', False):
                break
            is_video = (
                msg.video
                or (msg.document and any(isinstance(a, DocumentAttributeVideo) for a in msg.document.attributes))
                or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('video/'))
            )
            if not is_video:
                continue
            file_name = getattr(msg.file, 'name', None)
            if not file_name:
                mime = msg.document.mime_type if msg.document else 'video/mp4'
                ext = mime.split('/')[-1] if '/' in mime else 'mp4'
                file_name = f"video_{msg.id}.{ext}"
            size_mb = (msg.document.size if msg.document else msg.video.size) / (1024 * 1024)
            videos.append({
                'id': msg.id,
                'file_name': file_name,
                'size_mb': round(size_mb, 1),
                'date': msg.date.isoformat() if msg.date else None,
            })
        task = load_task(task_id)
        if task:
            task['status'] = 'scan_done'
            task['videos'] = videos
            save_task(task_id, task)
    except Exception as e:
        logger.exception(f"Scan chat error for task {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def download_selected_async(download_task_id, chat_link, message_ids):
    client = await get_telegram_client()
    try:
        entity = await client.get_entity(chat_link)
        total = len(message_ids)
        for idx, msg_id in enumerate(message_ids, 1):
            task = load_task(download_task_id)
            if task and task.get('cancelled', False):
                break
            task = load_task(download_task_id)
            if task:
                task['status'] = f'downloading_{idx}'
                task['current'] = idx
                task['total'] = total
                task['progress'] = int(100 * (idx - 1) / total)
                task['download_progress'] = 0
                save_task(download_task_id, task)

            message = await client.get_messages(entity, ids=int(msg_id))
            if not message or not (message.video or message.document):
                continue

            original_name = getattr(message.file, 'name', None)
            if not original_name:
                mime = message.document.mime_type if message.document else 'video/mp4'
                ext = mime.split('/')[-1] if '/' in mime else 'mp4'
                original_name = f"video_{msg_id}.{ext}"

            final_name = _get_unique_filename(original_name)
            final_path = os.path.join(UPLOAD_FOLDER, final_name)
            temp_path = os.path.join(UPLOAD_FOLDER, f"{download_task_id}_temp_{idx}.tmp")

            def progress_cb(cur, tot, _idx=idx, _total=total):
                if tot:
                    pct_inner = int(100 * cur / tot)
                    overall = int(100 * ((_idx - 1) + cur / tot) / _total)
                    t = load_task(download_task_id)
                    if t:
                        t['download_progress'] = pct_inner
                        t['progress'] = overall
                        save_task(download_task_id, t)

            await client.download_media(message, file=temp_path, progress_callback=progress_cb)

            # Check cancellation before renaming
            t = load_task(download_task_id)
            if t and t.get('cancelled', False):
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                break

            os.rename(temp_path, final_path)
            task = load_task(download_task_id)
            if task:
                task['output_file'] = final_name
                save_task(download_task_id, task)

        task = load_task(download_task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'done'
            task['progress'] = 100
            save_task(download_task_id, task)
    except Exception as e:
        logger.exception(f"Download selected error for task {download_task_id}")
        task = load_task(download_task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(download_task_id, task)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_file_to_telegram_async(task_id, chat_link, file_path, original_filename):
    client = await get_telegram_client()
    try:
        entity = await client.get_entity(chat_link)
        task = load_task(task_id)
        if task:
            task['status'] = 'sending'
            task['upload_progress'] = 0
            save_task(task_id, task)

        def progress_cb(current, total):
            if total:
                pct = int(100 * current / total)
                t = load_task(task_id)
                if t:
                    t['upload_progress'] = pct
                    save_task(task_id, t)

        await client.send_file(
            entity, file_path,
            progress_callback=progress_cb,
            caption=f"📁 {original_filename}"
        )

        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['upload_progress'] = 100
            save_task(task_id, task)
    except Exception as e:
        logger.exception(f"Send file error for task {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ============================================================
# SYNC WRAPPERS (serialise via lock, run on global loop)
# ============================================================
def _run_telegram_job(coro_func, *args, **kwargs):
    """Serialize all Telegram operations – session file can't handle concurrency."""
    start_tg_loop_if_needed()
    with _tg_operation_lock:
        return run_in_tg_loop(coro_func(*args, **kwargs))


def _get_unique_filename(filename):
    base, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(UPLOAD_FOLDER, new_name)):
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    return new_name


# ============================================================
# ROUTES
# ============================================================
def register_routes(app):
    # Ensure the loop exists before any request arrives
    start_tg_loop_if_needed()

    @app.route('/telegram/scan_chat', methods=['POST'])
    def telegram_scan_chat():
        chat_link = request.form.get('chat_link')
        try:
            limit = int(request.form.get('limit', 200))
        except ValueError:
            limit = 200
        if not chat_link:
            return jsonify({'error': 'Chat link required'}), 400

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'scanning', 'chat_link': chat_link,
            'limit': limit, 'created_at': time.time(), 'cancelled': False,
            'progress': 0,
        }
        save_task(task_id, task_data)

        def run_scan():
            try:
                _run_telegram_job(scan_chat_async, task_id, chat_link, limit)
            except Exception as e:
                logger.exception(f"scan_chat wrapper error: {e}")
                t = load_task(task_id)
                if t:
                    t['status'] = 'error'
                    t['error_msg'] = str(e)
                    save_task(task_id, t)

        threading.Thread(target=run_scan, daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/telegram/download_selected', methods=['POST'])
    def telegram_download_selected():
        scan_task_id = request.form.get('scan_task_id')
        selected_ids = request.form.getlist('selected_ids')
        if not scan_task_id or not selected_ids:
            return jsonify({'error': 'Missing scan_task_id or selected_ids'}), 400

        parent_task = load_task(scan_task_id)
        if not parent_task:
            return jsonify({'error': 'Scan task not found'}), 404
        chat_link = parent_task.get('chat_link')

        download_task_id = str(uuid.uuid4())
        task_data = {
            'task_id': download_task_id, 'status': 'queued', 'chat_link': chat_link,
            'selected_ids': selected_ids, 'total': len(selected_ids), 'current': 0,
            'created_at': time.time(), 'cancelled': False,
            'progress': 0, 'download_progress': 0,
        }
        save_task(download_task_id, task_data)

        def run_download():
            try:
                _run_telegram_job(
                    download_selected_async,
                    download_task_id, chat_link, selected_ids
                )
            except Exception as e:
                logger.exception(f"download_selected wrapper error: {e}")
                t = load_task(download_task_id)
                if t:
                    t['status'] = 'error'
                    t['error_msg'] = str(e)
                    save_task(download_task_id, t)

        threading.Thread(target=run_download, daemon=True).start()
        return jsonify({'download_task_id': download_task_id})

    @app.route('/telegram/send_file', methods=['POST'])
    def telegram_send_file():
        file_path = request.form.get('file_path')
        filename = request.form.get('filename')
        chat_link = request.form.get('chat_link')
        if not chat_link:
            return jsonify({'error': 'Missing chat_link'}), 400
        if file_path:
            if '..' in file_path or file_path.startswith('/'):
                return jsonify({'error': 'Invalid path'}), 400
            full_path = os.path.join(UPLOAD_FOLDER, file_path)
        elif filename:
            full_path = os.path.join(UPLOAD_FOLDER, filename)
        else:
            return jsonify({'error': 'Missing file_path or filename'}), 400
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            return jsonify({'error': 'File not found'}), 404

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'upload_progress': 0,
            'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'output_file': os.path.basename(full_path),
        }
        save_task(task_id, task_data)

        def run_send():
            try:
                _run_telegram_job(
                    send_file_to_telegram_async,
                    task_id, chat_link, full_path, os.path.basename(full_path)
                )
            except Exception as e:
                logger.exception(f"send_file wrapper error: {e}")
                t = load_task(task_id)
                if t:
                    t['status'] = 'error'
                    t['error_msg'] = str(e)
                    save_task(task_id, t)

        threading.Thread(target=run_send, daemon=True).start()
        return jsonify({'task_id': task_id})
