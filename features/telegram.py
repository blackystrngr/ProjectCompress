import os
import json
import uuid
import asyncio
import threading
import time
import logging
from concurrent.futures import CancelledError as FuturesCancelled
from flask import request, jsonify
from telethon import TelegramClient, errors
from telethon.tl.types import DocumentAttributeVideo
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, TELEGRAM_SESSION_FILE, TELEGRAM_CREDS_FILE, TELEGRAM_PROXY

logger = logging.getLogger(__name__)

# How many Telegram jobs (scan / download / upload) may run at once.
# 1 = old strict serialisation. Keep small to avoid FloodWait.
TG_MAX_CONCURRENT_JOBS = 3
# Attempts for scan / download steps that hit a dropped connection.
TG_RETRY_ATTEMPTS = 3

# ============================================================
# GLOBAL EVENT LOOP (single, persistent) + LOCK
# ============================================================
_tg_loop = None
_tg_loop_lock = threading.Lock()
_tg_job_slots = threading.BoundedSemaphore(TG_MAX_CONCURRENT_JOBS)


def get_tg_loop():
    global _tg_loop
    with _tg_loop_lock:
        if _tg_loop is None or _tg_loop.is_closed():
            _tg_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_tg_loop)
            logger.info("Created global Telegram event loop")
        return _tg_loop


def run_in_tg_loop(coro):
    loop = get_tg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def start_tg_loop_if_needed():
    global _tg_loop
    with _tg_loop_lock:
        if _tg_loop is None or _tg_loop.is_closed():
            _tg_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_tg_loop)
            t = threading.Thread(target=_tg_loop.run_forever, daemon=True, name="tg-eventloop")
            t.start()
            logger.info("Started global Telegram event loop thread")


# ============================================================
# PERSISTENT CLIENT — one per process, shared by ops + listener
# ============================================================
_persistent_client = None
_async_init_lock = None
_client_ready = threading.Event()


async def _ensure_client_async():
    """
    Return the persistent Telegram client, creating it once.
    Must be awaited on the event loop (i.e. inside run_in_tg_loop).
    """
    global _persistent_client, _async_init_lock

    if _persistent_client is not None and _persistent_client.is_connected():
        return _persistent_client

    if _async_init_lock is None:
        _async_init_lock = asyncio.Lock()

    async with _async_init_lock:
        if _persistent_client is not None:
            if _persistent_client.is_connected():
                return _persistent_client
            try:
                await _persistent_client.connect()
                if _persistent_client.is_connected():
                    logger.info("Persistent Telegram client reconnected")
                    return _persistent_client
            except Exception as e:
                logger.warning(f"Reconnect failed: {e} — rebuilding client")
                try:
                    await _persistent_client.disconnect()
                except Exception:
                    pass
                _persistent_client = None

        if not os.path.exists(TELEGRAM_CREDS_FILE):
            raise FileNotFoundError(
                f"{TELEGRAM_CREDS_FILE} not found. Create it with "
                '{"api_id": ..., "api_hash": ...}'
            )
        with open(TELEGRAM_CREDS_FILE) as f:
            creds = json.load(f)
        api_id = int(creds['api_id'])
        api_hash = creds['api_hash']

        common_kwargs = {
            'timeout': 120,
            'request_retries': 8,
            'connection_retries': 10,   # survive short network drops
            'retry_delay': 2,
            'auto_reconnect': True,
        }
        if TELEGRAM_PROXY:
            client = TelegramClient(
                TELEGRAM_SESSION_FILE, api_id, api_hash,
                proxy=TELEGRAM_PROXY, **common_kwargs
            )
        else:
            client = TelegramClient(
                TELEGRAM_SESSION_FILE, api_id, api_hash, **common_kwargs
            )
        await client.start()
        _persistent_client = client
        _client_ready.set()
        logger.info("Persistent Telegram client ready")
        return client


def ensure_client():
    """Sync helper — blocks until the persistent client is ready."""
    start_tg_loop_if_needed()
    return run_in_tg_loop(_ensure_client_async())


def get_persistent_client():
    """Public getter used by telegram_listener."""
    return _persistent_client


# ============================================================
# LEGACY COMPAT (pre-persistent-client API)
# ------------------------------------------------------------
# Older code created one TelegramClient per task and cancelled by
# disconnecting it. Cancellation now goes through _current_ops
# (see cancel_telegram_task), so these only keep bookkeeping and
# NEVER disconnect the shared client.
# ============================================================
_running_clients = {}      # task_id -> TelegramClient (informational only)
_running_lock = threading.Lock()


def register_client(task_id, client):
    with _running_lock:
        _running_clients[task_id] = client


def unregister_client(task_id):
    with _running_lock:
        _running_clients.pop(task_id, None)


