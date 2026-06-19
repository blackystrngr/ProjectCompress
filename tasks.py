import os
import json
import threading
import logging
from config import TASKS_DIR

logger = logging.getLogger(__name__)

def save_task(task_id, task_data):
    """Atomically save task JSON using a temporary file."""
    with threading.Lock():
        os.makedirs(TASKS_DIR, exist_ok=True)
        path = os.path.join(TASKS_DIR, f"{task_id}.json")
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(task_data, f, indent=2)
            os.replace(tmp, path)  # atomic on Unix
            logger.debug(f"Task {task_id} saved")
        except Exception as e:
            logger.error(f"Save error for {task_id}: {e}")
            # If replace fails, the temp file is left; we ignore.
            # Next save will overwrite.

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
