# features/workflow.py
"""
Advanced node-based workflow engine.

Features:
  • Branching (Condition/Switch/Fork), merging (Merge), parallel branches
  • Sub-workflows (recursion-limited), user-input gates, dry run mode
  • Per-node timeout, retry with exponential backoff, error policies
  • Context snapshots, node timings, comments, import/export
  • Safe Python expressions in locked namespace
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
import copy
from flask import request, jsonify, Response
from tasks import save_task, load_task, delete_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(PROJECT_ROOT, 'workflows')
os.makedirs(WORKFLOWS_DIR, exist_ok=True)

MAX_SUBWORKFLOW_DEPTH = 10
MAX_CONTEXT_SNAPSHOTS = 5
MAX_TIMINGS = 20


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


# ============================================================
# Common advanced fields for all nodes
# ============================================================
COMMON_ADVANCED = [
    {'name': 'retry_max',   'type': 'number', 'label': 'Retries on failure',
     'default': 0, 'help': '0 = no retry'},
    {'name': 'retry_delay', 'type': 'number', 'label': 'Base retry delay (s)',
     'default': 3},
    {'name': 'retry_backoff', 'type': 'select', 'label': 'Retry backoff',
     'options': ['fixed', 'exponential'], 'default': 'fixed',
     'help': 'exponential: delay, 2×delay, 4×delay… capped at 60s'},
    {'name': 'timeout_sec', 'type': 'number', 'label': 'Timeout (s, 0 = none)',
     'default': 0,
     'help': 'Kills the node if it runs longer. Subprocesses are terminated '
             'cleanly.'},
    {'name': 'on_error',    'type': 'select', 'label': 'On error',
     'options': ['stop', 'continue'], 'default': 'stop',
     'help': '"stop" cancels the whole item. "continue" skips to next node.'},
    {'name': 'skip_if',     'type': 'text', 'label': 'Skip if (Python expr)',
     'default': '',
     'help': 'If evaluates True → node is skipped and passes through.'},
]


# ============================================================
# Node type registry
# ============================================================
NODE_TYPES = {
    # ---------- Triggers ----------
    'trigger.url_list': {
        'label': 'URL List',
        'category': 'trigger', 'icon': 'fa-list', 'color': '#4ade80',
        'description': 'Provides a list of URLs. Each downstream action runs '
                       'once per URL.',
        'ports': {'in': [], 'out': ['out']},
        'fields': [
            {'name': 'urls', 'type': 'textarea', 'label': 'URLs (one per line)',
             'required': True,
             'placeholder': 'https://youtube.com/watch?v=...\nhttps://example.com/file.mp4',
             'help': 'Blank lines and lines starting with # are ignored.'},
            {'name': 'quality', 'type': 'select', 'label': 'Download quality',
             'options': ['best', '2160p', '1440p', '1080p', '720p', '480p', 'audio'],
             'default': '1080p'},
            {'name': 'expand_playlists', 'type': 'checkbox',
             'label': 'Expand playlists into individual videos',
             'default': True},
            {'name': 'max_items', 'type': 'number',
             'label': 'Max videos per playlist (0 = all)', 'default': 0},
        ],
        'advanced_fields': [],
    },
    'trigger.file_glob': {
        'label': 'File Glob',
        'category': 'trigger', 'icon': 'fa-folder-open', 'color': '#4ade80',
        'description': 'Files matching a glob pattern inside downloads/.',
        'ports': {'in': [], 'out': ['out']},
        'fields': [
            {'name': 'pattern', 'type': 'text', 'label': 'Glob pattern',
             'required': True, 'default': '**/*.mp4'},
            {'name': 'max_items', 'type': 'number',
             'label': 'Max files (0 = all)', 'default': 0},
        ],
        'advanced_fields': [],
    },

    # ---------- Actions ----------
    'action.download': {
        'label': 'Download Video',
        'category': 'action', 'icon': 'fa-download', 'color': '#5f8fe0',
        'description': 'Downloads the current URL with yt-dlp.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.download_file': {
        'label': 'Download File',
        'category': 'action', 'icon': 'fa-file-arrow-down', 'color': '#5f8fe0',
        'description': 'Downloads the URL as a raw file.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.telegram_send': {
        'label': 'Send to Telegram',
        'category': 'action', 'icon': 'fa-paper-plane', 'color': '#22d3ee',
        'description': 'Sends the current file to a Telegram chat.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'chat_link', 'type': 'text', 'label': 'Chat link',
             'required': True, 'placeholder': '@channel or https://t.me/...'},
            {'name': 'caption', 'type': 'text', 'label': 'Caption',
             'default': '📁 {filename}',
             'help': 'Placeholders: {filename}, {url}, {index}, {total}, '
                     '{size}, {basename}, {ext}, {vars.name}'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.drive_upload': {
        'label': 'Upload to Drive',
        'category': 'action', 'icon': 'fa-cloud-upload-alt', 'color': '#a78bfa',
        'description': 'Uploads the current file to Google Drive.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'account', 'type': 'text', 'label': 'Drive account',
             'default': 'default'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.compress': {
        'label': 'Compress Video',
        'category': 'action', 'icon': 'fa-compress', 'color': '#f59e0b',
        'description': 'Re-encodes the current video with H.265 or AV1.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'codec', 'type': 'select', 'label': 'Codec',
             'options': ['x265', 'av1'], 'default': 'x265'},
            {'name': 'resolution', 'type': 'select', 'label': 'Resolution',
             'options': ['360', '480', '720', '1080'], 'default': '720'},
            {'name': 'crf', 'type': 'number', 'label': 'CRF', 'default': 27},
            {'name': 'preset', 'type': 'text', 'label': 'Preset',
             'default': 'veryfast'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.delete_local': {
        'label': 'Delete Local File',
        'category': 'action', 'icon': 'fa-trash-alt', 'color': '#ef4444',
        'description': 'Deletes the current file from local disk.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.delete_drive': {
        'label': 'Delete Drive File',
        'category': 'action', 'icon': 'fa-trash-alt', 'color': '#ef4444',
        'description': 'Deletes the file from Drive (upload first).',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'account', 'type': 'text', 'label': 'Drive account',
             'default': 'default'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.wait': {
        'label': 'Wait',
        'category': 'action', 'icon': 'fa-clock', 'color': '#8a9bc0',
        'description': 'Pauses for N seconds.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'seconds', 'type': 'number', 'label': 'Seconds',
             'default': 5},
            {'name': 'jitter', 'type': 'number', 'label': 'Random jitter (s)',
             'default': 0},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.webhook': {
        'label': 'Webhook',
        'category': 'action', 'icon': 'fa-globe', 'color': '#4ade80',
        'description': 'Sends an HTTP request.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'url', 'type': 'text', 'label': 'URL',
             'required': True, 'placeholder': 'https://hooks.slack.com/...'},
            {'name': 'method', 'type': 'select', 'label': 'Method',
             'options': ['POST', 'GET'], 'default': 'POST'},
            {'name': 'body', 'type': 'textarea', 'label': 'Body',
             'default': '{"text": "Downloaded {filename} from {url}"}'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.extract_metadata': {
        'label': 'Extract Metadata',
        'category': 'action', 'icon': 'fa-info-circle', 'color': '#22d3ee',
        'description': 'Extracts video metadata (duration, resolution, codecs) '
                       'into {duration}, {width}, {height}, {vcodec}, {acodec}.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.sub_workflow': {
        'label': 'Run Sub-Workflow',
        'category': 'action', 'icon': 'fa-cubes', 'color': '#a78bfa',
        'description': 'Runs another saved workflow synchronously. Its final '
                       'context is returned. Depth-limited to 10.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'workflow_id', 'type': 'text', 'label': 'Workflow ID',
             'required': True,
             'help': 'Copy the ID from the workflow selector (e.g. a1b2c3d4e5f6)'},
            {'name': 'inherit_context', 'type': 'checkbox',
             'label': 'Pass parent context to sub-workflow',
             'default': True},
            {'name': 'return_context', 'type': 'checkbox',
             'label': 'Merge sub-workflow result into parent context',
             'default': True},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.user_input': {
        'label': 'Wait for Approval',
        'category': 'action', 'icon': 'fa-user-check', 'color': '#f59e0b',
        'description': 'Pauses the workflow until you approve/reject from the UI. '
                       'Routes to "approved", "rejected", or "timeout".',
        'ports': {'in': ['in'], 'out': ['approved', 'rejected', 'timeout']},
        'fields': [
            {'name': 'message', 'type': 'text', 'label': 'Prompt',
             'default': 'Approve this step?'},
            {'name': 'approval_timeout', 'type': 'number',
             'label': 'Approval timeout (s)', 'default': 3600},
        ],
        'advanced_fields': [],
    },
    'action.json_parse': {
        'label': 'JSON Parse',
        'category': 'action', 'icon': 'fa-code', 'color': '#4ade80',
        'description': 'Parses a JSON string into vars. Sets vars.<prefix>.* or '
                       'vars.<key> directly.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'source', 'type': 'text', 'label': 'Source expression',
             'required': True, 'default': 'vars.body',
             'help': 'Where to read the JSON string from. '
                     'Example: vars.body or filename'},
            {'name': 'into_var', 'type': 'text', 'label': 'Store into var',
             'default': 'parsed',
             'help': 'If the JSON is an object, its keys land in vars.<name>.*. '
                     'If it\'s an array, the array goes into vars.<name>.'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.string_ops': {
        'label': 'String Ops',
        'category': 'action', 'icon': 'fa-font', 'color': '#4ade80',
        'description': 'Manipulate a string value.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'source', 'type': 'text', 'label': 'Source expression',
             'required': True, 'default': 'filename',
             'help': 'e.g. filename, basename, vars.title, url'},
            {'name': 'operation', 'type': 'select', 'label': 'Operation',
             'options': ['lower', 'upper', 'trim', 'strip_ext', 'replace',
                         'split_take_first', 'split_take_last', 'slice',
                         'regex_replace', 'regex_extract'],
             'default': 'lower'},
            {'name': 'arg1', 'type': 'text', 'label': 'Arg 1',
             'default': '',
             'help': 'For replace: search string · For regex: pattern · '
                     'For slice: start:end'},
            {'name': 'arg2', 'type': 'text', 'label': 'Arg 2',
             'default': '',
             'help': 'For replace/regex_replace: replacement'},
            {'name': 'target', 'type': 'text', 'label': 'Store into',
             'required': True, 'default': 'vars.result',
             'help': 'Example: vars.clean_name or filename'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.assert': {
        'label': 'Assert',
        'category': 'action', 'icon': 'fa-shield-alt', 'color': '#ef4444',
        'description': 'Fails the workflow with a message if the condition is '
                       'false.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'expression', 'type': 'text', 'label': 'Must be True',
             'required': True, 'default': 'size > 0'},
            {'name': 'message', 'type': 'text', 'label': 'Error message',
             'default': 'Assertion failed: {filename}'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.http_fetch': {
        'label': 'HTTP Fetch',
        'category': 'action', 'icon': 'fa-cloud-download-alt', 'color': '#4ade80',
        'description': 'GET a URL and store the response body in vars.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'url', 'type': 'text', 'label': 'URL',
             'required': True,
             'help': 'Supports {url}, {filename}, {vars.x} placeholders'},
            {'name': 'into_var', 'type': 'text', 'label': 'Store into var',
             'default': 'body'},
            {'name': 'max_bytes', 'type': 'number', 'label': 'Max response size',
             'default': 1048576,
             'help': 'Truncates response if larger (default 1 MB)'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.log': {
        'label': 'Log Message',
        'category': 'action', 'icon': 'fa-comment', 'color': '#8a9bc0',
        'description': 'Adds a custom message to the execution log.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'level', 'type': 'select', 'label': 'Level',
             'options': ['info', 'success', 'warning', 'error'],
             'default': 'info'},
            {'name': 'message', 'type': 'text', 'label': 'Message',
             'default': 'Processing {filename}',
             'help': 'Supports {url}, {filename}, {size}, {vars.x}'},
        ],
        'advanced_fields': [],
    },

    # ---------- Logic ----------
    'logic.condition': {
        'label': 'Condition',
        'category': 'logic', 'icon': 'fa-code-branch', 'color': '#a78bfa',
        'description': 'Routes to "true" or "false" port based on an expression.',
        'ports': {'in': ['in'], 'out': ['true', 'false']},
        'fields': [
            {'name': 'expression', 'type': 'text', 'label': 'Expression',
             'required': True, 'default': 'size > 1000000',
             'help': 'Available: filename, basename, ext, url, size, index, '
                     'total, duration, width, height, vcodec, acodec, vars, re'},
        ],
        'advanced_fields': [],
    },
    'logic.switch': {
        'label': 'Switch',
        'category': 'logic', 'icon': 'fa-sitemap', 'color': '#a78bfa',
        'description': 'Routes to a matching case port, or "default".',
        'ports': {'in': ['in'], 'out': ['1', '2', '3', 'default']},
        'fields': [
            {'name': 'expression', 'type': 'text', 'label': 'Expression',
             'required': True, 'default': 'ext'},
            {'name': 'cases', 'type': 'text', 'label': 'Cases (comma-separated)',
             'default': 'mp4,mkv,webm',
             'help': 'If expression matches case N, port "N" fires. '
                     'Otherwise "default" fires.'},
        ],
        'advanced_fields': [],
    },
    'logic.merge': {
        'label': 'Merge',
        'category': 'logic', 'icon': 'fa-code-merge', 'color': '#a78bfa',
        'description': 'Joins multiple branches back into one path.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'mode', 'type': 'select', 'label': 'Mode',
             'options': ['all', 'any'], 'default': 'all',
             'help': '"all" waits for every incoming branch. '
                     '"any" fires on first arrival.'},
        ],
        'advanced_fields': [],
    },
    'logic.fork': {
        'label': 'Fork',
        'category': 'logic', 'icon': 'fa-code', 'color': '#a78bfa',
        'description': 'Fires ALL output ports in parallel branches. '
                       'Use a Merge later to re-join.',
        'ports': {'in': ['in'], 'out': ['a', 'b', 'c']},
        'fields': [],
        'advanced_fields': [],
    },
    'logic.delay_until': {
        'label': 'Delay Until',
        'category': 'logic', 'icon': 'fa-hourglass-half', 'color': '#8a9bc0',
        'description': 'Waits until a specific timestamp.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'target_time', 'type': 'text', 'label': 'Target time',
             'required': True, 'default': '',
             'placeholder': 'HH:MM or ISO timestamp',
             'help': 'HH:MM (today or tomorrow) or full ISO '
                     '(2025-12-31T23:59:00)'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },

    # ---------- Data ----------
    'data.transform': {
        'label': 'Transform',
        'category': 'data', 'icon': 'fa-wand-magic-sparkles', 'color': '#4ade80',
        'description': 'Modify a context value with a Python expression or template.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'target', 'type': 'text', 'label': 'Target key',
             'required': True, 'default': 'filename',
             'help': 'Use "vars.name" for custom variables.'},
            {'name': 'expression', 'type': 'text', 'label': 'Value',
             'required': True, 'default': 'filename.lower()',
             'help': 'Template ({filename}) → expanded. '
                     'Otherwise → evaluated as Python.'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'data.set_variable': {
        'label': 'Set Variable',
        'category': 'data', 'icon': 'fa-tag', 'color': '#4ade80',
        'description': 'Sets a custom variable accessible as {vars.name}.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'name', 'type': 'text', 'label': 'Variable name',
             'required': True, 'default': 'my_var'},
            {'name': 'value', 'type': 'text', 'label': 'Value (template)',
             'default': '{url}'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
}


# ============================================================
# Safe expression evaluation
# ============================================================
_SAFE_BUILTINS = {
    'abs': abs, 'bool': bool, 'float': float, 'int': int,
    'len': len, 'max': max, 'min': min, 'str': str, 'round': round,
    'True': True, 'False': False, 'None': None,
    'any': any, 'all': all, 'sorted': sorted, 'reversed': reversed,
    'list': list, 'dict': dict, 'tuple': tuple, 'set': set,
    'enumerate': enumerate, 'zip': zip, 'range': range,
    'sum': sum, 'isinstance': isinstance,
}


def _safe_eval(expr, ctx_dict):
    if not expr or not str(expr).strip():
        return None
    ns = dict(_SAFE_BUILTINS)
    ns.update(ctx_dict)
    ns['re'] = re
    try:
        return eval(str(expr), {'__builtins__': {}}, ns)
    except Exception as e:
        raise Exception(f"Expression error: {e}")


def _ctx_to_dict(ctx):
    return {
        'url':      ctx.get('url', ''),
        'filename': ctx.get('filename', ''),
        'basename': ctx.get('basename', ''),
        'ext':      os.path.splitext(ctx.get('filename', ''))[1].lstrip('.'),
        'size':     ctx.get('size', 0),
        'index':    ctx.get('index', 0),
        'total':    ctx.get('total', 0),
        'quality':  ctx.get('quality', 'best'),
        'duration': ctx.get('duration', 0),
        'width':    ctx.get('width', 0),
        'height':   ctx.get('height', 0),
        'vcodec':   ctx.get('vcodec', ''),
        'acodec':   ctx.get('acodec', ''),
        'vars':     ctx.get('vars', {}),
    }


def _safe_ctx_snapshot(ctx):
    """JSON-safe copy of ctx for storage."""
    def safe(v):
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v if not isinstance(v, str) or len(v) < 500 else v[:500] + '…'
        if isinstance(v, dict):
            return {str(k): safe(val) for k, val in list(v.items())[:30]}
        if isinstance(v, (list, tuple)):
            return [safe(x) for x in v[:30]]
        return str(v)[:200]

    return {str(k): safe(v) for k, v in ctx.items() if not str(k).startswith('_')}


# ============================================================
# Graph helpers
# ============================================================
def _norm_edge(e):
    if 'from_port' not in e or not e.get('from_port'):
        e['from_port'] = 'out'
    return e


def _node_ports(node_type):
    return NODE_TYPES.get(node_type, {}).get('ports', {'in': ['in'], 'out': ['out']})


def _in_edges(node_id, edges):
    return [e for e in edges if e.get('to') == node_id]


def _out_edges(node_id, edges, port=None):
    out = [e for e in edges if e.get('from') == node_id]
    if port is not None:
        out = [e for e in out if _norm_edge(e)['from_port'] == port]
    return out


def _topological_order(nodes, edges):
    ids = {n['id'] for n in nodes}
    adj = {nid: [] for nid in ids}
    indeg = {nid: 0 for nid in ids}
    for e in edges:
        f, t = e.get('from'), e.get('to')
        if f in ids and t in ids:
            adj[f].append(t)
            indeg[t] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    order = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for nxt in adj[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(nodes):
        return None
    node_map = {n['id']: n for n in nodes}
    return [node_map[nid] for nid in order]


def _migrate_workflow(wf):
    if not isinstance(wf, dict):
        return wf
    nodes = wf.get('nodes') or []
    if not nodes:
        return wf
    for i, node in enumerate(nodes):
        if 'x' not in node or not isinstance(node.get('x'), (int, float)):
            node['x'] = 80 + i * 280
        if 'y' not in node or not isinstance(node.get('y'), (int, float)):
            node['y'] = 200
    for e in (wf.get('edges') or []):
        _norm_edge(e)
    wf['edges'] = wf.get('edges') or []
    return wf


# ============================================================
# Progress / logging / abort helpers
# ============================================================
_last_save = {}
_last_save_lock = threading.Lock()


def _update_node_progress(task_id, node_id, progress, status=None):
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
    task = load_task(task_id)
    if not task:
        return
    logs = task.get('logs', [])
    logs.append({'ts': time.time(), 'node_id': node_id,
                 'level': level, 'msg': str(msg)})
    task['logs'] = logs[-500:]
    save_task(task_id, task)


def _should_abort(task_id):
    task = load_task(task_id)
    if not task:
        return False
    return bool(task.get('cancelled') or task.get('_abort'))


def _abort_reason(task_id):
    task = load_task(task_id)
    if not task:
        return None
    if task.get('cancelled'):
        return 'cancelled'
    if task.get('_abort'):
        return task.get('_abort')
    return None


def _record_timing(task_id, node_id, duration_ms):
    task = load_task(task_id)
    if not task:
        return
    timings = task.get('node_timings', [])
    timings.append({'node_id': node_id, 'duration_ms': int(duration_ms),
                    'ts': time.time()})
    task['node_timings'] = timings[-MAX_TIMINGS:]
    save_task(task_id, task)


def _record_context_snapshot(task_id, node_id, ctx):
    task = load_task(task_id)
    if not task:
        return
    snaps = task.get('context_snapshots', {})
    snaps[node_id] = {'ts': time.time(), 'ctx': _safe_ctx_snapshot(ctx)}
    if len(snaps) > MAX_CONTEXT_SNAPSHOTS:
        oldest = min(snaps.items(), key=lambda x: x[1]['ts'])[0]
        del snaps[oldest]
    task['context_snapshots'] = snaps
    save_task(task_id, task)


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
    if not text:
        return ''
    d = _ctx_to_dict(ctx)
    d['vars'] = ctx.get('vars', {}) or {}
    def repl(m):
        key = m.group(1)
        if key.startswith('vars.'):
            return str(d['vars'].get(key[5:], ''))
        if key == 'item':
            return str(ctx.get('item', ''))
        return str(d.get(key, m.group(0)))
    return re.sub(r'\{([\w.]+)\}', repl, text)


# ============================================================
# Playlist expansion
# ============================================================
def _expand_playlist(url, max_items=0):
    from features.url_download import COOKIES_FILE
    cmd = ['yt-dlp', '--flat-playlist', '--print', '%(webpage_url)s',
           '--no-warnings', '--ignore-errors', '--no-color']
    if max_items and max_items > 0:
        cmd += ['--playlist-end', str(max_items)]
    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    urls, seen = [], set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if line and line.startswith(('http://', 'https://')) and line not in seen:
            seen.add(line)
            urls.append(line)
    return urls


# ============================================================
# Node handlers (return list of fired output port names)
# ============================================================
def _h_download(task_id, node_id, config, ctx):
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
    template = os.path.join(UPLOAD_FOLDER, f"_wf_{task_id}_{node_id}.%(ext)s")

    cmd = ['yt-dlp', '-o', template, '-f', format_choice,
           '--merge-output-format', 'mp4', '--no-part', '--no-mtime',
           '--no-warnings', '--ignore-errors', '--no-playlist',
           '--impersonate', 'chrome',
           '--extractor-args', 'youtube:player_client=mweb',
           '--newline', '--no-colors', '--retries', '3']
    if os.path.exists(COOKIES_FILE):
        cmd += ['--cookies', COOKIES_FILE]
    cmd.append(url)

    _log(task_id, node_id, 'info', f'yt-dlp: {url[:80]}')

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            preexec_fn=os.setsid)
    try:
        from features.url_download import (register_process, unregister_process,
                                            kill_process)
        register_process(task_id, proc)
        try:
            for line in proc.stdout:
                if _should_abort(task_id):
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
    _log(task_id, node_id, 'success',
         f'Downloaded {ctx["filename"]} ({_format_size(size)})')
    return ['out']


def _h_download_file(task_id, node_id, config, ctx):
    import requests
    url = ctx.get('url')
    if not url:
        raise Exception('No URL in context')
    name = _filename_from_url(url) or f'file_{uuid.uuid4().hex[:8]}'
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
                    if _should_abort(task_id):
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
    _log(task_id, node_id, 'success',
         f'Downloaded {out_name} ({_format_size(size)})')
    return ['out']


def _h_telegram_send(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file in context')
    chat_link = (config.get('chat_link') or '').strip()
    if not chat_link:
        raise Exception('Chat link required')
    caption = _expand_template(config.get('caption') or '', ctx) or None

    from features.telegram import (
        _run_telegram_job, send_file_to_telegram_async, start_tg_loop_if_needed,
    )
    start_tg_loop_if_needed()

    sub_id = str(uuid.uuid4())
    save_task(sub_id, {
        'task_id': sub_id, 'status': 'queued', 'upload_progress': 0,
        'created_at': time.time(), 'cancelled': False,
        '_hidden': True, 'parent_workflow': task_id,
    })

    def _run():
        try:
            _run_telegram_job(send_file_to_telegram_async, sub_id, chat_link,
                              file_path, os.path.basename(file_path), caption)
        except Exception as e:
            t = load_task(sub_id)
            if t:
                t['status'] = 'error'
                t['error_msg'] = str(e)
                save_task(sub_id, t)

    threading.Thread(target=_run, daemon=True).start()

    deadline = time.time() + 3600
    while time.time() < deadline:
        if _should_abort(task_id):
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
            _log(task_id, node_id, 'success', f'Sent to {chat_link}')
            delete_task(sub_id)
            return ['out']
        if sub.get('status') == 'error':
            err = sub.get('error_msg', 'Telegram send failed')
            delete_task(sub_id)
            raise Exception(err)
        _update_node_progress(task_id, node_id, sub.get('upload_progress', 0))
        time.sleep(0.5)
    delete_task(sub_id)
    raise Exception('Telegram send timed out')


def _h_drive_upload(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file in context')
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
        media_body=media, fields='id, name, webViewLink',
    )
    response = None
    while response is None:
        if _should_abort(task_id):
            raise _Cancelled()
        status, response = req.next_chunk()
        if status:
            _update_node_progress(task_id, node_id, int(status.progress() * 100))

    ctx['drive_file_id'] = response['id']
    ctx['drive_link'] = response.get('webViewLink')
    _log(task_id, node_id, 'success',
         f'Uploaded to Drive ({account}): {filename}')
    return ['out']


def _h_compress(task_id, node_id, config, ctx):
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

    cmd = ['ffmpeg', '-y', '-progress', 'pipe:2', '-stats_period', '0.5',
           '-i', file_path, '-vf', vf, *vcodec,
           '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
           '-af', 'aresample=async=1:first_pts=0',
           '-movflags', '+faststart', out_path]

    _log(task_id, node_id, 'info',
         f'Compressing ({codec} {resolution}p crf={crf} preset={preset})')

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, bufsize=1, preexec_fn=os.setsid)
    try:
        from features.url_download import (register_process, unregister_process,
                                            kill_process)
        register_process(task_id, proc)
        try:
            for line in proc.stderr:
                if _should_abort(task_id):
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
    _log(task_id, node_id, 'success',
         f'Compressed: {_format_size(orig_size)} → {_format_size(new_size)} '
         f'({savings:.0f}% smaller)')
    return ['out']


def _h_delete_local(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path:
        _log(task_id, node_id, 'warning', 'No file in context — skipping')
        return ['out']
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            _log(task_id, node_id, 'success',
                 f'Deleted {os.path.basename(file_path)}')
        except Exception as e:
            raise Exception(f'Delete failed: {e}')
    orig = ctx.get('original_file_path')
    if orig and orig != file_path and os.path.exists(orig):
        try:
            os.remove(orig)
        except Exception:
            pass
    for k in ('file_path', 'filename', 'basename', 'size', 'original_file_path'):
        ctx.pop(k, None)
    return ['out']


def _h_delete_drive(task_id, node_id, config, ctx):
    file_id = ctx.get('drive_file_id')
    if not file_id:
        _log(task_id, node_id, 'warning', 'No Drive file — skipping')
        return ['out']
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
    return ['out']


def _h_wait(task_id, node_id, config, ctx):
    import random
    try:
        seconds = float(config.get('seconds') or 5)
    except (TypeError, ValueError):
        seconds = 5
    try:
        jitter = float(config.get('jitter') or 0)
    except (TypeError, ValueError):
        jitter = 0
    if jitter > 0:
        seconds += random.uniform(0, jitter)
    seconds = max(0, min(seconds, 3600))
    _log(task_id, node_id, 'info', f'Waiting {seconds:.1f}s')
    end = time.time() + seconds
    while time.time() < end:
        if _should_abort(task_id):
            raise _Cancelled()
        time.sleep(0.2)
    return ['out']


def _h_webhook(task_id, node_id, config, ctx):
    import requests
    url = (config.get('url') or '').strip()
    if not url:
        raise Exception('Webhook URL required')
    method = (config.get('method') or 'POST').upper()
    body = _expand_template(config.get('body') or '', ctx)
    _log(task_id, node_id, 'info', f'{method} {url[:80]}')
    try:
        if method == 'GET':
            r = requests.get(url, timeout=30)
        else:
            r = requests.post(url, data=body, timeout=30,
                              headers={'Content-Type': 'application/json'})
        if 200 <= r.status_code < 300:
            _log(task_id, node_id, 'success', f'HTTP {r.status_code}')
        else:
            raise Exception(f'HTTP {r.status_code}')
        return ['out']
    except Exception as e:
        raise Exception(f'Webhook failed: {e}')


def _h_extract_metadata(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file in context')
    ffprobe = shutil.which('ffprobe') or 'ffprobe'
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', file_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise Exception('ffprobe failed')
        data = json.loads(r.stdout or '{}')
    except Exception as e:
        raise Exception(f'Metadata extraction failed: {e}')

    fmt = data.get('format', {})
    streams = data.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    a = next((s for s in streams if s.get('codec_type') == 'audio'), {})

    try:
        ctx['duration'] = float(fmt.get('duration', 0) or 0)
    except (TypeError, ValueError):
        ctx['duration'] = 0
    ctx['width'] = int(v.get('width', 0) or 0)
    ctx['height'] = int(v.get('height', 0) or 0)
    ctx['vcodec'] = v.get('codec_name', '')
    ctx['acodec'] = a.get('codec_name', '')
    _log(task_id, node_id, 'success',
         f'{ctx["width"]}x{ctx["height"]} · {ctx["duration"]:.1f}s · '
         f'{ctx["vcodec"] or "?"}/{ctx["acodec"] or "?"}')
    return ['out']


def _h_condition(task_id, node_id, config, ctx):
    expr = (config.get('expression') or '').strip()
    if not expr:
        raise Exception('Condition expression empty')
    result = _safe_eval(expr, _ctx_to_dict(ctx))
    fired = 'true' if bool(result) else 'false'
    _log(task_id, node_id, 'info', f'Condition → {fired} ({expr[:60]})')
    return [fired]


def _h_switch(task_id, node_id, config, ctx):
    expr = (config.get('expression') or '').strip()
    if not expr:
        raise Exception('Switch expression empty')
    cases = [c.strip() for c in (config.get('cases') or '').split(',') if c.strip()]
    result = _safe_eval(expr, _ctx_to_dict(ctx))
    result_str = str(result) if result is not None else ''
    if result_str in cases:
        port = str(cases.index(result_str) + 1)
        _log(task_id, node_id, 'info', f'Switch → "{result_str}" (port {port})')
        return [port]
    _log(task_id, node_id, 'info', f'Switch → default ({result_str!r})')
    return ['default']


def _h_merge(task_id, node_id, config, ctx):
    mode = (config.get('mode') or 'all').lower()
    _log(task_id, node_id, 'info', f'Merge ({mode})')
    return ['out']


def _h_fork(task_id, node_id, config, ctx):
    """Fires all three output ports. Downstream nodes run sequentially but
    share the same ctx — use Merge nodes to rendezvous."""
    _log(task_id, node_id, 'info', 'Fork → a, b, c')
    return ['a', 'b', 'c']


def _h_delay_until(task_id, node_id, config, ctx):
    from datetime import datetime, timedelta
    target_str = (config.get('target_time') or '').strip()
    if not target_str:
        raise Exception('Target time required')

    target_ts = None
    # HH:MM form
    m = re.match(r'^(\d{1,2}):(\d{2})$', target_str)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        target_ts = target.timestamp()
    else:
        try:
            target_ts = datetime.fromisoformat(target_str).timestamp()
        except Exception:
            raise Exception(f'Cannot parse target time: {target_str}')

    wait = target_ts - time.time()
    if wait <= 0:
        _log(task_id, node_id, 'info', 'Delay: already past target')
        return ['out']
    if wait > 86400:
        raise Exception(f'Target is more than 24h away ({wait/3600:.1f}h)')

    _log(task_id, node_id, 'info', f'Delaying until {target_str} ({wait:.0f}s)')
    end = time.time() + wait
    while time.time() < end:
        if _should_abort(task_id):
            raise _Cancelled()
        time.sleep(1.0)
    return ['out']


def _h_transform(task_id, node_id, config, ctx):
    target = (config.get('target') or '').strip()
    if not target:
        raise Exception('Target key required')
    expr = (config.get('expression') or '').strip()
    if '{' in expr and '}' in expr:
        value = _expand_template(expr, ctx)
    else:
        value = _safe_eval(expr, _ctx_to_dict(ctx))

    if target.startswith('vars.'):
        var_name = target[5:]
        ctx.setdefault('vars', {})[var_name] = value
    else:
        ctx[target] = value
    _log(task_id, node_id, 'success', f'{target} = {str(value)[:80]}')
    return ['out']


def _h_set_variable(task_id, node_id, config, ctx):
    name = (config.get('name') or '').strip()
    if not name:
        raise Exception('Variable name required')
    value = _expand_template(config.get('value') or '', ctx)
    ctx.setdefault('vars', {})[name] = value
    _log(task_id, node_id, 'success', f'vars["{name}"] = {value[:80]}')
    return ['out']


def _h_sub_workflow(task_id, node_id, config, ctx):
    wf_id = (config.get('workflow_id') or '').strip()
    if not wf_id:
        raise Exception('Workflow ID required')

    depth = int(ctx.get('_depth', 0))
    if depth >= MAX_SUBWORKFLOW_DEPTH:
        raise Exception(f'Sub-workflow depth limit ({MAX_SUBWORKFLOW_DEPTH}) exceeded')

    wf = _load_workflow(wf_id)
    if not wf:
        raise Exception(f'Workflow not found: {wf_id}')

    inherit = config.get('inherit_context', True)
    ret_ctx = config.get('return_context', True)

    sub_ctx = dict(ctx) if inherit else {
        'url': ctx.get('url', ''),
        'index': ctx.get('index', 0),
        'total': ctx.get('total', 0),
        'quality': ctx.get('quality', 'best'),
        'vars': dict(ctx.get('vars', {})),
    }
    sub_ctx['_depth'] = depth + 1

    _log(task_id, node_id, 'info',
         f'Running sub-workflow "{wf.get("name", wf_id)}" (depth {depth + 1})')

    # Run the sub-workflow synchronously — this is a nested execution
    try:
        final_ctx = _run_workflow_sync(wf, sub_ctx, parent_task_id=task_id,
                                       parent_node_id=node_id)
    except Exception as e:
        raise Exception(f'Sub-workflow failed: {e}')

    if ret_ctx and final_ctx:
        for k, v in final_ctx.items():
            if not str(k).startswith('_') and k not in ('item', 'index', 'total', 'quality'):
                if k == 'vars':
                    merged = dict(ctx.get('vars', {}))
                    merged.update(v or {})
                    ctx['vars'] = merged
                elif k in ('file_path', 'filename', 'basename', 'size',
                           'drive_file_id', 'drive_link', 'duration',
                           'width', 'height', 'vcodec', 'acodec'):
                    ctx[k] = v

    _log(task_id, node_id, 'success', 'Sub-workflow complete')
    return ['out']


def _h_user_input(task_id, node_id, config, ctx):
    message = _expand_template(config.get('message') or 'Approve?', ctx)
    try:
        timeout = float(config.get('approval_timeout') or 3600)
    except (TypeError, ValueError):
        timeout = 3600

    # Set task state
    task = load_task(task_id)
    task['awaiting_input'] = {
        'node_id': node_id,
        'message': message,
        'created_at': time.time(),
        'expires_at': time.time() + timeout,
    }
    save_task(task_id, task)
    _log(task_id, node_id, 'warning', f'⏸ Awaiting approval: {message[:80]}')

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _should_abort(task_id):
            task = load_task(task_id)
            task['awaiting_input'] = None
            save_task(task_id, task)
            raise _Cancelled()
        t = load_task(task_id)
        resp = t.get('_user_input_response')
        if resp and resp.get('node_id') == node_id:
            t['awaiting_input'] = None
            t['_user_input_response'] = None
            save_task(task_id, t)
            if resp.get('response') == 'approve':
                _log(task_id, node_id, 'success', '✅ Approved')
                return ['approved']
            else:
                _log(task_id, node_id, 'warning', '❌ Rejected')
                return ['rejected']
        time.sleep(0.5)

    task = load_task(task_id)
    task['awaiting_input'] = None
    save_task(task_id, task)
    _log(task_id, node_id, 'warning', '⏱ Approval timed out')
    return ['timeout']


def _h_json_parse(task_id, node_id, config, ctx):
    src = _safe_eval(config.get('source') or '', _ctx_to_dict(ctx))
    into = (config.get('into_var') or 'parsed').strip()
    if src is None:
        raise Exception('Source expression returned None')
    if isinstance(src, (dict, list)):
        data = src
    else:
        try:
            data = json.loads(str(src))
        except Exception as e:
            raise Exception(f'JSON parse failed: {e}')

    ctx.setdefault('vars', {})
    if isinstance(data, dict):
        for k, v in data.items():
            ctx['vars'][f'{into}.{k}'] = v
        # Also expose the whole object for expressions
        ctx['vars'][into] = data
    else:
        ctx['vars'][into] = data

    n = len(data) if hasattr(data, '__len__') else 0
    _log(task_id, node_id, 'success',
         f'Parsed {n} item(s) into vars.{into}')
    return ['out']


def _h_string_ops(task_id, node_id, config, ctx):
    source = _safe_eval(config.get('source') or '', _ctx_to_dict(ctx))
    s = '' if source is None else str(source)
    op = (config.get('operation') or 'lower').lower()
    a1 = _expand_template(config.get('arg1') or '', ctx)
    a2 = _expand_template(config.get('arg2') or '', ctx)
    target = (config.get('target') or 'vars.result').strip()

    try:
        if op == 'lower':
            result = s.lower()
        elif op == 'upper':
            result = s.upper()
        elif op == 'trim':
            result = s.strip()
        elif op == 'strip_ext':
            result = os.path.splitext(s)[0]
        elif op == 'replace':
            result = s.replace(a1, a2)
        elif op == 'split_take_first':
            result = s.split(a1 or ' ')[0]
        elif op == 'split_take_last':
            result = s.split(a1 or ' ')[-1]
        elif op == 'slice':
            parts = (a1 or '0:').split(':')
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else None
            result = s[start:end]
        elif op == 'regex_replace':
            result = re.sub(a1, a2, s)
        elif op == 'regex_extract':
            m = re.search(a1, s)
            result = m.group(0) if m else ''
        else:
            raise Exception(f'Unknown operation: {op}')
    except Exception as e:
        raise Exception(f'String op failed: {e}')

    if target.startswith('vars.'):
        ctx.setdefault('vars', {})[target[5:]] = result
    else:
        ctx[target] = result
    _log(task_id, node_id, 'success', f'{target} = {str(result)[:80]}')
    return ['out']


def _h_assert(task_id, node_id, config, ctx):
    expr = (config.get('expression') or '').strip()
    if not expr:
        raise Exception('Expression required')
    result = _safe_eval(expr, _ctx_to_dict(ctx))
    if not result:
        msg = _expand_template(config.get('message') or 'Assertion failed', ctx)
        raise Exception(msg)
    _log(task_id, node_id, 'success', 'Assertion passed')
    return ['out']


def _h_http_fetch(task_id, node_id, config, ctx):
    import requests
    url = _expand_template(config.get('url') or '', ctx).strip()
    if not url:
        raise Exception('URL required')
    into = (config.get('into_var') or 'body').strip()
    try:
        max_bytes = int(config.get('max_bytes') or 1048576)
    except (TypeError, ValueError):
        max_bytes = 1048576

    _log(task_id, node_id, 'info', f'GET {url[:80]}')
    try:
        r = requests.get(url, timeout=30, stream=True)
        r.raise_for_status()
        content = b''
        for chunk in r.iter_content(chunk_size=8192):
            if _should_abort(task_id):
                raise _Cancelled()
            content += chunk
            if len(content) >= max_bytes:
                content = content[:max_bytes]
                break
        text = content.decode('utf-8', errors='replace')
    except _Cancelled:
        raise
    except Exception as e:
        raise Exception(f'HTTP fetch failed: {e}')

    ctx.setdefault('vars', {})[into] = text
    _log(task_id, node_id, 'success',
         f'Fetched {len(text)} chars into vars.{into}')
    return ['out']


def _h_log(task_id, node_id, config, ctx):
    level = (config.get('level') or 'info').lower()
    msg = _expand_template(config.get('message') or '', ctx)
    _log(task_id, node_id, level, msg)
    return ['out']


NODE_HANDLERS = {
    'action.download':          _h_download,
    'action.download_file':     _h_download_file,
    'action.telegram_send':     _h_telegram_send,
    'action.drive_upload':      _h_drive_upload,
    'action.compress':          _h_compress,
    'action.delete_local':      _h_delete_local,
    'action.delete_drive':      _h_delete_drive,
    'action.wait':              _h_wait,
    'action.webhook':           _h_webhook,
    'action.extract_metadata':  _h_extract_metadata,
    'action.sub_workflow':      _h_sub_workflow,
    'action.user_input':        _h_user_input,
    'action.json_parse':        _h_json_parse,
    'action.string_ops':        _h_string_ops,
    'action.assert':            _h_assert,
    'action.http_fetch':        _h_http_fetch,
    'action.log':               _h_log,
    'logic.condition':          _h_condition,
    'logic.switch':             _h_switch,
    'logic.merge':              _h_merge,
    'logic.fork':               _h_fork,
    'logic.delay_until':        _h_delay_until,
    'data.transform':           _h_transform,
    'data.set_variable':        _h_set_variable,
}


# ============================================================
# Storage
# ============================================================
def _wf_path(wf_id):
    return os.path.join(WORKFLOWS_DIR, f'{wf_id}.json')


def _load_workflow(wf_id):
    path = _wf_path(wf_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return _migrate_workflow(data)
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
    edges = workflow.get('edges', [])
    if not isinstance(edges, list):
        return 'Edges must be a list'

    node_map = {}
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            return f'Node {i} must be an object'
        if not node.get('id') or not isinstance(node['id'], str):
            return f'Node {i} missing id'
        if node['id'] in node_map:
            return f'Duplicate node id: {node["id"]}'
        ntype = node.get('type')
        if ntype not in NODE_TYPES:
            return f'Node {i} unknown type: {ntype}'
        node_map[node['id']] = node

        config = node.get('config') or {}
        for field in NODE_TYPES[ntype].get('fields', []):
            if field.get('required'):
                if not str(config.get(field['name'], '')).strip():
                    return f'{NODE_TYPES[ntype]["label"]}: {field["label"]} required'

    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            return f'Edge {i} must be an object'
        f, t = e.get('from'), e.get('to')
        if f not in node_map or t not in node_map:
            return f'Edge {i} references unknown node'
        if f == t:
            return f'Edge {i} is a self-loop'
        from_port = e.get('from_port') or 'out'
        if from_port not in _node_ports(node_map[f]['type'])['out']:
            return (f'Edge {i}: node {f[:8]} has no output port "{from_port}"')

    triggers = [n for n in nodes if n['type'].startswith('trigger.')]
    if not triggers:
        return 'Need at least one trigger'
    if len(triggers) > 1:
        return 'Only one trigger allowed'
    trigger = triggers[0]

    if _in_edges(trigger['id'], edges):
        return 'Trigger cannot have incoming connections'

    for node in nodes:
        if node['id'] == trigger['id']:
            continue
        ins = _in_edges(node['id'], edges)
        if node['type'] == 'logic.merge':
            if len(ins) < 2:
                return f'{NODE_TYPES[node["type"]]["label"]} needs ≥2 incoming'
        else:
            if len(ins) != 1:
                return (f'{NODE_TYPES[node["type"]]["label"]}: needs exactly '
                        f'1 incoming (has {len(ins)})')

    if _topological_order(nodes, edges) is None:
        return 'Workflow contains a cycle'

    reachable = {trigger['id']}
    changed = True
    while changed:
        changed = False
        for e in edges:
            if e.get('from') in reachable and e.get('to') not in reachable:
                reachable.add(e['to'])
                changed = True
    unreachable = [n['id'] for n in nodes if n['id'] not in reachable]
    if unreachable:
        return f'{len(unreachable)} node(s) unreachable from trigger'

    return None


# ============================================================
# Node execution with retry / timeout / dry-run
# ============================================================
class _TimeoutContext:
    def __init__(self, task_id, timeout_sec):
        self.task_id = task_id
        self.timer = None
        if timeout_sec > 0:
            self.timer = threading.Timer(timeout_sec, self._fire)
            self.timer.daemon = True
            self.timer.start()

    def _fire(self):
        task = load_task(self.task_id)
        if task and not task.get('_abort'):
            task['_abort'] = 'timeout'
            save_task(self.task_id, task)

    def cancel(self):
        if self.timer:
            self.timer.cancel()


def _execute_node(task_id, node, node_config, ctx, dry_run=False):
    node_type = node['type']
    if node_type.startswith('trigger.'):
        raise Exception(f'Trigger node cannot be executed: {node_type}')

    handler = NODE_HANDLERS.get(node_type)
    if not handler:
        raise Exception(f'No handler for {node_type}')

    # Skip condition
    skip_expr = (node_config.get('skip_if') or '').strip()
    if skip_expr:
        try:
            if _safe_eval(skip_expr, _ctx_to_dict(ctx)):
                _log(task_id, node['id'], 'info', f'Skipped ({skip_expr[:60]})')
                return ['out']
        except Exception as e:
            _log(task_id, node['id'], 'warning',
                 f'skip_if error: {e} — running anyway')

    # Dry run: skip side-effectful nodes
    if dry_run and node_type.startswith(('action.', 'data.')):
        label = NODE_TYPES[node_type]['label']
        _log(task_id, node['id'], 'info', f'[DRY RUN] Would run {label}')
        # Determine what ports would fire
        if node_type == 'logic.switch':
            # Still evaluate to show routing
            try:
                ports = handler(task_id, node['id'], node_config, ctx)
                _log(task_id, node['id'], 'info',
                     f'[DRY RUN] Ports: {", ".join(ports)}')
                return ports
            except Exception:
                pass
        # Default to all non-special ports (or 'out')
        ports = _node_ports(node_type)['out']
        # For action/user_input etc., dry-run returns first non-error port
        if 'out' in ports:
            return ['out']
        return [ports[0]] if ports else ['out']

    # Retry settings
    try:
        retry_max = int(node_config.get('retry_max') or 0)
    except (TypeError, ValueError):
        retry_max = 0
    try:
        retry_delay = float(node_config.get('retry_delay') or 3)
    except (TypeError, ValueError):
        retry_delay = 3
    backoff = (node_config.get('retry_backoff') or 'fixed').lower()
    on_error = (node_config.get('on_error') or 'stop').lower()
    try:
        timeout_sec = float(node_config.get('timeout_sec') or 0)
    except (TypeError, ValueError):
        timeout_sec = 0

    attempt = 0
    last_err = None
    start_time = time.time()

    while attempt <= retry_max:
        if _should_abort(task_id):
            reason = _abort_reason(task_id)
            if reason == 'timeout':
                raise _TimedOut('Node timeout')
            raise _Cancelled()

        tctx = _TimeoutContext(task_id, timeout_sec)
        try:
            ports = handler(task_id, node['id'], node_config, ctx)
            tctx.cancel()
            _update_node_progress(task_id, node['id'], 100, 'done')
            _record_timing(task_id, node['id'],
                           (time.time() - start_time) * 1000)
            _record_context_snapshot(task_id, node['id'], ctx)
            return ports or ['out']
        except _Cancelled:
            tctx.cancel()
            raise
        except _TimedOut:
            tctx.cancel()
            last_err = _TimedOut('Node timed out')
            attempt += 1
        except Exception as e:
            tctx.cancel()
            last_err = e
            attempt += 1

        # Check for timeout abort
        if _abort_reason(task_id) == 'timeout':
            task = load_task(task_id)
            if task:
                task['_abort'] = None
                save_task(task_id, task)
            last_err = _TimedOut(f'Node timed out after {timeout_sec}s')

        if attempt <= retry_max:
            if backoff == 'exponential':
                delay = min(retry_delay * (2 ** (attempt - 1)), 60)
            else:
                delay = retry_delay
            _log(task_id, node['id'], 'warning',
                 f'Attempt {attempt}/{retry_max + 1} failed: {last_err}. '
                 f'Retry in {delay:.1f}s…')
            end = time.time() + delay
            while time.time() < end:
                if _should_abort(task_id):
                    raise _Cancelled()
                time.sleep(0.2)

    # All retries exhausted
    _update_node_progress(task_id, node['id'], 100, 'error')
    _record_timing(task_id, node['id'], (time.time() - start_time) * 1000)
    _record_context_snapshot(task_id, node['id'], ctx)

    if on_error == 'continue':
        _log(task_id, node['id'], 'error',
             f'Failed (continuing): {last_err}')
        return ['out']

    raise last_err


# ============================================================
# Item preparation (trigger execution)
# ============================================================
def _prepare_items(task_id, trigger, edges):
    cfg = trigger.get('config') or {}

    if trigger['type'] == 'trigger.url_list':
        urls_raw = str(cfg.get('urls', '')).strip()
        raw = [ln.strip() for ln in urls_raw.split('\n')
               if ln.strip() and not ln.strip().startswith('#')]
        if not raw:
            raise Exception('URL list is empty')

        if cfg.get('expand_playlists', True):
            from features.url_download import is_playlist_url
            try:
                max_items = int(cfg.get('max_items', 0) or 0)
            except (TypeError, ValueError):
                max_items = 0
            _log(task_id, trigger['id'], 'info',
                 f'Checking {len(raw)} URL(s) for playlists…')
            expanded = []
            for u in raw:
                if _should_abort(task_id):
                    raise _Cancelled()
                if is_playlist_url(u):
                    _log(task_id, trigger['id'], 'info',
                         f'Expanding: {u[:80]}')
                    vids = _expand_playlist(u, max_items)
                    if vids:
                        _log(task_id, trigger['id'], 'success',
                             f'Playlist → {len(vids)} video(s)')
                        expanded.extend(vids)
                    else:
                        expanded.append(u)
                else:
                    expanded.append(u)
            return expanded
        return raw

    if trigger['type'] == 'trigger.file_glob':
        import glob
        pattern = cfg.get('pattern') or '*.mp4'
        try:
            max_items = int(cfg.get('max_items', 0) or 0)
        except (TypeError, ValueError):
            max_items = 0
        matches = sorted(glob.glob(os.path.join(UPLOAD_FOLDER, pattern),
                                   recursive=True))
        if not matches:
            raise Exception(f'No files matched: {pattern}')
        files = []
        for m in matches:
            if os.path.isfile(m):
                files.append(m)
                if max_items > 0 and len(files) >= max_items:
                    break
        return files

    raise Exception(f'Unknown trigger type: {trigger["type"]}')


# ============================================================
# Workflow execution
# ============================================================
def _run_workflow_sync(workflow, initial_ctx, parent_task_id=None,
                        parent_node_id=None):
    """
    Run a workflow synchronously and return the final context.
    Used by the sub-workflow node.

    Doesn't create a visible task — logs into parent task.
    """
    nodes = workflow['nodes']
    edges = workflow.get('edges', [])
    node_map = {n['id']: n for n in nodes}

    order = _topological_order(nodes, edges)
    if not order:
        raise Exception('Cycle in sub-workflow')

    trigger = next((n for n in nodes if n['type'].startswith('trigger.')), None)
    if not trigger:
        raise Exception('Sub-workflow has no trigger')

    # For sub-workflows we start fresh: extract items from trigger
    items = _prepare_items(parent_task_id or 'sub', trigger, edges)

    # Run only the FIRST item to keep semantics simple.
    # (If sub-workflow needs to iterate, use its own trigger loop.)
    if not items:
        raise Exception('Sub-workflow trigger produced no items')

    item = items[0]
    ctx = dict(initial_ctx)
    ctx['item'] = item
    ctx['url'] = str(item)
    ctx['index'] = 1
    ctx['total'] = 1
    ctx['quality'] = (trigger.get('config') or {}).get('quality', 'best')

    # Execute the graph (same token model as parent)
    incoming_count = {nid: 0 for nid in node_map}
    expected_count = {nid: len(_in_edges(nid, edges)) for nid in node_map}
    completed = {trigger['id']}
    worklist = []

    for edge in _out_edges(trigger['id'], edges, 'out'):
        to_id = edge['to']
        incoming_count[to_id] = incoming_count.get(to_id, 0) + 1
        if incoming_count[to_id] >= expected_count.get(to_id, 1):
            worklist.append(to_id)

    while worklist:
        if _should_abort(parent_task_id or 'sub'):
            raise _Cancelled()

        node_id = worklist.pop(0)
        if node_id in completed:
            continue
        node = node_map[node_id]
        node_cfg = node.get('config') or {}

        in_count = incoming_count[node_id]
        exp = expected_count[node_id]
        if exp > 0 and in_count < exp:
            if not (node['type'] == 'logic.merge' and
                    (node_cfg.get('mode') or 'all').lower() == 'any'):
                continue

        ports = _execute_node(parent_task_id or 'sub', node, node_cfg, ctx,
                              dry_run=False)
        completed.add(node_id)

        for port in ports:
            for edge in _out_edges(node_id, edges, port):
                to_id = edge['to']
                if to_id in completed:
                    continue
                incoming_count[to_id] = incoming_count.get(to_id, 0) + 1
                exp_to = expected_count.get(to_id, 0)
                if exp_to == 0 or incoming_count[to_id] >= exp_to:
                    worklist.append(to_id)
                elif (node_map[to_id]['type'] == 'logic.merge' and
                      (node_map[to_id].get('config') or {}).get('mode', 'all').lower() == 'any'):
                    worklist.append(to_id)

    return ctx


def _run_workflow(task_id, workflow, dry_run=False):
    nodes = workflow['nodes']
    edges = workflow.get('edges', [])
    node_map = {n['id']: n for n in nodes}

    order = _topological_order(nodes, edges)
    if not order:
        _log(task_id, None, 'error', 'Cycle detected')
        raise Exception('Invalid workflow graph')

    trigger = next((n for n in nodes if n['type'].startswith('trigger.')), None)
    if not trigger:
        raise Exception('No trigger node')

    try:
        items = _prepare_items(task_id, trigger, edges)
        if not items:
            raise Exception('Trigger produced no items')

        task = load_task(task_id)
        task['items'] = [str(x)[:200] for x in items]
        task['total_items'] = len(items)
        save_task(task_id, task)

        mode_label = '[DRY RUN] ' if dry_run else ''
        _log(task_id, None, 'info',
             f'{mode_label}Starting "{workflow.get("name", "Untitled")}" — '
             f'{len(items)} item(s), {len(nodes) - 1} node(s)')

        for idx, item in enumerate(items):
            if _should_abort(task_id):
                raise _Cancelled()

            task = load_task(task_id)
            task['current_item_index'] = idx
            task['current_item'] = str(item)[:200]
            task['progress'] = int(100 * idx / len(items))
            task['node_statuses'] = {
                trigger['id']: {'status': 'done', 'progress': 100},
            }
            save_task(task_id, task)

            _log(task_id, None, 'info',
                 f'▶ Item {idx + 1}/{len(items)}: {str(item)[:100]}')

            ctx = {
                'item': item,
                'url': str(item),
                'index': idx + 1,
                'total': len(items),
                'quality': (trigger.get('config') or {}).get('quality', 'best'),
                'vars': {},
                '_depth': 0,
            }

            # ---- Token-based traversal ----
            incoming_count = {nid: 0 for nid in node_map}
            expected_count = {nid: len(_in_edges(nid, edges)) for nid in node_map}
            completed = {trigger['id']}
            worklist = []

            # Seed worklist from trigger's 'out' port (trigger already ran)
            for edge in _out_edges(trigger['id'], edges, 'out'):
                to_id = edge['to']
                if to_id in completed:
                    continue
                incoming_count[to_id] = incoming_count.get(to_id, 0) + 1
                exp_to = expected_count.get(to_id, 0)
                if exp_to == 0 or incoming_count[to_id] >= exp_to:
                    worklist.append(to_id)
                elif (node_map[to_id]['type'] == 'logic.merge' and
                      (node_map[to_id].get('config') or {}).get('mode', 'all').lower() == 'any'):
                    worklist.append(to_id)

            while worklist:
                if _should_abort(task_id):
                    raise _Cancelled()

                node_id = worklist.pop(0)
                if node_id in completed:
                    continue

                node = node_map[node_id]
                node_cfg = node.get('config') or {}

                in_count = incoming_count[node_id]
                exp = expected_count[node_id]
                if exp > 0 and in_count < exp:
                    if not (node['type'] == 'logic.merge' and
                            (node_cfg.get('mode') or 'all').lower() == 'any'):
                        continue

                task = load_task(task_id)
                task['current_node_id'] = node_id
                task['node_statuses'][node_id] = {'status': 'running', 'progress': 0}
                save_task(task_id, task)
                _log(task_id, node_id, 'info',
                     f'Running {NODE_TYPES[node["type"]]["label"]}')

                try:
                    fired_ports = _execute_node(task_id, node, node_cfg, ctx,
                                                dry_run=dry_run)
                except _TimedOut as e:
                    raise Exception(
                        f'{NODE_TYPES[node["type"]]["label"]}: timed out')
                except _Cancelled:
                    raise
                except Exception as e:
                    logger.exception(f'Node {node_id} failed')
                    raise Exception(
                        f'{NODE_TYPES[node["type"]]["label"]}: {e}')

                completed.add(node_id)

                for port in fired_ports:
                    for edge in _out_edges(node_id, edges, port):
                        to_id = edge['to']
                        if to_id in completed:
                            continue
                        incoming_count[to_id] = incoming_count.get(to_id, 0) + 1
                        exp_to = expected_count.get(to_id, 0)
                        if exp_to == 0 or incoming_count[to_id] >= exp_to:
                            worklist.append(to_id)
                        elif (node_map[to_id]['type'] == 'logic.merge' and
                              (node_map[to_id].get('config') or {}).get('mode', 'all').lower() == 'any'):
                            worklist.append(to_id)

            # Stall detection
            stalled = [nid for nid in node_map if nid not in completed]
            if stalled:
                labels = [NODE_TYPES[node_map[nid]['type']]['label'] for nid in stalled]
                _log(task_id, None, 'warning',
                     f'Item {idx+1}: {len(stalled)} node(s) never fired: '
                     f'{", ".join(labels[:5])}')

        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            save_task(task_id, task)
        _log(task_id, None, 'success',
             f'✅ All {len(items)} item(s) processed')

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
            return jsonify({'error': 'Failed to save'}), 500
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

    @app.route('/workflow/export/<wf_id>')
    def workflow_export(wf_id):
        wf = _load_workflow(wf_id)
        if not wf:
            return jsonify({'error': 'Not found'}), 404
        return Response(
            json.dumps(wf, indent=2),
            mimetype='application/json',
            headers={
                'Content-Disposition':
                    f'attachment; filename=workflow_{wf_id}.json'
            }
        )

    @app.route('/workflow/import', methods=['POST'])
    def workflow_import():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'No JSON data'}), 400
        old_id = data.get('id')
        data.pop('id', None)
        data['name'] = (data.get('name') or 'Imported') + ' (imported)'
        err = _validate_workflow(data)
        if err:
            return jsonify({'error': err, 'source_id': old_id}), 400
        new_id = _save_workflow(data)
        if not new_id:
            return jsonify({'error': 'Failed to save'}), 500
        return jsonify({'id': new_id, 'name': data['name']})

    @app.route('/workflow/run', methods=['POST'])
    def workflow_run():
        data = request.get_json(silent=True) or {}
        err = _validate_workflow(data)
        if err:
            return jsonify({'error': err}), 400
        if len(data['nodes']) < 2:
            return jsonify({'error': 'Add at least one action node'}), 400

        dry_run = bool(data.get('dry_run', False))
        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id, 'status': 'running', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'workflow_name': data.get('name', 'Untitled'),
            'items': [], 'total_items': 0,
            'current_item_index': 0, 'current_item': '',
            'current_node_id': None, 'node_statuses': {}, 'logs': [],
            'context_snapshots': {}, 'node_timings': [],
            'dry_run': dry_run,
            'type': 'workflow',
        })

        threading.Thread(target=_run_workflow,
                         args=(task_id, data, dry_run),
                         daemon=True).start()
        return jsonify({'task_id': task_id, 'dry_run': dry_run})

    @app.route('/workflow/input/<task_id>', methods=['POST'])
    def workflow_input_response(task_id):
        data = request.get_json(silent=True) or {}
        response = (data.get('response') or '').strip().lower()
        if response not in ('approve', 'reject'):
            return jsonify({'error': 'response must be "approve" or "reject"'}), 400
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        awaiting = task.get('awaiting_input')
        if not awaiting:
            return jsonify({'error': 'No pending input'}), 400
        task['_user_input_response'] = {
            'node_id': awaiting['node_id'],
            'response': response,
            'ts': time.time(),
        }
        save_task(task_id, task)
        return jsonify({'status': 'ok', 'response': response})

    @app.route('/workflow/context/<task_id>')
    def workflow_context(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({
            'current_item': task.get('current_item'),
            'current_node_id': task.get('current_node_id'),
            'context_snapshots': task.get('context_snapshots', {}),
            'node_timings': task.get('node_timings', []),
            'awaiting_input': task.get('awaiting_input'),
        })

    @app.route('/workflow/test_node', methods=['POST'])
    def workflow_test_node():
        data = request.get_json(silent=True) or {}
        node_type = data.get('type')
        node_config = data.get('config') or {}
        dry_run = bool(data.get('dry_run', True))

        if node_type not in NODE_TYPES:
            return jsonify({'error': 'Unknown node type'}), 400
        if node_type.startswith('trigger.'):
            return jsonify({'error': 'Cannot test a trigger node directly'}), 400
        if node_type not in NODE_HANDLERS:
            return jsonify({'error': f'No handler for {node_type}'}), 400

        sample_ctx = {
            'url': data.get('url', 'https://example.com/test.mp4'),
            'filename': data.get('filename', 'test.mp4'),
            'basename': os.path.splitext(data.get('filename', 'test.mp4'))[0],
            'size': data.get('size', 1024 * 1024),
            'index': 1, 'total': 1, 'quality': 'best',
            'duration': 0, 'width': 0, 'height': 0,
            'vcodec': '', 'acodec': '',
            'vars': {},
            '_depth': 0,
        }
        sample_ctx.update(data.get('ctx') or {})

        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id, 'status': 'running', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'logs': [], 'node_statuses': {}, 'node_timings': [],
            'context_snapshots': {},
            'dry_run': dry_run,
            '_hidden': True,
            'type': 'workflow_test',
        })

        try:
            node = {'id': 'test', 'type': node_type, 'config': node_config}
            ports = _execute_node(task_id, node, node_config, sample_ctx,
                                  dry_run=dry_run)

            t = load_task(task_id)
            logs = t.get('logs', []) if t else []

            delete_task(task_id)

            return jsonify({
                'status': 'ok',
                'output_ports': ports,
                'ctx_after': _safe_ctx_snapshot(sample_ctx),
                'logs': logs,
                'dry_run': dry_run,
            })
        except Exception as e:
            t = load_task(task_id)
            logs = t.get('logs', []) if t else []
            delete_task(task_id)
            return jsonify({
                'status': 'error',
                'error': str(e),
                'logs': logs,
                'dry_run': dry_run,
            }), 200
