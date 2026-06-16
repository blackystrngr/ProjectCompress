import os
import json
import threading
import logging
from config import TASKS_DIR

logger = logging.getLogger(__name__)

def save_task(task_id, task_data):
    """Write task JSON directly (atomic enough with lock)."""
    with threading.Lock():
        os.makedirs(TASKS_DIR, exist_ok=True)
        path = os.path.join(TASKS_DIR, f"{task_id}.json")
        try:
            with open(path, 'w') as f:
                json.dump(task_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            logger.debug(f"Task {task_id} saved")
        except Exception as e:
            logger.error(f"Save error for {task_id}: {e}")
            # Do not re-raise to avoid breaking the request
            # The task will be missing, but at least the app continues

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

