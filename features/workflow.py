# features/workflow.py
"""
Advanced node-based workflow engine with:
  • Server-Sent Events for live updates (no polling)
  • Autosave support
  • Resume with graph validation
  • Checkpointing after every successful node
"""

import os
import re
import json
import time
import uuid
import queue
import shutil
import hashlib
import threading
import subprocess
import logging
import glob as _glob
from flask import request, jsonify, Response, stream_with_context
from tasks import (save_task, load_task, delete_task,
                    add_subscriber, remove_subscriber)
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(PROJECT_ROOT, 'workflows')
os.makedirs(WORKFLOWS_DIR, exist_ok=True)

MAX_SUBWORKFLOW_DEPTH = 10
MAX_CONTEXT_SNAPSHOTS = 5
MAX_TIMINGS = 20
SSE_HEARTBEAT_SEC = 20


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


# ============================================================
# Node type registry — unchanged structure, condensed here
# ============================================================
COMMON_ADVANCED = [
    {'name': 'retry_max',   'type': 'number', 'label': 'Retries on failure',
     'default': 0, 'help': '0 = no retry'},
    {'name': 'retry_delay', 'type': 'number', 'label': 'Base retry delay (s)',
     'default': 3},
    {'name': 'retry_backoff', 'type': 'select', 'label': 'Retry backoff',
     'options': ['fixed', 'exponential'], 'default': 'fixed'},
    {'name': 'timeout_sec', 'type': 'number', 'label': 'Timeout (s, 0 = none)',
     'default': 0},
    {'name': 'on_error',    'type': 'select', 'label': 'On error',
     'options': ['stop', 'continue'], 'default': 'stop'},
    {'name': 'skip_if',     'type': 'text', 'label': 'Skip if (Python expr)',
     'default': ''},
]


