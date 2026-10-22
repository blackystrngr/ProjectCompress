# features/workflow.py
"""
Smart node-based workflow engine.

Execution model: token-based traversal with multi-output-port branching.

Storage (v3):
    {
      "id", "name",
      "nodes": [{"id","type","config","x","y"}, ...],
      "edges": [{"from","from_port","to"}, ...]
    }

Edges without `from_port` default to "out" (v2 compat).
Execution is per-item: a "token" starts at the trigger and flows
through nodes, activating output ports, until no more tokens are active.
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
# Node types — fields, ports, retry/error config
# ============================================================
# Common per-node advanced fields (rendered under "Advanced" in inspector):
COMMON_ADVANCED = [
    {'name': 'retry_max',   'type': 'number', 'label': 'Retries on failure',
     'default': 0, 'help': '0 = no retry. Failed attempts sleep then retry.'},
    {'name': 'retry_delay', 'type': 'number', 'label': 'Retry delay (s)',
     'default': 3},
    {'name': 'on_error',    'type': 'select', 'label': 'On error',
     'options': ['stop', 'continue'],
     'default': 'stop',
     'help': '"stop" cancels the whole item. "continue" skips the node '
             'and moves on (context is left unchanged).'},
    {'name': 'skip_if',     'type': 'text', 'label': 'Skip if (Python expr)',
     'default': '',
     'help': 'If this evaluates to True, the node is skipped. '
             'Example: size < 1000  ·  "sample" in filename'},
]

NODE_TYPES = {
    # ---------------- Triggers ----------------
    'trigger.url_list': {
        'label': 'URL List',
        'category': 'trigger',
        'icon': 'fa-list',
        'color': '#4ade80',
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
             'default': True,
             'help': 'Each playlist URL becomes N videos — one per video. '
                     'Each flows through the whole chain independently.'},
            {'name': 'max_items', 'type': 'number',
             'label': 'Max videos per playlist (0 = all)', 'default': 0},
        ],
        'advanced_fields': [],
    },
    'trigger.file_glob': {
        'label': 'File Glob',
        'category': 'trigger',
        'icon': 'fa-folder-open',
        'color': '#4ade80',
        'description': 'Provides a list of files matching a glob pattern '
                       'inside the downloads folder.',
        'ports': {'in': [], 'out': ['out']},
        'fields': [
            {'name': 'pattern', 'type': 'text', 'label': 'Glob pattern',
             'required': True, 'default': '**/*.mp4',
             'help': 'Relative to downloads/. Examples: *.mp4, playlist_*/*.mp4'},
            {'name': 'max_items', 'type': 'number',
             'label': 'Max files (0 = all)', 'default': 0},
        ],
        'advanced_fields': [],
    },

    # ---------------- Actions ----------------
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
                     '{size}, {basename}, {ext}'},
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
            {'name': 'crf', 'type': 'number', 'label': 'CRF', 'default': 27,
             'help': 'x265: 24-30 · AV1: 28-35'},
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
        'description': 'Pauses the workflow for N seconds.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'seconds', 'type': 'number', 'label': 'Seconds',
             'default': 5},
            {'name': 'jitter', 'type': 'number', 'label': 'Random jitter (s)',
             'default': 0,
             'help': 'Adds 0..jitter seconds randomly. Avoids thundering-herd '
                     'when processing large playlists.'},
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
             'default': '{"text": "Downloaded {filename} from {url}"}',
             'help': 'Placeholders: {filename}, {url}, {size}, {index}, {total}'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'action.extract_metadata': {
        'label': 'Extract Metadata',
        'category': 'action', 'icon': 'fa-info-circle', 'color': '#22d3ee',
        'description': 'Extracts video metadata (duration, resolution, codecs) '
                       'into the context as {duration}, {width}, {height}, '
                       '{vcodec}, {acodec}.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [],
        'advanced_fields': COMMON_ADVANCED,
    },

    # ---------------- Logic ----------------
    'logic.condition': {
        'label': 'Condition',
        'category': 'logic', 'icon': 'fa-code-branch', 'color': '#a78bfa',
        'description': 'Evaluates an expression. Routes to "true" or "false" '
                       'port based on the result.',
        'ports': {'in': ['in'], 'out': ['true', 'false']},
        'fields': [
            {'name': 'expression', 'type': 'text', 'label': 'Expression',
             'required': True, 'default': 'size > 1000000',
             'help': 'Python expression. Available: filename, basename, ext, '
                     'url, size, index, total, duration, width, height, '
                     'vcodec, acodec, vars (dict), re (module). '
                     'Example: "mp4" in filename and size > 10_000_000'},
        ],
        'advanced_fields': [],
    },
    'logic.switch': {
        'label': 'Switch',
        'category': 'logic', 'icon': 'fa-sitemap', 'color': '#a78bfa',
        'description': 'Evaluates an expression and routes to the matching '
                       'case port, or "default".',
        'ports': {'in': ['in'],
                  'out': ['1', '2', '3', 'default']},  # rendered per config
        'fields': [
            {'name': 'expression', 'type': 'text', 'label': 'Expression',
             'required': True, 'default': 'ext',
             'help': 'Python expression. Result is compared to each case key. '
                     'Example: ext  →  cases "mp4", "mkv", "webm"'},
            {'name': 'cases', 'type': 'text', 'label': 'Cases (comma-separated)',
             'default': 'mp4,mkv,webm',
             'help': 'If expression matches a case, that port fires. '
                     'Otherwise the "default" port fires.'},
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
             'help': '"all" waits for every input branch to arrive. '
                     '"any" fires on the first incoming wire.'},
        ],
        'advanced_fields': [],
    },

    # ---------------- Data ----------------
    'data.transform': {
        'label': 'Transform',
        'category': 'data', 'icon': 'fa-wand-magic-sparkles', 'color': '#4ade80',
        'description': 'Modifies a context value using a Python expression, '
                       'or sets a variable from a template.',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'target', 'type': 'text', 'label': 'Target key',
             'required': True, 'default': 'filename',
             'help': 'Which context key to write. Use "vars.<name>" to set '
                     'a custom variable.'},
            {'name': 'expression', 'type': 'text', 'label': 'Value expression',
             'required': True, 'default': 'filename.lower()',
             'help': 'Python expression or template. If it looks like a '
                     'template ({filename}), placeholders are expanded. '
                     'Otherwise it is evaluated as Python.'},
        ],
        'advanced_fields': COMMON_ADVANCED,
    },
    'data.set_variable': {
        'label': 'Set Variable',
        'category': 'data', 'icon': 'fa-tag', 'color': '#4ade80',
        'description': 'Sets a custom variable you can reference later as '
                       '{vars.name} or in expressions as vars["name"].',
        'ports': {'in': ['in'], 'out': ['out']},
        'fields': [
            {'name': 'name', 'type': 'text', 'label': 'Variable name',
             'required': True, 'default': 'my_var'},
            {'name': 'value', 'type': 'text', 'label': 'Value',
             'default': '{url}',
             'help': 'Template — {url}, {filename}, {index}, etc. are expanded.'},
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
    'any': any, 'all': all,
}


def _safe_eval(expr, ctx_dict):
    """Evaluate a Python expression in a locked namespace."""
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
    """Flatten context into an eval namespace."""
    d = {
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
    return d


# ============================================================
# Graph helpers
# ============================================================
def _norm_edge(e):
    """Ensure edge has a from_port. v2 edges lack it."""
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
    """Kahn's algorithm — returns list of node dicts or None if cycle."""
    ids = {n['id'] for n in nodes}
    adj = {nid: [] for nid in ids}
    indeg = {nid: 0 for nid in ids}

    for e in edges:
        f = e.get('from')
        t = e.get('to')
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
    """Migrate v2 (no positions, no ports) to v3."""
    if not isinstance(wf, dict):
        return wf
    nodes = wf.get('nodes') or []
    if not nodes:
        return wf

    # Add positions if missing
    for i, node in enumerate(nodes):
        if 'x' not in node or not isinstance(node.get('x'), (int, float)):
            node['x'] = 80 + i * 280
        if 'y' not in node or not isinstance(node.get('y'), (int, float)):
            node['y'] = 200

    # Normalize edges
    edges = wf.get('edges') or []
    for e in edges:
        _norm_edge(e)
    wf['edges'] = edges

    return wf


