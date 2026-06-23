import os
import json
import time
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

# Statuses that represent search/scan *results* rather than a download in
# progress. These never belong in the live task pane.
_HIDDEN_STATUSES = {'search_done', 'scan_done'}

# Statuses that mean "finished". We keep these visible for a little while
# after they finish (instead of deleting them from the list instantly) so a
# page refresh right after completion still shows the result/download link
# and a finished/failed task doesn't just vanish before the user notices.
_TERMINAL_STATUSES = {'done', 'error', 'cancelled'}
TERMINAL_RETENTION_SECONDS = 600  # keep finished tasks visible for 10 minutes


def get_active_tasks():
    """
    Return the list of tasks worth showing in the task pane right now:
    anything still in progress, plus anything that finished/failed/was
    cancelled within the last TERMINAL_RETENTION_SECONDS. Used for the
    SSE initial snapshot, SSE broadcasts, and /get_tasks.
    """
    visible = []
    now = time.time()
    for tid in get_all_task_ids():
        task = load_task(tid)
        if not task:
            continue
        status = task.get('status')
        if status in _HIDDEN_STATUSES:
            continue
        if status in _TERMINAL_STATUSES:
            path = os.path.join(TASKS_DIR, f"{tid}.json")
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                age = 0
            if age > TERMINAL_RETENTION_SECONDS:
                continue
        # Remove process_pid to avoid sending internal data
        safe = {k: v for k, v in task.items() if k not in ['process_pid']}
        visible.append(safe)
    return visible


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
                # The client hasn't consumed the previous snapshot yet.
                # The queue only ever needs to hold the *latest* state, so
                # drop the stale item and put the fresh one in its place.
                # IMPORTANT: do not remove the subscriber here - it is
                # still connected and listening, it's just a bit behind.
                # Disconnecting it would silently stop all future updates
                # to that browser tab even though the EventSource never
                # errors, which makes the UI look "frozen"/"stopped".
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # Extremely unlikely race; just skip this round, the
                    # next broadcast will catch the subscriber up.
                    pass


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