async def get_telegram_client():
    """
    Legacy alias — returns the shared persistent client.
    Callers must NOT call client.disconnect() on it.
    """
    return await _ensure_client_async()


# ============================================================
# CURRENT OPS REGISTRY (cancellation without disconnecting)
# ============================================================
_current_ops = {}      # task_id -> asyncio.Task
_current_ops_lock = threading.Lock()


def _register_current_op(task_id):
    atask = asyncio.current_task()
    with _current_ops_lock:
        _current_ops[task_id] = atask


def _unregister_op(task_id):
    with _current_ops_lock:
        _current_ops.pop(task_id, None)


def cancel_telegram_task(task_id):
    """
    Cancel the running Telegram operation for a task.

    The persistent client stays connected — the listener and any other
    operations continue to run.
    """
    with _current_ops_lock:
        atask = _current_ops.get(task_id)

    if atask is None or atask.done():
        return False

    try:
        loop = get_tg_loop()
        loop.call_soon_threadsafe(atask.cancel)
        logger.info(f"Cancelling Telegram operation for task {task_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to cancel Telegram op {task_id}: {e}")
        return False


# ============================================================
# HELPERS
# ============================================================
def get_telegram_creds():
    if os.path.exists(TELEGRAM_CREDS_FILE):
        with open(TELEGRAM_CREDS_FILE, 'r') as f:
            creds = json.load(f)
        return creds['api_id'], creds['api_hash']
    raise Exception(
        f"Telegram credentials not found at {TELEGRAM_CREDS_FILE}. "
        "Create the file with {\"api_id\": ..., \"api_hash\": ...}"
    )


def _humanize_telegram_error(e):
    msg = str(e) or type(e).__name__
    low = msg.lower()
    if 'unsuccessful' in low or 'internal issues' in low:
        return ("Telegram's servers had a transient issue saving this file. "
                "This is usually temporary — retry in a few minutes.")
    if 'flood' in low or 'too many requests' in low:
        return f"Rate limited by Telegram. Wait a bit before retrying. ({msg})"
    if 'file is too big' in low or 'file too large' in low:
        return f"File exceeds Telegram's size limit. ({msg})"
    if 'chat_write_forbidden' in low or 'not enough rights' in low:
        return ("The bot/session can't post in that chat. Check that the bot "
                f"is an admin with 'Post messages' permission. ({msg})")
    if 'cannot find any entity' in low or 'chat not found' in low:
        return ("Chat not found. Check the link/ID — bot mode needs the numeric "
                f"-100… ID for private channels. ({msg})")
    if 'user is banned' in low or 'not a member' in low:
        return f"The bot/user is banned or not a member of that chat. ({msg})"
    return msg


_PERMANENT_ERROR_MARKERS = (
    'file is too big', 'file too large',
    'chat_write_forbidden', 'not enough rights',
    'cannot find any entity', 'chat not found',
    'user is banned', 'not a member',
    'message too long', 'peer_id_invalid',
)


class TelegramCancelled(Exception):
    pass