# ============================================================
# Progress throttling
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
    """Replace {key} placeholders."""
    if not text:
        return ''
    d = _ctx_to_dict(ctx)
    d['vars'] = ctx.get('vars', {})
    def repl(m):
        key = m.group(1)
        # vars.name form
        if key.startswith('vars.'):
            return str(d['vars'].get(key[5:], ''))
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
    except Exception as e:
        logger.warning(f'Playlist expansion failed: {e}')
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
# Node handlers — return list of output ports to fire
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
    out_path = os.path.join(UPLOAD_FOLDER, out_name)
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
        if _is_cancelled(task_id):
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
        _log(task_id, node_id, 'warning', 'No Drive file in context — skipping')
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
        if _is_cancelled(task_id):
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
            raise Exception(f'Webhook returned HTTP {r.status_code}')
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
         f'Metadata: {ctx["width"]}x{ctx["height"]} · {ctx["duration"]:.1f}s · '
         f'{ctx["vcodec"] or "?"}/{ctx["acodec"] or "?"}')
    return ['out']


def _h_condition(task_id, node_id, config, ctx):
    expr = (config.get('expression') or '').strip()
    if not expr:
        raise Exception('Condition expression is empty')
    result = _safe_eval(expr, _ctx_to_dict(ctx))
    fired = 'true' if bool(result) else 'false'
    _log(task_id, node_id, 'info', f'Condition → {fired} ({expr[:60]})')
    return [fired]