NODE_TYPES = {
    'trigger.url_list': {
        'label': 'URL List', 'category': 'trigger',
        'icon': 'fa-list', 'color': '#4ade80',
        'description': 'Provides a list of URLs. Each downstream action runs '
                       'once per URL.',
        'ports': {'in': [], 'out': ['out']},
        'fields': [
            {'name': 'urls', 'type': 'textarea', 'label': 'URLs (one per line)',
             'required': True,
             'placeholder': 'https://youtube.com/watch?v=...\nhttps://example.com/file.mp4'},
            {'name': 'quality', 'type': 'select', 'label': 'Download quality',
             'options': ['best', '2160p', '1440p', '1080p', '720p', '480p', 'audio'],
             'default': '1080p'},
            {'name': 'expand_playlists', 'type': 'checkbox',
             'label': 'Expand playlists into individual videos', 'default': True},
            {'name': 'max_items', 'type': 'number',
             'label': 'Max videos per playlist (0 = all)', 'default': 0},
            {'name': 'title_based_naming', 'type': 'checkbox',
             'label': 'Name files by video title', 'default': True},
        ],
        'advanced_fields': [],
    },
    'trigger.file_glob': {
        'label': 'File Glob', 'category': 'trigger',
        'icon': 'fa-folder-open', 'color': '#4ade80',
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
    'action.download': {
        'label': 'Download Video', 'category': 'action',
        'icon': 'fa-download', 'color': '#5f8fe0',
        'description': 'Downloads the current URL with yt-dlp.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.download_file': {
        'label': 'Download File', 'category': 'action',
        'icon': 'fa-file-arrow-down', 'color': '#5f8fe0',
        'description': 'Downloads the URL as a raw file (PDF, ZIP, etc).',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.telegram_send': {
        'label': 'Send to Telegram', 'category': 'action',
        'icon': 'fa-paper-plane', 'color': '#22d3ee',
        'description': 'Sends the current file to a Telegram chat.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'chat_link', 'type': 'text', 'label': 'Chat link',
             'required': True, 'placeholder': '@channel or https://t.me/...'},
            {'name': 'caption', 'type': 'text', 'label': 'Caption',
             'default': '📁 {filename}'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.drive_upload': {
        'label': 'Upload to Drive', 'category': 'action',
        'icon': 'fa-cloud-upload-alt', 'color': '#a78bfa',
        'description': 'Uploads the current file to Google Drive.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'account', 'type': 'text', 'label': 'Drive account',
             'default': 'default'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.compress': {
        'label': 'Compress Video', 'category': 'action',
        'icon': 'fa-compress', 'color': '#f59e0b',
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
        'label': 'Delete Local File', 'category': 'action',
        'icon': 'fa-trash-alt', 'color': '#ef4444',
        'description': 'Deletes the current file from local disk.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.delete_drive': {
        'label': 'Delete Drive File', 'category': 'action',
        'icon': 'fa-trash-alt', 'color': '#ef4444',
        'description': 'Deletes the file from Drive (upload first).',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'account', 'type': 'text', 'label': 'Drive account',
             'default': 'default'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.wait': {
        'label': 'Wait', 'category': 'action',
        'icon': 'fa-clock', 'color': '#8a9bc0',
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
        'label': 'Webhook', 'category': 'action',
        'icon': 'fa-globe', 'color': '#4ade80',
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
        'label': 'Extract Metadata', 'category': 'action',
        'icon': 'fa-info-circle', 'color': '#22d3ee',
        'description': 'Extracts duration, resolution, codecs.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.sub_workflow': {
        'label': 'Run Sub-Workflow', 'category': 'action',
        'icon': 'fa-cubes', 'color': '#a78bfa',
        'description': 'Runs another saved workflow synchronously.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'workflow_id', 'type': 'text', 'label': 'Workflow ID',
             'required': True},
            {'name': 'inherit_context', 'type': 'checkbox',
             'label': 'Pass parent context to sub-workflow', 'default': True},
            {'name': 'return_context', 'type': 'checkbox',
             'label': 'Merge sub-workflow result into parent context',
             'default': True},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.user_input': {
        'label': 'Wait for Approval', 'category': 'action',
        'icon': 'fa-user-check', 'color': '#f59e0b',
        'description': 'Pauses until approved/rejected from the UI.',
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
        'label': 'JSON Parse', 'category': 'action',
        'icon': 'fa-code', 'color': '#4ade80',
        'description': 'Parses a JSON string into vars.<into>.*',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'source', 'type': 'text', 'label': 'Source expression',
             'required': True, 'default': 'vars.body'},
            {'name': 'into_var', 'type': 'text', 'label': 'Store into var',
             'default': 'parsed'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.string_ops': {
        'label': 'String Ops', 'category': 'action',
        'icon': 'fa-font', 'color': '#4ade80',
        'description': 'Manipulate a string.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'source', 'type': 'text', 'label': 'Source expression',
             'required': True, 'default': 'filename'},
            {'name': 'operation', 'type': 'select', 'label': 'Operation',
             'options': ['lower', 'upper', 'trim', 'strip_ext', 'replace',
                         'split_take_first', 'split_take_last', 'slice',
                         'regex_replace', 'regex_extract'],
             'default': 'lower'},
            {'name': 'arg1', 'type': 'text', 'label': 'Arg 1', 'default': ''},
            {'name': 'arg2', 'type': 'text', 'label': 'Arg 2', 'default': ''},
            {'name': 'target', 'type': 'text', 'label': 'Store into',
             'required': True, 'default': 'vars.result'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.assert': {
        'label': 'Assert', 'category': 'action',
        'icon': 'fa-shield-alt', 'color': '#ef4444',
        'description': 'Fails the workflow if the condition is false.',
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
        'label': 'HTTP Fetch', 'category': 'action',
        'icon': 'fa-cloud-download-alt', 'color': '#4ade80',
        'description': 'GET a URL, store body in vars.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'url', 'type': 'text', 'label': 'URL', 'required': True},
            {'name': 'into_var', 'type': 'text', 'label': 'Store into var',
             'default': 'body'},
            {'name': 'max_bytes', 'type': 'number', 'label': 'Max response',
             'default': 1048576},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.log': {
        'label': 'Log Message', 'category': 'action',
        'icon': 'fa-comment', 'color': '#8a9bc0',
        'description': 'Adds a custom message to the execution log.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'level', 'type': 'select', 'label': 'Level',
             'options': ['info', 'success', 'warning', 'error'],
             'default': 'info'},
            {'name': 'message', 'type': 'text', 'label': 'Message',
             'default': 'Processing {filename}'},
        ],
        'advanced_fields': [],
    },
    'logic.condition': {
        'label': 'Condition', 'category': 'logic',
        'icon': 'fa-code-branch', 'color': '#a78bfa',
        'description': 'Routes to "true" or "false" port.',
        'ports': {'in': ['in'], 'out': ['true', 'false']},
        'fields': [
            {'name': 'expression', 'type': 'text', 'label': 'Expression',
             'required': True, 'default': 'size > 1000000'},
        ],
        'advanced_fields': [],
    },
    'logic.switch': {
        'label': 'Switch', 'category': 'logic',
        'icon': 'fa-sitemap', 'color': '#a78bfa',
        'description': 'Routes to a matching case port, or "default".',
        'ports': {'in': ['in'], 'out': ['1', '2', '3', 'default']},
        'fields': [
            {'name': 'expression', 'type': 'text', 'label': 'Expression',
             'required': True, 'default': 'ext'},
            {'name': 'cases', 'type': 'text', 'label': 'Cases (comma-separated)',
             'default': 'mp4,mkv,webm'},
        ],
        'advanced_fields': [],
    },
    'logic.merge': {
        'label': 'Merge', 'category': 'logic',
        'icon': 'fa-code-merge', 'color': '#a78bfa',
        'description': 'Joins multiple branches back into one path.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'mode', 'type': 'select', 'label': 'Mode',
             'options': ['all', 'any'], 'default': 'all'},
        ],
        'advanced_fields': [],
    },
    'logic.fork': {
        'label': 'Fork', 'category': 'logic',
        'icon': 'fa-code', 'color': '#a78bfa',
        'description': 'Fires all output ports (a, b, c) sequentially.',
        'ports': {'in': ['in'], 'out': ['a', 'b', 'c']},
        'fields': [],
        'advanced_fields': [],
    },
    'logic.delay_until': {
        'label': 'Delay Until', 'category': 'logic',
        'icon': 'fa-hourglass-half', 'color': '#8a9bc0',
        'description': 'Waits until a specific timestamp.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'target_time', 'type': 'text', 'label': 'Target time',
             'required': True, 'placeholder': 'HH:MM or ISO timestamp'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'data.transform': {
        'label': 'Transform', 'category': 'data',
        'icon': 'fa-wand-magic-sparkles', 'color': '#4ade80',
        'description': 'Modify a context value with a Python expression.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'target', 'type': 'text', 'label': 'Target key',
             'required': True, 'default': 'filename'},
            {'name': 'expression', 'type': 'text', 'label': 'Value',
             'required': True, 'default': 'filename.lower()'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'data.set_variable': {
        'label': 'Set Variable', 'category': 'data',
        'icon': 'fa-tag', 'color': '#4ade80',
        'description': 'Sets a custom variable as {vars.name}.',
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
# Safe eval + ctx helpers
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
    def safe(v):
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v if not isinstance(v, str) or len(v) < 500 else v[:500] + '…'
        if isinstance(v, dict):
            return {str(k): safe(val) for k, val in list(v.items())[:30]}
        if isinstance(v, (list, tuple)):
            return [safe(x) for x in v[:30]]
        return str(v)[:200]
    return {str(k): safe(v) for k, v in ctx.items() if not str(k).startswith('_')}


def _checkpoint_ctx(ctx):
    out = {}
    for k, v in ctx.items():
        key = str(k)
        if key.startswith('_') and key != '_depth':
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            if isinstance(v, str) and len(v) > 5000:
                out[key] = v[:5000] + '…'
            else:
                out[key] = v
        elif isinstance(v, dict):
            trimmed = {}
            for kk, vv in list(v.items())[:100]:
                if isinstance(vv, str) and len(vv) > 2000:
                    trimmed[kk] = vv[:2000] + '…'
                elif isinstance(vv, (str, int, float, bool)) or vv is None:
                    trimmed[kk] = vv
                elif isinstance(vv, (list, dict)):
                    try:
                        if len(json.dumps(vv)) < 5000:
                            trimmed[kk] = vv
                        else:
                            trimmed[kk] = '…(large)…'
                    except Exception:
                        trimmed[kk] = str(vv)[:200]
                else:
                    trimmed[kk] = str(vv)[:200]
            out[key] = trimmed
        elif isinstance(v, (list, tuple)):
            out[key] = [
                (x[:2000] + '…' if isinstance(x, str) and len(x) > 2000
                 else x if isinstance(x, (str, int, float, bool)) or x is None
                 else str(x)[:200])
                for x in list(v)[:100]
            ]
        else:
            out[key] = str(v)[:500]

    try:
        if len(json.dumps(out)) > 50000:
            out.pop('vars', None)
    except Exception:
        pass

    return out


def _workflow_signature(workflow):
    if not workflow:
        return ''
    nodes = workflow.get('nodes') or []
    edges = workflow.get('edges') or []
    node_parts = sorted(
        f"{n.get('id','')}:{n.get('type','')}" for n in nodes
    )
    edge_parts = sorted(
        f"{e.get('from','')}:{e.get('from_port','out')}->{e.get('to','')}"
        for e in edges
    )
    blob = '|'.join(node_parts) + '||' + '|'.join(edge_parts)
    return hashlib.md5(blob.encode('utf-8')).hexdigest()


# ============================================================
# Live-update payloads (SSE)
# ============================================================
def _task_version(task):
    """Compact fingerprint — changes only when meaningful data changes."""
    logs = task.get('logs') or []
    last_log_ts = logs[-1].get('ts', 0) if logs else 0
    parts = [
        str(task.get('status', '')),
        str(task.get('progress', 0)),
        str(task.get('current_item_index', 0)),
        str(task.get('current_node_id', '') or ''),
        str(task.get('failed_item_index', '') or ''),
        str(task.get('resume_attempts', 0)),
        str(last_log_ts),
        str(len(logs)),
        json.dumps(task.get('node_statuses') or {}, sort_keys=True),
        json.dumps(task.get('awaiting_input') or None, sort_keys=True),
    ]
    return hashlib.md5('|'.join(parts).encode('utf-8')).hexdigest()


def _lite_payload(task):
    """~2–5 KB payload for the SSE stream."""
    cps = task.get('checkpoints') or {}
    completed_count = sum(
        1 for cp in cps.values()
        if isinstance(cp, dict) and cp.get('completed')
    )
    return {
        'task_id': task.get('task_id'),
        'status': task.get('status'),
        'progress': int(task.get('progress', 0) or 0),
        'current_item_index': task.get('current_item_index', 0),
        'current_item': task.get('current_item', ''),
        'total_items': task.get('total_items', 0),
        'current_node_id': task.get('current_node_id'),
        'node_statuses': task.get('node_statuses') or {},
        'logs': (task.get('logs') or [])[-50:],
        'awaiting_input': task.get('awaiting_input'),
        'failed_item_index': task.get('failed_item_index'),
        'resume_attempts': task.get('resume_attempts', 0),
        'dry_run': task.get('dry_run', False),
        'completed_count': completed_count,
        'cancelled': task.get('cancelled', False),
        'error_msg': task.get('error_msg'),
    }


# ============================================================
# Graph helpers
# ============================================================
def _norm_edge(e):
    if 'from_port' not in e or not e.get('from_port'):
        e['from_port'] = 'out'
    return e


def _node_ports(node_type):
    return NODE_TYPES.get(node_type, {}).get(
        'ports', {'in': ['in'], 'out': ['out']})


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
    q = [nid for nid, d in indeg.items() if d == 0]
    order = []
    while q:
        nid = q.pop(0)
        order.append(nid)
        for nxt in adj[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                q.append(nxt)
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


def _build_initial_worklist_from_checkpoint(trigger, edges, node_map,
                                              last_node_id, last_ports_fired):
    incoming_count = {nid: 0 for nid in node_map}
    expected_count = {nid: len(_in_edges(nid, edges)) for nid in node_map}
    completed = {trigger['id'], last_node_id}
    worklist = []

    upstream = {last_node_id}
    stack = [last_node_id]
    while stack:
        cur = stack.pop()
        for e in _in_edges(cur, edges):
            if e['from'] not in upstream:
                upstream.add(e['from'])
                stack.append(e['from'])
    completed.update(upstream)

    for port in (last_ports_fired or ['out']):
        for edge in _out_edges(last_node_id, edges, port):
            to_id = edge['to']
            if to_id in completed:
                continue
            incoming_count[to_id] = incoming_count.get(to_id, 0) + 1
            exp = expected_count.get(to_id, 0)
            if exp == 0 or incoming_count[to_id] >= exp:
                worklist.append(to_id)
            elif (node_map[to_id]['type'] == 'logic.merge' and
                  (node_map[to_id].get('config') or {}).get('mode', 'all').lower() == 'any'):
                worklist.append(to_id)

    return incoming_count, expected_count, completed, worklist


# ============================================================
# Progress / logging helpers (unchanged)
# ============================================================
_last_save = {}
_last_save_lock = threading.Lock()


def _update_node_progress(task_id, node_id, progress, status=None):
    if not node_id:
        return
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
    return task.get('_abort')


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
# Small helpers
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
# Feature API bridge
# ============================================================
def _make_subtask(parent_task_id, node_id):
    sub_id = str(uuid.uuid4())
    save_task(sub_id, {
        'task_id': sub_id, 'status': 'queued',
        'created_at': time.time(), 'cancelled': False,
        '_hidden': True,
        'parent_workflow': parent_task_id,
        'parent_node_id': node_id,
    })
    return sub_id


def _kill_subtask(sub_task_id):
    try:
        from features.url_download import kill_process
        kill_process(sub_task_id)
    except Exception:
        pass
    try:
        from features.telegram import cancel_telegram_task
        cancel_telegram_task(sub_task_id)
    except Exception:
        pass
    try:
        delete_task(sub_task_id)
    except Exception:
        pass


def _poll_subtask(parent_task_id, node_id, sub_task_id,
                  progress_key='progress', timeout=4 * 3600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _should_abort(parent_task_id):
            _kill_subtask(sub_task_id)
            raise _Cancelled()

        sub = load_task(sub_task_id)
        if not sub:
            time.sleep(0.3)
            continue

        status = sub.get('status')
        if status == 'done':
            return sub
        if status in ('error', 'cancelled'):
            raise Exception(sub.get('error_msg') or f'Sub-task {status}')

        _update_node_progress(parent_task_id, node_id,
                               sub.get(progress_key, 0) or 0)
        time.sleep(0.5)

    _kill_subtask(sub_task_id)
    raise Exception(f'Operation timed out after {timeout}s')


def _extract_file_ctx(out_file):
    full_path = os.path.join(UPLOAD_FOLDER, out_file)
    size = os.path.getsize(full_path) if os.path.exists(full_path) else 0
    return {
        'file_path': full_path,
        'filename': out_file,
        'basename': os.path.splitext(out_file)[0],
        'size': size,
    }


def _api_download_single(parent_task_id, node_id, url, quality='best',
                          title_based=True):
    if not url:
        raise Exception('No URL provided')
    from features.url_download import download_with_ytdlp

    sub_id = _make_subtask(parent_task_id, node_id)

    def _run():
        try:
            download_with_ytdlp(url, sub_id, quality, title_based=title_based)
        except Exception as e:
            t = load_task(sub_id)
            if t:
                t['status'] = 'error'
                t['error_msg'] = str(e)
                save_task(sub_id, t)

    threading.Thread(target=_run, daemon=True).start()
    try:
        sub = _poll_subtask(parent_task_id, node_id, sub_id,
                             progress_key='download_progress')
        out_file = sub.get('output_file')
        if not out_file:
            raise Exception('Download finished but no output_file set')
        return _extract_file_ctx(out_file)
    finally:
        try:
            delete_task(sub_id)
        except Exception:
            pass


def _api_download_file(parent_task_id, node_id, url):
    if not url:
        raise Exception('No URL provided')
    from features.url_download import download_direct_file

    sub_id = _make_subtask(parent_task_id, node_id)

    def _run():
        try:
            download_direct_file(url, sub_id)
        except Exception as e:
            t = load_task(sub_id)
            if t:
                t['status'] = 'error'
                t['error_msg'] = str(e)
                save_task(sub_id, t)

    threading.Thread(target=_run, daemon=True).start()
    try:
        sub = _poll_subtask(parent_task_id, node_id, sub_id,
                             progress_key='download_progress')
        out_file = sub.get('output_file')
        if not out_file:
            raise Exception('File download finished but no output_file')
        return _extract_file_ctx(out_file)
    finally:
        try:
            delete_task(sub_id)
        except Exception:
            pass


def _api_compress(parent_task_id, node_id, file_path,
                   codec='x265', resolution=720, crf=27, preset='veryfast'):
    if not file_path or not os.path.exists(file_path):
        raise Exception(f'File not found: {file_path}')
    from features.video_clipper import compress_video_file

    base = os.path.splitext(os.path.basename(file_path))[0]
    stem = f'{base}_{codec}'
    out_name = f'{stem}.mp4'
    i = 1
    while os.path.exists(os.path.join(UPLOAD_FOLDER, out_name)):
        out_name = f'{stem}_{i}.mp4'
        i += 1
    out_path = os.path.join(UPLOAD_FOLDER, out_name)

    compress_video_file(file_path, out_path, parent_task_id,
                         codec=codec, resolution=resolution,
                         crf=crf, preset=preset)

    if not os.path.exists(out_path):
        raise Exception('Compression produced no output')
    return {
        'original_file_path': file_path,
        **_extract_file_ctx(out_name),
    }


def _api_telegram(parent_task_id, node_id, file_path, chat_link, caption=None):
    if not file_path or not os.path.exists(file_path):
        raise Exception(f'File not found: {file_path}')
    if not chat_link:
        raise Exception('Chat link required')
    from features.telegram import (
        _run_telegram_job, send_file_to_telegram_async,
        start_tg_loop_if_needed,
    )

    start_tg_loop_if_needed()
    sub_id = _make_subtask(parent_task_id, node_id)
    t = load_task(sub_id)
    if t:
        t['upload_progress'] = 0
        save_task(sub_id, t)

    def _run():
        try:
            _run_telegram_job(send_file_to_telegram_async, sub_id,
                               chat_link, file_path,
                               os.path.basename(file_path), caption)
        except Exception as e:
            t = load_task(sub_id)
            if t:
                t['status'] = 'error'
                t['error_msg'] = str(e)
                save_task(sub_id, t)

    threading.Thread(target=_run, daemon=True).start()
    try:
        _poll_subtask(parent_task_id, node_id, sub_id,
                       progress_key='upload_progress', timeout=3600)
    finally:
        try:
            delete_task(sub_id)
        except Exception:
            pass


def _api_drive_upload(parent_task_id, node_id, file_path, account='default'):
    if not file_path or not os.path.exists(file_path):
        raise Exception(f'File not found: {file_path}')
    from features.google_drive import upload_to_colab_processing
    response = upload_to_colab_processing(
        file_path, account=account,
        task_id=parent_task_id, node_id=node_id,
    )
    return {
        'drive_file_id': response.get('id'),
        'drive_link': response.get('webViewLink'),
    }


def _api_drive_delete(parent_task_id, node_id, file_id, account='default'):
    if not file_id:
        raise Exception('No drive_file_id in context')
    from features.google_drive import get_drive_service
    service = get_drive_service(account)
    service.files().delete(fileId=file_id).execute()


def _api_extract_metadata(parent_task_id, node_id, file_path):
    if not file_path or not os.path.exists(file_path):
        raise Exception(f'File not found: {file_path}')
    ffprobe = shutil.which('ffprobe') or 'ffprobe'
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', file_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise Exception('ffprobe returned non-zero')
        data = json.loads(r.stdout or '{}')
    except Exception as e:
        raise Exception(f'Metadata extraction failed: {e}')

    fmt = data.get('format', {})
    streams = data.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    a = next((s for s in streams if s.get('codec_type') == 'audio'), {})

    try:
        dur = float(fmt.get('duration', 0) or 0)
    except (TypeError, ValueError):
        dur = 0

    return {
        'duration': dur,
        'width': int(v.get('width', 0) or 0),
        'height': int(v.get('height', 0) or 0),
        'vcodec': v.get('codec_name', ''),
        'acodec': a.get('codec_name', ''),
    }


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
# Node handlers
# ============================================================
def _h_download(task_id, node_id, config, ctx):
    updates = _api_download_single(
        task_id, node_id, ctx.get('url'),
        quality=ctx.get('quality', 'best'),
        title_based=ctx.get('title_based', True),
    )
    ctx.update(updates)
    _log(task_id, node_id, 'success',
         f'Downloaded {ctx["filename"]} ({_format_size(ctx["size"])})')
    return ['out']


def _h_download_file(task_id, node_id, config, ctx):
    updates = _api_download_file(task_id, node_id, ctx.get('url'))
    ctx.update(updates)
    _log(task_id, node_id, 'success',
         f'Downloaded {ctx["filename"]} ({_format_size(ctx["size"])})')
    return ['out']


def _h_telegram_send(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path:
        ctx_keys = sorted(k for k in ctx.keys() if not k.startswith('_'))
        raise Exception(
            f'No file_path in context. Context keys: {ctx_keys}. '
            f'Check that the previous Download node succeeded.'
        )
    if not os.path.exists(file_path):
        raise Exception(
            f'file_path ({file_path}) does not exist on disk. '
            f'Move Delete Local File to the end of the chain.'
        )
    chat_link = (config.get('chat_link') or '').strip()
    if not chat_link:
        raise Exception('Chat link required')
    caption = _expand_template(config.get('caption') or '', ctx) or None
    _api_telegram(task_id, node_id, file_path, chat_link, caption)
    _log(task_id, node_id, 'success', f'Sent to {chat_link}')
    return ['out']


def _h_drive_upload(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file in context')
    account = (config.get('account') or 'default').strip()
    updates = _api_drive_upload(task_id, node_id, file_path, account)
    ctx.update(updates)
    _log(task_id, node_id, 'success',
         f'Uploaded to Drive ({account}): {os.path.basename(file_path)}')
    return ['out']


def _h_compress(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file to compress')
    orig_size = os.path.getsize(file_path)
    try:
        res = int(config.get('resolution') or 720)
    except (TypeError, ValueError):
        res = 720
    try:
        crf = int(config.get('crf') or 27)
    except (TypeError, ValueError):
        crf = 27
    updates = _api_compress(
        task_id, node_id, file_path,
        codec=(config.get('codec') or 'x265').lower(),
        resolution=res, crf=crf,
        preset=config.get('preset') or 'veryfast',
    )
    ctx.update(updates)
    new_size = ctx.get('size', 0)
    savings = ((1 - new_size / orig_size) * 100) if orig_size > 0 else 0
    _log(task_id, node_id, 'success',
         f'Compressed: {_format_size(orig_size)} → '
         f'{_format_size(new_size)} ({savings:.0f}% smaller)')
    return ['out']


def _h_delete_local(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    original = ctx.get('original_file_path')
    if not file_path:
        _log(task_id, node_id, 'warning', 'No file in context — skipping')
        return ['out']
    name = os.path.basename(file_path)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            raise Exception(f'Delete failed: {e}')
    if original and original != file_path and os.path.exists(original):
        try:
            os.remove(original)
        except Exception:
            pass
    _log(task_id, node_id, 'success', f'Deleted {name}')
    for k in ('file_path', 'filename', 'basename', 'size', 'original_file_path'):
        ctx.pop(k, None)
    return ['out']


def _h_delete_drive(task_id, node_id, config, ctx):
    file_id = ctx.get('drive_file_id')
    if not file_id:
        _log(task_id, node_id, 'warning', 'No Drive file in context — skipping')
        return ['out']
    account = (config.get('account') or 'default').strip()
    try:
        _api_drive_delete(task_id, node_id, file_id, account)
        _log(task_id, node_id, 'success', f'Deleted Drive file ({account})')
    except Exception as e:
        _log(task_id, node_id, 'warning', f'Drive delete failed: {e}')
    ctx.pop('drive_file_id', None)
    ctx.pop('drive_link', None)
    return ['out']


def _h_extract_metadata(task_id, node_id, config, ctx):
    file_path = ctx.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise Exception('No file in context')
    updates = _api_extract_metadata(task_id, node_id, file_path)
    ctx.update(updates)
    _log(task_id, node_id, 'success',
         f'{ctx["width"]}x{ctx["height"]} · {ctx["duration"]:.1f}s')
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


def _h_condition(task_id, node_id, config, ctx):
    expr = (config.get('expression') or '').strip()
    if not expr:
        raise Exception('Condition expression empty')
    result = _safe_eval(expr, _ctx_to_dict(ctx))
    fired = 'true' if bool(result) else 'false'
    _log(task_id, node_id, 'info', f'Condition → {fired}')
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
    _log(task_id, node_id, 'info',
         f'Merge ({(config.get("mode") or "all").lower()})')
    return ['out']


def _h_fork(task_id, node_id, config, ctx):
    _log(task_id, node_id, 'info', 'Fork → a, b, c')
    return ['a', 'b', 'c']


def _h_delay_until(task_id, node_id, config, ctx):
    from datetime import datetime, timedelta
    target_str = (config.get('target_time') or '').strip()
    if not target_str:
        raise Exception('Target time required')
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
        ctx.setdefault('vars', {})[target[5:]] = value
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
        ctx['vars'][into] = data
    else:
        ctx['vars'][into] = data
    n = len(data) if hasattr(data, '__len__') else 0
    _log(task_id, node_id, 'success', f'Parsed {n} item(s) into vars.{into}')
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


def _h_sub_workflow(task_id, node_id, config, ctx):
    wf_id = (config.get('workflow_id') or '').strip()
    if not wf_id:
        raise Exception('Workflow ID required')
    depth = int(ctx.get('_depth', 0))
    if depth >= MAX_SUBWORKFLOW_DEPTH:
        raise Exception(f'Sub-workflow depth limit ({MAX_SUBWORKFLOW_DEPTH})')
    wf = _load_workflow(wf_id)
    if not wf:
        raise Exception(f'Workflow not found: {wf_id}')
    inherit = config.get('inherit_context', True)
    ret_ctx = config.get('return_context', True)
    if inherit:
        sub_ctx = dict(ctx)
    else:
        sub_ctx = {
            'url': ctx.get('url', ''),
            'index': ctx.get('index', 0),
            'total': ctx.get('total', 0),
            'quality': ctx.get('quality', 'best'),
            'vars': dict(ctx.get('vars', {})),
        }
    sub_ctx['_depth'] = depth + 1
    _log(task_id, node_id, 'info',
         f'Running sub-workflow "{wf.get("name", wf_id)}"')
    try:
        final_ctx = _run_workflow_sync(wf, sub_ctx, parent_task_id=task_id)
    except Exception as e:
        raise Exception(f'Sub-workflow failed: {e}')
    if ret_ctx and final_ctx:
        for k, v in final_ctx.items():
            if str(k).startswith('_') or k in ('item', 'index', 'total', 'quality'):
                continue
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
    task = load_task(task_id)
    task['awaiting_input'] = {
        'node_id': node_id, 'message': message,
        'created_at': time.time(), 'expires_at': time.time() + timeout,
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
            _log(task_id, node_id, 'warning', '❌ Rejected')
            return ['rejected']
        time.sleep(0.5)
    task = load_task(task_id)
    task['awaiting_input'] = None
    save_task(task_id, task)
    _log(task_id, node_id, 'warning', '⏱ Approval timed out')
    return ['timeout']


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
            return f'Edge {i}: node {f[:8]} has no output port "{from_port}"'

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
    return None


# ============================================================
# Node execution
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
        raise Exception('Trigger node cannot be executed directly')
    handler = NODE_HANDLERS.get(node_type)
    if not handler:
        raise Exception(f'No handler for {node_type}')

    skip_expr = (node_config.get('skip_if') or '').strip()
    if skip_expr:
        try:
            if _safe_eval(skip_expr, _ctx_to_dict(ctx)):
                _log(task_id, node['id'], 'info', f'Skipped ({skip_expr[:60]})')
                return ['out']
        except Exception as e:
            _log(task_id, node['id'], 'warning',
                 f'skip_if error: {e} — running anyway')

    requires_file = {
        'action.telegram_send', 'action.drive_upload', 'action.compress',
        'action.delete_local', 'action.extract_metadata',
    }
    if node_type in requires_file:
        fp = ctx.get('file_path')
        if not fp or not os.path.exists(fp):
            ctx_keys = sorted(k for k in ctx.keys() if not k.startswith('_'))
            raise Exception(
                f'{NODE_TYPES[node_type]["label"]} needs a file in context, '
                f'but none is present. Context keys: {ctx_keys}. '
                f'Upstream Download must succeed.'
            )

    if dry_run and node_type.startswith(('action.', 'data.')):
        _log(task_id, node['id'], 'info',
             f'[DRY RUN] Would run {NODE_TYPES[node_type]["label"]}')
        ports = _node_ports(node_type)['out']
        if 'out' in ports:
            return ['out']
        return [ports[0]] if ports else ['out']

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
            if _abort_reason(task_id) == 'timeout':
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

    _update_node_progress(task_id, node['id'], 100, 'error')
    _record_timing(task_id, node['id'], (time.time() - start_time) * 1000)
    _record_context_snapshot(task_id, node['id'], ctx)

    if on_error == 'continue':
        _log(task_id, node['id'], 'error',
             f'Failed (continuing): {last_err}')
        return ['out']
    raise last_err


# ============================================================
# Trigger execution
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
        pattern = cfg.get('pattern') or '*.mp4'
        try:
            max_items = int(cfg.get('max_items', 0) or 0)
        except (TypeError, ValueError):
            max_items = 0
        matches = sorted(_glob.glob(os.path.join(UPLOAD_FOLDER, pattern),
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
# Execution engine
# ============================================================
def _build_initial_worklist(trigger, edges, node_map):
    incoming_count = {nid: 0 for nid in node_map}
    expected_count = {nid: len(_in_edges(nid, edges)) for nid in node_map}
    completed = {trigger['id']}
    worklist = []

    for edge in _out_edges(trigger['id'], edges, 'out'):
        to_id = edge['to']
        if to_id in completed:
            continue
        incoming_count[to_id] = incoming_count.get(to_id, 0) + 1
        exp = expected_count.get(to_id, 0)
        if exp == 0 or incoming_count[to_id] >= exp:
            worklist.append(to_id)
        elif (node_map[to_id]['type'] == 'logic.merge' and
              (node_map[to_id].get('config') or {}).get('mode', 'all').lower() == 'any'):
            worklist.append(to_id)

    return incoming_count, expected_count, completed, worklist


def _fire_port(node_id, port, edges, node_map, incoming_count, expected_count,
                completed, worklist):
    for edge in _out_edges(node_id, edges, port):
        to_id = edge['to']
        if to_id in completed:
            continue
        incoming_count[to_id] = incoming_count.get(to_id, 0) + 1
        exp = expected_count.get(to_id, 0)
        if exp == 0 or incoming_count[to_id] >= exp:
            worklist.append(to_id)
        elif (node_map[to_id]['type'] == 'logic.merge' and
              (node_map[to_id].get('config') or {}).get('mode', 'all').lower() == 'any'):
            worklist.append(to_id)


def _run_workflow_sync(workflow, initial_ctx, parent_task_id=None):
    nodes = workflow['nodes']
    edges = workflow.get('edges', [])
    node_map = {n['id']: n for n in nodes}
    if _topological_order(nodes, edges) is None:
        raise Exception('Cycle in sub-workflow')
    trigger = next((n for n in nodes if n['type'].startswith('trigger.')), None)
    if not trigger:
        raise Exception('Sub-workflow has no trigger')

    items = _prepare_items(parent_task_id or 'sub', trigger, edges)
    if not items:
        raise Exception('Sub-workflow trigger produced no items')
    item = items[0]

    ctx = dict(initial_ctx)
    ctx['item'] = item
    ctx['url'] = str(item)
    ctx['index'] = 1
    ctx['total'] = 1
    ctx['quality'] = (trigger.get('config') or {}).get('quality', 'best')
    ctx.setdefault('vars', {})
    ctx['_depth'] = int(initial_ctx.get('_depth', 0))

    incoming_count, expected_count, completed, worklist = \
        _build_initial_worklist(trigger, edges, node_map)

    while worklist:
        if _should_abort(parent_task_id or 'sub'):
            raise _Cancelled()
        node_id = worklist.pop(0)
        if node_id in completed:
            continue
        node = node_map[node_id]
        node_cfg = node.get('config') or {}
        if (expected_count.get(node_id, 0) > 0 and
                incoming_count.get(node_id, 0) < expected_count.get(node_id, 0)):
            if not (node['type'] == 'logic.merge' and
                    (node_cfg.get('mode') or 'all').lower() == 'any'):
                continue
        ports = _execute_node(parent_task_id or 'sub', node, node_cfg, ctx,
                              dry_run=False)
        completed.add(node_id)
        for port in ports:
            _fire_port(node_id, port, edges, node_map, incoming_count,
                       expected_count, completed, worklist)
    return ctx


def _run_workflow(task_id, workflow, dry_run=False, items_override=None):
    nodes = workflow['nodes']
    edges = workflow.get('edges', [])
    node_map = {n['id']: n for n in nodes}

    if _topological_order(nodes, edges) is None:
        _log(task_id, None, 'error', 'Cycle detected')
        raise Exception('Invalid workflow graph')

    trigger = next((n for n in nodes if n['type'].startswith('trigger.')), None)
    if not trigger:
        raise Exception('No trigger node')

    try:
        if items_override is not None:
            items = list(items_override)
            _log(task_id, None, 'info',
                 f'Resuming with {len(items)} stored item(s)')
        else:
            items = _prepare_items(task_id, trigger, edges)
            if not items:
                raise Exception('Trigger produced no items')

        task = load_task(task_id)
        task['items'] = [str(x)[:200] for x in items]
        task['total_items'] = len(items)
        task['workflow_snapshot'] = workflow
        task['workflow_id'] = workflow.get('id')
        task['workflow_signature'] = _workflow_signature(workflow)
        save_task(task_id, task)

        task = load_task(task_id)
        checkpoints = dict(task.get('checkpoints') or {})

        already_done = sum(1 for cp in checkpoints.values()
                            if cp.get('completed'))
        if already_done:
            _log(task_id, None, 'info',
                 f'{already_done}/{len(items)} item(s) already completed')

        mode_label = '[DRY RUN] ' if dry_run else ''
        _log(task_id, None, 'info',
             f'{mode_label}Starting "{workflow.get("name", "Untitled")}" — '
             f'{len(items)} item(s), {len(nodes) - 1} action node(s)')

        trigger_cfg = trigger.get('config') or {}

        for idx, item in enumerate(items):
            if _should_abort(task_id):
                raise _Cancelled()

            item_key = str(idx)
            prev_cp = checkpoints.get(item_key)

            if prev_cp and prev_cp.get('completed'):
                task = load_task(task_id)
                task['current_item_index'] = idx
                task['current_item'] = str(item)[:200]
                task['progress'] = int(100 * (idx + 1) / len(items))
                task['node_statuses'] = {
                    trigger['id']: {'status': 'done', 'progress': 100},
                }
                save_task(task_id, task)
                _log(task_id, None, 'info',
                     f'⏭ Item {idx + 1}/{len(items)} already completed')
                continue

            task = load_task(task_id)
            task['current_item_index'] = idx
            task['current_item'] = str(item)[:200]
            task['progress'] = int(100 * idx / len(items))
            task['node_statuses'] = {
                trigger['id']: {'status': 'done', 'progress': 100},
            }
            save_task(task_id, task)

            if prev_cp and prev_cp.get('last_node_id'):
                last_node_id = prev_cp['last_node_id']
                last_ports = prev_cp.get('last_ports_fired') or ['out']
                base_ctx = prev_cp.get('last_ctx') or {}

                ctx = dict(base_ctx)
                ctx['item'] = item
                ctx['url'] = str(item)
                ctx['index'] = idx + 1
                ctx['total'] = len(items)
                ctx.setdefault('vars', {})
                ctx['_depth'] = int(ctx.get('_depth', 0))

                _log(task_id, None, 'info',
                     f'▶ Item {idx + 1}/{len(items)}: {str(item)[:100]} '
                     f'(↩ resuming from node {last_node_id[:8]})')

                incoming_count, expected_count, completed, worklist = \
                    _build_initial_worklist_from_checkpoint(
                        trigger, edges, node_map, last_node_id, last_ports)
            else:
                ctx = {
                    'item': item,
                    'url': str(item),
                    'index': idx + 1,
                    'total': len(items),
                    'quality': trigger_cfg.get('quality', 'best'),
                    'title_based': trigger_cfg.get('title_based_naming', True),
                    'vars': {},
                    '_depth': 0,
                }
                _log(task_id, None, 'info',
                     f'▶ Item {idx + 1}/{len(items)}: {str(item)[:100]}')

                incoming_count, expected_count, completed, worklist = \
                    _build_initial_worklist(trigger, edges, node_map)

            while worklist:
                if _should_abort(task_id):
                    raise _Cancelled()

                node_id = worklist.pop(0)
                if node_id in completed:
                    continue

                node = node_map[node_id]
                node_cfg = node.get('config') or {}
                if (expected_count.get(node_id, 0) > 0 and
                        incoming_count.get(node_id, 0) <
                        expected_count.get(node_id, 0)):
                    if not (node['type'] == 'logic.merge' and
                            (node_cfg.get('mode') or 'all').lower() == 'any'):
                        continue

                task = load_task(task_id)
                task['current_node_id'] = node_id
                task['node_statuses'][node_id] = {'status': 'running',
                                                    'progress': 0}
                save_task(task_id, task)
                _log(task_id, node_id, 'info',
                     f'Running {NODE_TYPES[node["type"]]["label"]}')

                try:
                    fired_ports = _execute_node(task_id, node, node_cfg, ctx,
                                                 dry_run=dry_run)
                except _TimedOut:
                    raise Exception(
                        f'{NODE_TYPES[node["type"]]["label"]}: timed out')
                except _Cancelled:
                    raise
                except Exception as e:
                    logger.exception(f'Node {node_id} failed')
                    raise Exception(
                        f'{NODE_TYPES[node["type"]]["label"]}: {e}')

                completed.add(node_id)

                task = load_task(task_id)
                cps = task.get('checkpoints', {})
                cps[item_key] = {
                    'last_node_id': node_id,
                    'last_ports_fired': list(fired_ports),
                    'last_ctx': _checkpoint_ctx(ctx),
                    'completed_nodes': list(completed),
                    'ts': time.time(),
                }
                task['checkpoints'] = cps
                save_task(task_id, task)

                for port in fired_ports:
                    _fire_port(node_id, port, edges, node_map,
                               incoming_count, expected_count,
                               completed, worklist)

            task = load_task(task_id)
            cps = task.get('checkpoints', {})
            cps[item_key] = {'completed': True, 'ts': time.time()}
            task['checkpoints'] = cps
            save_task(task_id, task)

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
            task['failed_item_index'] = task.get('current_item_index', 0)
            task['workflow_snapshot'] = workflow
            task['workflow_id'] = workflow.get('id')
            task['workflow_signature'] = _workflow_signature(workflow)
            save_task(task_id, task)
        _log(task_id, None, 'warning', '⛔ Cancelled')
    except Exception as e:
        logger.exception(f'Workflow {task_id} failed')
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            task['failed_item_index'] = task.get('current_item_index', 0)
            task['workflow_snapshot'] = workflow
            task['workflow_id'] = workflow.get('id')
            task['workflow_signature'] = _workflow_signature(workflow)
            save_task(task_id, task)
        _log(task_id, None, 'error', f'❌ {e}')


# ============================================================
# Routes
# ============================================================
def register_routes(app):

    # ---------- SSE stream (replaces polling) ----------
    @app.route('/workflow/stream/<task_id>')
    def workflow_stream(task_id):
        def event_gen():
            q = queue.Queue(maxsize=5)
            add_subscriber(q)
            try:
                task = load_task(task_id)
                if not task:
                    yield ('event: error\n'
                           f'data: {json.dumps({"error": "Task not found"})}\n\n')
                    return

                last_version = None
                payload = _lite_payload(task)
                last_version = _task_version(task)
                yield f'data: {json.dumps(payload)}\n\n'

                if task.get('status') in ('done', 'error', 'cancelled'):
                    yield (f'event: close\ndata: {json.dumps(payload)}\n\n')
                    return

                heartbeat_counter = 0

                while True:
                    try:
                        # Wait for a broadcast from save_task()
                        raw = q.get(timeout=SSE_HEARTBEAT_SEC)
                        heartbeat_counter = 0

                        # Find our specific task in the broadcast
                        our_task = None
                        try:
                            tasks_data = json.loads(raw)
                            if isinstance(tasks_data, list):
                                for td in tasks_data:
                                    if td.get('task_id') == task_id:
                                        our_task = td
                                        break
                            elif isinstance(tasks_data, dict):
                                if tasks_data.get('task_id') == task_id:
                                    our_task = tasks_data
                        except Exception:
                            pass

                        # Fallback: read from disk
                        if our_task is None:
                            our_task = load_task(task_id)
                        if not our_task:
                            yield ('event: error\n'
                                   f'data: {json.dumps({"error": "Task removed"})}\n\n')
                            return

                        version = _task_version(our_task)
                        if version != last_version:
                            last_version = version
                            payload = _lite_payload(our_task)
                            yield f'data: {json.dumps(payload)}\n\n'

                            if our_task.get('status') in ('done', 'error', 'cancelled'):
                                yield (f'event: close\n'
                                       f'data: {json.dumps(payload)}\n\n')
                                return

                    except queue.Empty:
                        heartbeat_counter += 1
                        # Verify task still exists every heartbeat
                        current = load_task(task_id)
                        if not current:
                            yield (': heartbeat\n\n')
                            return
                        if current.get('status') in ('done', 'error', 'cancelled'):
                            payload = _lite_payload(current)
                            yield (f'event: close\n'
                                   f'data: {json.dumps(payload)}\n\n')
                            return
                        yield ': heartbeat\n\n'
            finally:
                remove_subscriber(q)

        return Response(
            stream_with_context(event_gen()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache, no-transform',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
        )

    # ---------- Lite JSON (fallback if SSE fails) ----------
    @app.route('/workflow/progress_lite/<task_id>')
    def workflow_progress_lite(task_id):
        try:
            task = load_task(task_id)
            if not task:
                return jsonify({'error': 'Task not found'}), 404
            if task.get('type') != 'workflow':
                return jsonify({k: v for k, v in task.items()
                                if k != 'process_pid'})
            return jsonify(_lite_payload(task))
        except Exception as e:
            logger.error(f'progress_lite failed for {task_id}: {e}')
            return jsonify({'error': str(e)}), 500

    # ---------- Rest of routes (unchanged) ----------
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
            headers={'Content-Disposition':
                     f'attachment; filename=workflow_{wf_id}.json'}
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
            'checkpoints': {},
            'workflow_snapshot': data,
            'workflow_id': data.get('id'),
            'workflow_signature': _workflow_signature(data),
            'dry_run': dry_run, 'type': 'workflow',
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
            return jsonify({'error': 'response must be approve or reject'}), 400
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

    @app.route('/workflow/resume/info/<task_id>')
    def workflow_resume_info(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        if task.get('type') != 'workflow':
            return jsonify({'error': 'Not a workflow task'}), 400
        if task.get('_hidden'):
            return jsonify({'error': 'Internal task'}), 400

        workflow = task.get('workflow_snapshot')
        if not workflow:
            return jsonify({
                'resumable': False,
                'reason': 'No workflow snapshot',
            })

        cps = task.get('checkpoints', {})
        completed = sum(1 for cp in cps.values() if cp.get('completed'))
        partial = sum(1 for cp in cps.values()
                      if not cp.get('completed') and cp.get('last_node_id'))

        wf_id = task.get('workflow_id') or workflow.get('id')
        latest_available = bool(wf_id and _load_workflow(wf_id))

        return jsonify({
            'resumable': True,
            'status': task.get('status'),
            'workflow_name': task.get('workflow_name'),
            'workflow_id': wf_id,
            'latest_file_available': latest_available,
            'total_items': task.get('total_items', 0),
            'completed_items': completed,
            'partial_items': partial,
            'failed_item_index': task.get('failed_item_index'),
            'resumed_from': task.get('resumed_from'),
            'resume_attempts': task.get('resume_attempts', 0),
            'error_msg': task.get('error_msg'),
        })

    @app.route('/workflow/resume/<source_task_id>', methods=['POST'])
    def workflow_resume(source_task_id):
        source = load_task(source_task_id)
        if not source:
            return jsonify({'error': 'Source task not found'}), 404
        if source.get('type') != 'workflow':
            return jsonify({'error': 'Not a workflow task'}), 400
        if source.get('_hidden'):
            return jsonify({'error': 'Cannot resume an internal task'}), 400

        source_snapshot = source.get('workflow_snapshot') or {}
        source_sig = (source.get('workflow_signature')
                      or _workflow_signature(source_snapshot))
        wf_id = source.get('workflow_id') or source_snapshot.get('id')

        workflow = None
        latest_source = 'snapshot'
        if wf_id:
            workflow = _load_workflow(wf_id)
            if workflow:
                latest_source = 'file'

        if not workflow:
            workflow = source_snapshot

        if not workflow:
            return jsonify({'error': 'No workflow available'}), 400

        new_sig = _workflow_signature(workflow)
        graph_changed = (new_sig != source_sig)

        items = source.get('items', [])
        if not items:
            return jsonify({'error': 'Source task has no items'}), 400

        checkpoints = source.get('checkpoints', {})
        stats = {'kept_completed': 0, 'kept_partial': 0,
                 'dropped_partial': 0, 'total': len(checkpoints)}

        if graph_changed:
            new_node_ids = {n['id'] for n in workflow.get('nodes', [])}
            filtered = {}
            for k, cp in checkpoints.items():
                if cp.get('completed'):
                    filtered[k] = cp
                    stats['kept_completed'] += 1
                elif cp.get('last_node_id') in new_node_ids:
                    filtered[k] = cp
                    stats['kept_partial'] += 1
                else:
                    stats['dropped_partial'] += 1
            checkpoints = filtered
        else:
            stats['kept_completed'] = sum(
                1 for cp in checkpoints.values() if cp.get('completed'))
            stats['kept_partial'] = (len(checkpoints) - stats['kept_completed'])

        completed = stats['kept_completed']
        attempts = (source.get('resume_attempts') or 0) + 1

        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id, 'status': 'running',
            'progress': int(100 * completed / len(items)) if items else 0,
            'created_at': time.time(), 'cancelled': False,
            'workflow_name': workflow.get('name', 'Untitled'),
            'items': items, 'total_items': len(items),
            'current_item_index': 0, 'current_item': '',
            'current_node_id': None, 'node_statuses': {}, 'logs': [],
            'context_snapshots': {}, 'node_timings': [],
            'dry_run': source.get('dry_run', False), 'type': 'workflow',
            'workflow_snapshot': workflow, 'workflow_id': wf_id,
            'workflow_signature': new_sig,
            'resumed_from': source_task_id,
            'resume_attempts': attempts,
            'checkpoints': checkpoints,
            'graph_changed': graph_changed,
        })

        threading.Thread(
            target=_run_workflow,
            args=(task_id, workflow, source.get('dry_run', False)),
            kwargs={'items_override': items},
            daemon=True,
        ).start()

        return jsonify({
            'task_id': task_id,
            'resumed_from': source_task_id,
            'total_items': len(items),
            'completed_items': completed,
            'resume_attempts': attempts,
            'graph_changed': graph_changed,
            'latest_source': latest_source,
            'checkpoint_stats': stats,
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
            return jsonify({'error': 'Cannot test a trigger node'}), 400
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
            'vars': {}, '_depth': 0,
        }
        sample_ctx.update(data.get('ctx') or {})

        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id, 'status': 'running', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'logs': [], 'node_statuses': {}, 'node_timings': [],
            'context_snapshots': {}, 'dry_run': dry_run,
            '_hidden': True, 'type': 'workflow_test',
        })
        try:
            node = {'id': 'test', 'type': node_type, 'config': node_config}
            ports = _execute_node(task_id, node, node_config, sample_ctx,
                                   dry_run=dry_run)
            t = load_task(task_id)
            logs = t.get('logs', []) if t else []
            delete_task(task_id)
            return jsonify({
                'status': 'ok', 'output_ports': ports,
                'ctx_after': _safe_ctx_snapshot(sample_ctx),
                'logs': logs, 'dry_run': dry_run,
            })
        except Exception as e:
            t = load_task(task_id)
            logs = t.get('logs', []) if t else []
            delete_task(task_id)
            return jsonify({
                'status': 'error', 'error': str(e),
                'logs': logs, 'dry_run': dry_run,
            }), 200
