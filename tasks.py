import os
import json
import time
import threading
import logging
import queue
from config import TASKS_DIR

logger = logging.getLogger(__name__)

# ---------- Locks ----------
_save_lock = threading.Lock()
_task_cache_lock = threading.Lock()

# ---------- In-memory task cache ----------
_task_cache = {}     # task_id -> task dict

# ---------- SSE broadcaster ----------
_subscribers = []
_subscribers_lock = threading.Lock()

# ---------- Broadcast coalescing ----------
_broadcast_timer = None
_broadcast_timer_lock = threading.Lock()

# Statuses hidden from the visible task list
_HIDDEN_STATUSES = {'search_done', 'scan_done'}

# Terminal statuses kept visible briefly
_TERMINAL_STATUSES = {'done', 'error', 'cancelled'}
TERMINAL_RETENTION_SECONDS = 600

# Fields the browser task panel never needs — excluded from broadcasts
# to keep SSE payloads small (~2 KB instead of ~150 KB per task).
_HEAVY_FIELDS = {
    'workflow_snapshot',   # node graph
    'items',               # playlist URLs
    'checkpoints',         # per-item progress snapshots (grows O(n))
    'context_snapshots',   # ctx at each node
    'node_timings',        # per-node durations
    'logs',                # workflow logs (up to 500 entries)
    'candidates',          # URL cracker candidates
    'process_pid',         # internal
}


# ============================================================
# Load / Save with in-memory cache
# ============================================================
def load_task(task_id):
    """Return task dict (from cache if possible, else from disk)."""
    with _task_cache_lock:
        if task_id in _task_cache:
            return dict(_task_cache[task_id])

    path = os.path.join(TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            task = json.load(f)
        if not isinstance(task, dict) or 'task_id' not in task:
            logger.error(f"Task {task_id} missing 'task_id' – removing")
            os.remove(path)
            return None
        with _task_cache_lock:
            _task_cache[task_id] = task
        return dict(task)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Corrupted task {task_id} – removing: {e}")
        try:
            os.remove(path)
        except Exception:
            pass
        return None


def save_task(task_id, task_data):
    """Save task to cache + disk, then schedule a coalesced broadcast."""
    with _task_cache_lock:
        _task_cache[task_id] = dict(task_data)

    success = False
    with _save_lock:
        os.makedirs(TASKS_DIR, exist_ok=True)
        path = os.path.join(TASKS_DIR, f"{task_id}.json")
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(task_data, f, separators=(',', ':'))  # compact
            os.replace(tmp, path)
            success = True
        except Exception as e:
            logger.error(f"Save error for {task_id}: {e}")

    if success:
        _schedule_broadcast()


# ============================================================
# Active tasks (from cache — NO disk reads)
# ============================================================
def get_active_tasks():
    """Build the visible task list from the in-memory cache."""
    now = time.time()
    visible = []
    with _task_cache_lock:
        for tid, task in _task_cache.items():
            status = task.get('status')
            if status in _HIDDEN_STATUSES:
                continue
            if status in _TERMINAL_STATUSES:
                created = task.get('created_at', now)
                if now - created > TERMINAL_RETENTION_SECONDS:
                    continue
            # Strip heavy fields → ~2 KB payload instead of ~150 KB
            safe = {k: v for k, v in task.items() if k not in _HEAVY_FIELDS}
            visible.append(safe)
    return visible


# ============================================================
# Coalesced broadcast
# ============================================================
def _schedule_broadcast():
    """Schedule a broadcast at most once per 300ms."""
    global _broadcast_timer
    with _broadcast_timer_lock:
        if _broadcast_timer is not None:
            return  # already scheduled
        _broadcast_timer = threading.Timer(0.3, _do_scheduled_broadcast)
        _broadcast_timer.daemon = True
        _broadcast_timer.start()


def _do_scheduled_broadcast():
    global _broadcast_timer
    with _broadcast_timer_lock:
        _broadcast_timer = None
    try:
        _broadcast_from_cache()
    except Exception as e:
        logger.warning(f"Coalesced broadcast failed: {e}")


def _broadcast_from_cache():
    """Push current task snapshot to all SSE subscribers."""
    try:
        data = json.dumps(get_active_tasks(), separators=(',', ':'))
    except Exception as e:
        logger.warning(f"Failed to serialize tasks: {e}")
        return

    with _subscribers_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass


def broadcast_active_tasks():
    """Public alias — used by route handlers."""
    _broadcast_from_cache()


# ============================================================
# SSE subscribers
# ============================================================
def add_subscriber(q):
    with _subscribers_lock:
        _subscribers.append(q)


def remove_subscriber(q):
    with _subscribers_lock:
        if q in _subscribers:
            _subscribers.remove(q)


# ============================================================
# Task IDs / cleanup
# ============================================================
def get_all_task_ids():
    try:
        os.makedirs(TASKS_DIR, exist_ok=True)
        return [f[:-5] for f in os.listdir(TASKS_DIR) if f.endswith('.json')]
    except Exception:
        return []


def cleanup_old_tasks(max_age_seconds=86400):
    """Delete terminal task files older than max_age_seconds."""
    now = time.time()
    cleaned = 0
    with _task_cache_lock:
        cached_ids = set(_task_cache.keys())

    for tid in get_all_task_ids():
        path = os.path.join(TASKS_DIR, f"{tid}.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue

        if tid not in cached_ids:
            task = load_task(tid)
            if not task:
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
            status = task.get('status')
        else:
            task = load_task(tid)
            status = task.get('status') if task else None

        if status in _TERMINAL_STATUSES and (now - mtime) > max_age_seconds:
            try:
                os.remove(path)
                with _task_cache_lock:
                    _task_cache.pop(tid, None)
                cleaned += 1
            except Exception:
                pass

    if cleaned:
        logger.info(f"Cleaned up {cleaned} old task files")


def delete_task(task_id):
    """Delete a task from cache + disk."""
    with _task_cache_lock:
        _task_cache.pop(task_id, None)
    path = os.path.join(TASKS_DIR, f"{task_id}.json")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