def _h_switch(task_id, node_id, config, ctx):
    expr = (config.get('expression') or '').strip()
    if not expr:
        raise Exception('Switch expression is empty')
    raw_cases = (config.get('cases') or '').strip()
    cases = [c.strip() for c in raw_cases.split(',') if c.strip()]

    result = _safe_eval(expr, _ctx_to_dict(ctx))
    result_str = str(result) if result is not None else ''

    if result_str in cases:
        _log(task_id, node_id, 'info', f'Switch → "{result_str}"')
        return [result_str]
    _log(task_id, node_id, 'info', f'Switch → "default" ({result_str!r})')
    return ['default']


def _h_merge(task_id, node_id, config, ctx):
    # Runtime already checked readiness before calling us
    mode = (config.get('mode') or 'all').lower()
    _log(task_id, node_id, 'info', f'Merge ({mode})')
    return ['out']


def _h_transform(task_id, node_id, config, ctx):
    target = (config.get('target') or '').strip()
    if not target:
        raise Exception('Target key required')
    expr = (config.get('expression') or '').strip()

    # If it looks like a template with {placeholders}, expand.
    if '{' in expr and '}' in expr:
        value = _expand_template(expr, ctx)
    else:
        value = _safe_eval(expr, _ctx_to_dict(ctx))

    if target.startswith('vars.'):
        var_name = target[5:]
        if 'vars' not in ctx or not isinstance(ctx['vars'], dict):
            ctx['vars'] = {}
        ctx['vars'][var_name] = value
    else:
        ctx[target] = value
    _log(task_id, node_id, 'success',
         f'{target} = {str(value)[:80]}')
    return ['out']