def _is_transient(e):
    """True for dropped-connection style errors worth retrying."""
    low = str(e).lower()
    if any(m in low for m in _PERMANENT_ERROR_MARKERS):
        return False
    if isinstance(e, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return False
    return isinstance(e, (ConnectionError, TimeoutError, asyncio.TimeoutError,
                          OSError, EOFError, errors.RPCError))


async def _retry_async(task_id, label, call, cleanup=None,
                       attempts=TG_RETRY_ATTEMPTS, base_delay=3.0):
    """
    Run `await call(client)`; on a transient failure wait, re-acquire the
    (possibly rebuilt) persistent client and try again.
    """
    delay = base_delay
    client = await _ensure_client_async()
    for attempt in range(1, attempts + 1):
        t = load_task(task_id)
        if t and t.get('cancelled', False):
            raise TelegramCancelled("Cancelled by user")
        try:
            return await call(client)
        except (asyncio.CancelledError, TelegramCancelled):
            raise
        except Exception as e:
            if attempt == attempts or not _is_transient(e):
                raise
            logger.warning(
                f"Telegram {label} attempt {attempt}/{attempts} failed for "
                f"task {task_id}: {type(e).__name__}: {e} — retrying in {delay:.0f}s"
            )
            if cleanup:
                try:
                    cleanup()
                except Exception:
                    pass
            await asyncio.sleep(delay)
            delay *= 2
            client = await _ensure_client_async()


def _mark_wrapper_failure(task_id, e):
    """Common handler for exceptions escaping a sync job wrapper."""
    t = load_task(task_id)
    if not t:
        return
    if (isinstance(e, (FuturesCancelled, asyncio.CancelledError, TelegramCancelled))
            or t.get('cancelled', False)):
        if t.get('status') not in ('done', 'cancelled'):
            t['status'] = 'cancelled'
            t['error_msg'] = 'Cancelled by user'
            save_task(task_id, t)
        return
    if t.get('status') in ('done', 'cancelled'):
        return
    t['status'] = 'error'
    t['error_msg'] = _humanize_telegram_error(e)
    save_task(task_id, t)


# ============================================================
# THROTTLED PROGRESS SAVER
# ============================================================
class ProgressThrottle:
    """Save task state at most every `interval` seconds."""
    def __init__(self, interval=1.0):
        self.interval = interval
        self.last = 0

    def should_save(self):
        now = time.time()
        if now - self.last >= self.interval:
            self.last = now
            return True
        return False


# ============================================================
# SCAN CHAT
# ============================================================
async def scan_chat_async(task_id, chat_link, limit):
    await _ensure_client_async()
    _register_current_op(task_id)
    try:
        async def _scan(client):
            entity = await client.get_entity(chat_link)
            videos = []
            was_cancelled = False
            async for msg in client.iter_messages(entity, limit=limit):
                task = load_task(task_id)
                if task and task.get('cancelled', False):
                    was_cancelled = True
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
            return videos, was_cancelled

        videos, was_cancelled = await _retry_async(task_id, 'scan', _scan)

        task = load_task(task_id)
        if task:
            task['videos'] = videos
            if was_cancelled or task.get('cancelled', False):
                task['status'] = 'cancelled'
                task['error_msg'] = 'Cancelled by user'
            else:
                task['status'] = 'scan_done'
            save_task(task_id, task)
    except (asyncio.CancelledError, TelegramCancelled):
        # Handled here (not re-raised) so the sync wrapper sees a clean finish
        logger.info(f"Scan {task_id} cancelled")
        task = load_task(task_id)
        if task:
            task['status'] = 'cancelled'
            task['error_msg'] = 'Cancelled by user'
            save_task(task_id, task)
    except Exception as e:
        logger.exception(f"Scan chat error for task {task_id}")
        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'error'
            task['error_msg'] = _humanize_telegram_error(e)
            save_task(task_id, task)
    finally:
        _unregister_op(task_id)


# ============================================================
# DOWNLOAD SELECTED
# ============================================================
async def download_selected_async(download_task_id, chat_link, message_ids):
    client = await _ensure_client_async()
    _register_current_op(download_task_id)
    throttle = ProgressThrottle(1.0)

    try:
        entity = await client.get_entity(chat_link)
        total = len(message_ids)

        for idx, msg_id in enumerate(message_ids, 1):
            task = load_task(download_task_id)
            if task and task.get('cancelled', False):
                raise TelegramCancelled("Cancelled by user")

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
                if tot and throttle.should_save():
                    pct_inner = int(100 * cur / tot)
                    overall = int(100 * ((_idx - 1) + cur / tot) / _total)
                    t = load_task(download_task_id)
                    if t:
                        t['download_progress'] = pct_inner
                        t['progress'] = overall
                        save_task(download_task_id, t)

            def _drop_partial(_p=temp_path):
                if os.path.exists(_p):
                    os.remove(_p)

            await _retry_async(
                download_task_id, f'download msg {msg_id}',
                lambda c, _m=message, _p=temp_path, _cb=progress_cb:
                    c.download_media(_m, file=_p, progress_callback=_cb),
                cleanup=_drop_partial,
            )

            t = load_task(download_task_id)
            if t and t.get('cancelled', False):
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise TelegramCancelled("Cancelled by user")

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

    except (asyncio.CancelledError, TelegramCancelled):
        logger.info(f"Download {download_task_id} cancelled")
        task = load_task(download_task_id)
        if task:
            task['status'] = 'cancelled'
            save_task(download_task_id, task)
    except Exception as e:
        logger.exception(f"Download selected error for task {download_task_id}")
        task = load_task(download_task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'error'
            task['error_msg'] = _humanize_telegram_error(e)
            save_task(download_task_id, task)
    finally:
        _unregister_op(download_task_id)


# ============================================================
# SEND FILE (with cancellation + throttled progress + retries)
# ============================================================
async def send_file_to_telegram_async(task_id, chat_link, file_path, original_filename, caption=None):
    client = await _ensure_client_async()
    _register_current_op(task_id)
    throttle = ProgressThrottle(1.0)   # save at most once per second

    try:
        entity = await client.get_entity(chat_link)
        task = load_task(task_id)
        if task:
            task['status'] = 'sending'
            task['upload_progress'] = 0
            task['error_msg'] = None
            save_task(task_id, task)

        def progress_cb(current, total):
            if not total:
                return
            # Check cancellation on every callback
            t = load_task(task_id)
            if t and t.get('cancelled', False):
                # Raise a CancelledError to interrupt Telethon
                raise asyncio.CancelledError("Cancelled by user")

            if throttle.should_save():
                pct = int(100 * current / total)
                t = load_task(task_id)
                if t:
                    t['upload_progress'] = pct
                    save_task(task_id, t)

        MAX_ATTEMPTS = 4
        retry_delay = 3.0
        last_err = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            t = load_task(task_id)
            if t and t.get('cancelled', False):
                raise TelegramCancelled("Cancelled by user")

            try:
                await client.send_file(
                    entity, file_path,
                    progress_callback=progress_cb,
                    caption=caption or f"📁 {original_filename}",
                    force_document=True,
                    part_size_kb=256,
                )
                last_err = None
                if attempt > 1:
                    logger.info(
                        f"Telegram send succeeded for task {task_id} "
                        f"on attempt {attempt}/{MAX_ATTEMPTS}"
                    )
                break
            except (asyncio.CancelledError, TelegramCancelled):
                raise
            except (ValueError, errors.RPCError, ConnectionError, TimeoutError,
                    asyncio.TimeoutError, OSError, EOFError) as e:
                if isinstance(e, (FileNotFoundError, PermissionError, IsADirectoryError)):
                    raise
                last_err = e
                err_low = str(e).lower()
                if 'cancelled' in err_low:
                    raise TelegramCancelled("Cancelled by user")
                if any(m in err_low for m in _PERMANENT_ERROR_MARKERS):
                    logger.error(
                        f"Telegram send permanent failure for task {task_id}: {e}"
                    )
                    raise
                if attempt == MAX_ATTEMPTS:
                    logger.error(
                        f"Telegram send failed for task {task_id} after "
                        f"{MAX_ATTEMPTS} outer attempts: {e}"
                    )
                    raise
                logger.warning(
                    f"Telegram send attempt {attempt}/{MAX_ATTEMPTS} "
                    f"failed for task {task_id}: {type(e).__name__}: {e}"
                )
                t = load_task(task_id)
                if t:
                    t['upload_progress'] = 0
                    t['error_msg'] = (
                        f"Retrying upload (attempt {attempt + 1}/{MAX_ATTEMPTS}) "
                        f"in {int(retry_delay)}s…"
                    )
                    save_task(task_id, t)
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
                client = await _ensure_client_async()   # may have been rebuilt

        if last_err is not None:
            raise last_err

        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'done'
            task['upload_progress'] = 100
            task['error_msg'] = None
            save_task(task_id, task)

    except (asyncio.CancelledError, TelegramCancelled):
        logger.info(f"Send file task {task_id} cancelled")
        task = load_task(task_id)
        if task:
            task['status'] = 'cancelled'
            task['error_msg'] = 'Cancelled by user'
            save_task(task_id, task)
    except errors.RPCError as e:
        # A cancel that surfaces as an RPC error is still a cancel
        task = load_task(task_id)
        if task and task.get('cancelled', False):
            logger.info(f"Send file task {task_id} aborted via cancel")
            task['status'] = 'cancelled'
            save_task(task_id, task)
        else:
            logger.exception(f"Telegram RPC error for {task_id}: {e}")
            task = load_task(task_id)
            if task:
                task['status'] = 'error'
                task['error_msg'] = _humanize_telegram_error(e)
                save_task(task_id, task)
    except Exception as e:
        logger.exception(f"Send file error for task {task_id}")
        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'error'
            task['error_msg'] = _humanize_telegram_error(e)
            save_task(task_id, task)
    finally:
        _unregister_op(task_id)


# ============================================================
# SYNC WRAPPERS
# ============================================================
def _run_telegram_job(coro_func, *args, **kwargs):
    """Run a Telegram coroutine; up to TG_MAX_CONCURRENT_JOBS run in parallel."""
    start_tg_loop_if_needed()
    with _tg_job_slots:
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
    start_tg_loop_if_needed()

    # Warm the client in the background so the first user op is instant
    def _warmup():
        try:
            ensure_client()
            logger.info("Telegram client warmed up")
        except Exception as e:
            logger.warning(f"Telegram warmup failed: {e}")

    threading.Timer(2.0, _warmup).start()

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
            except (Exception, FuturesCancelled) as e:
                logger.exception(f"scan_chat wrapper error: {e!r}")
                _mark_wrapper_failure(task_id, e)

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
            except (Exception, FuturesCancelled) as e:
                logger.exception(f"download_selected wrapper error: {e!r}")
                _mark_wrapper_failure(download_task_id, e)

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
            except (Exception, FuturesCancelled) as e:
                logger.exception(f"send_file wrapper error: {e!r}")
                _mark_wrapper_failure(task_id, e)

        threading.Thread(target=run_send, daemon=True).start()
        return jsonify({'task_id': task_id})
