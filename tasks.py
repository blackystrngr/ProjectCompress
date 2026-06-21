import os
import json
import threading
import logging
import queue
from config import TASKS_DIR

logger = logging.getLogger(__name__)

# ---------- Lock for file saves ----------
_save_lock = threading.Lock()

# ---------- SSE broadcaster ----------
_subscribers = []
_subscribers_lock = threading.Lock()


def get_active_tasks():
    """
    Return the list of active (non‑terminal) tasks, ready for JSON serialization.
    Used for SSE initial snapshot and broadcasting.
    """
    active = []
    terminal_statuses = {'done', 'error', 'cancelled', 'search_done', 'scan_done'}
    for tid in get_all_task_ids():
        task = load_task(tid)
        if task and task.get('status') not in terminal_statuses:
            # Remove process_pid to avoid sending internal data
            safe = {k: v for k, v in task.items() if k not in ['process_pid']}
            active.append(safe)
    return active


def add_subscriber(q):
    """Add a new SSE client queue to the broadcast list."""
    with _subscribers_lock:
        _subscribers.append(q)


def remove_subscriber(q):
    """Remove an SSE client queue (disconnect)."""
    with _subscribers_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def broadcast_active_tasks():
    """
    Push the current active task list to every connected SSE client.
    Called after every successful save_task().
    """
    data = json.dumps(get_active_tasks())
    with _subscribers_lock:
        # Iterate over a copy to avoid holding the lock while putting data
        for q in list(_subscribers):
            try:
                q.put_nowait(data)
            except queue.Full:
                # Client not consuming fast enough → disconnect
                remove_subscriber(q)


# ---------- Task file I/O ----------
def save_task(task_id, task_data):
    """Atomically save task JSON and broadcast update on success."""
    success = False
    with _save_lock:
        os.makedirs(TASKS_DIR, exist_ok=True)
        path = os.path.join(TASKS_DIR, f"{task_id}.json")
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(task_data, f, indent=2)
            os.replace(tmp, path)          # atomic on Unix
            logger.debug(f"Task {task_id} saved")
            success = True
        except Exception as e:
            logger.error(f"Save error for {task_id}: {e}")
            # If replace fails, the temp file is left; we ignore.
            # Next save will overwrite.
    if success:
        # Push update to all SSE clients
        broadcast_active_tasks()


def load_task(task_id):
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
        return task
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Corrupted task {task_id} – removing: {e}")
        try:
            os.remove(path)
        except:
            pass
        return None


def get_all_task_ids():
    try:
        os.makedirs(TASKS_DIR, exist_ok=True)
        return [f[:-5] for f in os.listdir(TASKS_DIR) if f.endswith('.json')]
    except Exception:
        return []