def _h_set_variable(task_id, node_id, config, ctx):
    name = (config.get('name') or '').strip()
    if not name:
        raise Exception('Variable name required')
    template = config.get('value') or ''
    value = _expand_template(template, ctx)
    if 'vars' not in ctx or not isinstance(ctx['vars'], dict):
        ctx['vars'] = {}
    ctx['vars'][name] = value
    _log(task_id, node_id, 'success', f'vars["{name}"] = {value[:80]}')
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
    'logic.condition':          _h_condition,
    'logic.switch':             _h_switch,
    'logic.merge':              _h_merge,
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
            return f'Node {i} has unknown type: {ntype}'
        node_map[node['id']] = node

        config = node.get('config') or {}
        for field in NODE_TYPES[ntype].get('fields', []):
            if field.get('required'):
                val = str(config.get(field['name'], '')).strip()
                if not val:
                    return f'{NODE_TYPES[ntype]["label"]}: {field["label"]} is required'

    # Edge validation
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            return f'Edge {i} must be an object'
        f, t = e.get('from'), e.get('to')
        if f not in node_map or t not in node_map:
            return f'Edge {i} references unknown node'
        if f == t:
            return f'Edge {i} is a self-loop'
        from_port = e.get('from_port') or 'out'
        from_ports = _node_ports(node_map[f]['type'])['out']
        if from_port not in from_ports:
            return (f'Edge {i}: node {f[:8]} has no output port "{from_port}" '
                    f'(available: {", ".join(from_ports)})')

    # Trigger validation
    triggers = [n for n in nodes if n['type'].startswith('trigger.')]
    if not triggers:
        return 'Need at least one trigger node'
    if len(triggers) > 1:
        return 'Only one trigger allowed'
    trigger = triggers[0]

    if _in_edges(trigger['id'], edges):
        return 'Trigger cannot have incoming connections'

    # Incoming edge count per non-trigger node
    for node in nodes:
        if node['id'] == trigger['id']:
            continue
        ins = _in_edges(node['id'], edges)
        if node['type'] == 'logic.merge':
            if len(ins) < 2:
                return f'{NODE_TYPES[node["type"]]["label"]} needs at least 2 incoming connections'
        else:
            if len(ins) != 1:
                return (f'{NODE_TYPES[node["type"]]["label"]}: must have exactly '
                        f'1 incoming connection (has {len(ins)})')

    # Cycle detection
    if _topological_order(nodes, edges) is None:
        return 'Workflow contains a cycle'

    # Reachability check
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
        return (f'{len(unreachable)} node(s) unreachable from trigger. '
                f'Connect them or delete them.')

    return None


# ============================================================
# Executor
# ============================================================
def _execute_node(task_id, node, node_config, ctx):
    """Run a single node with retry/error handling."""
    handler = NODE_HANDLERS.get(node['type'])
    if not handler:
        raise Exception(f'No handler for {node["type"]}')

    # Skip condition
    skip_expr = (node_config.get('skip_if') or '').strip()
    if skip_expr:
        try:
            if _safe_eval(skip_expr, _ctx_to_dict(ctx)):
                _log(task_id, node['id'], 'info', f'Skipped ({skip_expr})')
                return ['out']  # pass through
        except Exception as e:
            _log(task_id, node['id'], 'warning',
                 f'skip_if error: {e} — running anyway')

    retry_max = int(node_config.get('retry_max') or 0)
    retry_delay = float(node_config.get('retry_delay') or 3)
    on_error = (node_config.get('on_error') or 'stop').lower()

    attempt = 0
    last_err = None
    while attempt <= retry_max:
        if _is_cancelled(task_id):
            raise _Cancelled()
        try:
            ports = handler(task_id, node['id'], node_config, ctx)
            _update_node_progress(task_id, node['id'], 100, 'done')
            return ports or ['out']
        except _Cancelled:
            raise
        except Exception as e:
            last_err = e
            attempt += 1
            if attempt <= retry_max:
                _log(task_id, node['id'], 'warning',
                     f'Attempt {attempt}/{retry_max + 1} failed: {e}. '
                     f'Retrying in {retry_delay}s…')
                end = time.time() + retry_delay
                while time.time() < end:
                    if _is_cancelled(task_id):
                        raise _Cancelled()
                    time.sleep(0.2)

    # All retries exhausted
    if on_error == 'continue':
        _log(task_id, node['id'], 'error',
             f'Failed (continuing): {last_err}')
        _update_node_progress(task_id, node['id'], 100, 'error')
        return ['out']  # pass through anyway
    else:
        _update_node_progress(task_id, node['id'], 100, 'error')
        raise last_err


def _prepare_items(task_id, trigger, edges):
    """Expand trigger config into a list of items."""
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
                if _is_cancelled(task_id):
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


def _run_workflow(task_id, workflow):
    nodes = workflow['nodes']
    edges = workflow.get('edges', [])
    node_map = {n['id']: n for n in nodes}

    order = _topological_order(nodes, edges)
    if not order:
        _log(task_id, None, 'error', 'Cycle detected — cannot run')
        raise Exception('Invalid workflow graph')

    trigger = next((n for n in nodes if n['type'].startswith('trigger.')), None)
    if not trigger:
        raise Exception('No trigger node')

    try:
        items = _prepare_items(task_id, trigger, edges)
        if not items:
            raise Exception('Trigger produced no items')

        task = load_task(task_id)
        task['items'] = [str(x)[:200] for x in items]  # truncate for storage
        task['total_items'] = len(items)
        save_task(task_id, task)

        _log(task_id, None, 'info',
             f'Starting "{workflow.get("name", "Untitled")}" — '
             f'{len(items)} item(s), {len(nodes) - 1} node(s)')

        for idx, item in enumerate(items):
            if _is_cancelled(task_id):
                raise _Cancelled()

            task = load_task(task_id)
            task['current_item_index'] = idx
            task['current_item'] = str(item)[:200]
            task['progress'] = int(100 * idx / len(items))
            task['node_statuses'] = {}
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
            }

            # ---- Token-based traversal ----
            incoming_count = {nid: 0 for nid in node_map}
            expected_count = {nid: len(_in_edges(nid, edges)) for nid in node_map}
            completed = set()
            worklist = [trigger['id']]

            while worklist:
                if _is_cancelled(task_id):
                    raise _Cancelled()

                node_id = worklist.pop(0)
                if node_id in completed:
                    continue

                node = node_map[node_id]
                node_cfg = node.get('config') or {}

                # Readiness check for merge nodes
                in_count = incoming_count[node_id]
                exp = expected_count[node_id]
                if exp > 0 and in_count < exp:
                    if node['type'] == 'logic.merge' and \
                       (node_cfg.get('mode') or 'all').lower() == 'any':
                        pass  # 'any' mode: fire now
                    else:
                        continue  # wait for more inputs

                # Mark running
                task = load_task(task_id)
                task['current_node_id'] = node_id
                task['node_statuses'][node_id] = {'status': 'running', 'progress': 0}
                save_task(task_id, task)
                _log(task_id, node_id, 'info',
                     f'Running {NODE_TYPES[node["type"]]["label"]}')

                try:
                    fired_ports = _execute_node(task_id, node, node_cfg, ctx)
                except _Cancelled:
                    raise
                except Exception as e:
                    logger.exception(f'Node {node_id} failed')
                    raise Exception(
                        f'{NODE_TYPES[node["type"]]["label"]}: {e}')

                completed.add(node_id)

                # Fire output ports
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
                            # 'any' merge fires on first incoming
                            worklist.append(to_id)

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

    @app.route('/workflow/run', methods=['POST'])
    def workflow_run():
        data = request.get_json(silent=True) or {}
        err = _validate_workflow(data)
        if err:
            return jsonify({'error': err}), 400
        if len(data['nodes']) < 2:
            return jsonify({'error': 'Add at least one action node'}), 400

        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id, 'status': 'running', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'workflow_name': data.get('name', 'Untitled'),
            'items': [], 'total_items': 0,
            'current_item_index': 0, 'current_item': '',
            'current_node_id': None, 'node_statuses': {}, 'logs': [],
            'type': 'workflow',
        })

        threading.Thread(target=_run_workflow, args=(task_id, data),
                         daemon=True).start()
        return jsonify({'task_id': task_id})
