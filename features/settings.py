# features/settings.py
"""
Central settings store + JSON API for the slide-out settings panel.

settings.json is the source of truth. Existing consumers
(thumbnails.py, google_drive.py) keep reading the same file.
"""

import os
import json
import threading
import logging
from flask import request, jsonify

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(PROJECT_ROOT, 'settings.json')

_write_lock = threading.Lock()


# ============================================================
# Low-level I/O
# ============================================================
def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Failed to load settings.json: {e}")
    return {}


def save_settings(data: dict) -> bool:
    with _write_lock:
        try:
            tmp = SETTINGS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
            return True
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            return False


# ============================================================
# Schema — add new keys here
# ============================================================
# type: 'bool' | 'str' | 'int'
# choices (optional): allowed string values
# max (optional): max string length
# min/max (optional): int bounds
ALLOWED_KEYS = {
    'ui_theme':             {'type': 'str',  'choices': ('classic', 'modern')},
    'thumbnails_enabled':   {'type': 'bool'},
    'coordinator_enabled':  {'type': 'bool'},
    'tg_log_enabled':       {'type': 'bool'},
    'tg_log_channel':       {'type': 'str',  'max': 200},
    'tg_log_bot_token':     {'type': 'str',  'max': 200},
    'tg_log_level':         {'type': 'str',
                             'choices': ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')},
}


def _coerce(key: str, value, spec: dict):
    """Return (ok, coerced_value_or_error)."""
    t = spec['type']

    if t == 'bool':
        if isinstance(value, str):
            return True, value.strip().lower() in ('true', '1', 'yes', 'on')
        return True, bool(value)

    if t == 'str':
        s = '' if value is None else str(value).strip()
        if 'max' in spec and len(s) > spec['max']:
            return False, f'must be ≤ {spec["max"]} characters'
        if 'choices' in spec and s not in spec['choices']:
            return False, f'must be one of: {", ".join(spec["choices"])}'
        return True, s

    if t == 'int':
        try:
            i = int(value)
        except (TypeError, ValueError):
            return False, 'must be an integer'
        if 'min' in spec and i < spec['min']:
            return False, f'must be ≥ {spec["min"]}'
        if 'max' in spec and i > spec['max']:
            return False, f'must be ≤ {spec["max"]}'
        return True, i

    return False, 'unknown type'


# ============================================================
# Merge env vars with settings.json (settings.json wins)
# ============================================================
def get_effective_config() -> dict:
    s = load_settings()

    def pick(key, default, env_key=None, transform=None):
        if key in s:
            val = s[key]
        elif env_key and os.environ.get(env_key) not in (None, ''):
            val = os.environ[env_key]
        else:
            val = default
        if transform:
            try:
                return transform(val)
            except Exception:
                return default
        return val

    def _as_bool(v):
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ('true', '1', 'yes', 'on')

    return {
        'ui_theme':            s.get('ui_theme', 'classic'),
        'thumbnails_enabled':  s.get('thumbnails_enabled', True),
        'coordinator_enabled': s.get('coordinator_enabled', True),
        'tg_log_enabled':      pick('tg_log_enabled', False, 'TG_LOG_ENABLED', _as_bool),
        'tg_log_channel':      pick('tg_log_channel', '', 'TG_LOG_CHANNEL'),
        'tg_log_bot_token':    pick('tg_log_bot_token', '', 'TG_LOG_BOT_TOKEN'),
        'tg_log_level':        pick('tg_log_level', 'WARNING', 'TG_LOG_LEVEL',
                                    lambda v: str(v).upper() or 'WARNING'),
    }


# ============================================================
# Routes
# ============================================================
def register_routes(app):

    @app.route('/settings', methods=['GET'])
    def settings_get():
        cfg = get_effective_config()

        # Mask the bot token; never expose it over the wire
        token = cfg.get('tg_log_bot_token') or ''
        if token:
            masked = (token[:8] + '…' + token[-4:]) if len(token) > 16 else '••••••••'
        else:
            masked = ''

        # Which values came from env (read-only hints)
        env_info = {
            'TG_LOG_CHANNEL':     bool(os.environ.get('TG_LOG_CHANNEL')),
            'TG_LOG_BOT_TOKEN':   bool(os.environ.get('TG_LOG_BOT_TOKEN')),
            'TG_LOG_LEVEL':       bool(os.environ.get('TG_LOG_LEVEL')),
            'TG_LOG_ENABLED':     bool(os.environ.get('TG_LOG_ENABLED')),
        }

        return jsonify({
            'ui_theme':              cfg['ui_theme'],
            'thumbnails_enabled':    cfg['thumbnails_enabled'],
            'coordinator_enabled':   cfg['coordinator_enabled'],
            'tg_log_enabled':        cfg['tg_log_enabled'],
            'tg_log_channel':        cfg['tg_log_channel'],
            'tg_log_bot_token':      '',           # never send the real token
            'tg_log_bot_token_set':  bool(token),
            'tg_log_bot_token_masked': masked,
            'tg_log_level':          cfg['tg_log_level'],
            'env':                   env_info,
        })

    @app.route('/settings', methods=['POST'])
    def settings_post():
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict()
        if not data:
            return jsonify({'error': 'No data'}), 400

        current = load_settings()
        errors = {}
        updated = []

        for key, raw in data.items():
            if key not in ALLOWED_KEYS:
                errors[key] = 'unknown setting'
                continue
            ok, val = _coerce(key, raw, ALLOWED_KEYS[key])
            if not ok:
                errors[key] = val
                continue
            current[key] = val
            updated.append(key)

        if not updated and errors:
            return jsonify({'error': 'validation failed', 'errors': errors}), 400

        if updated and not save_settings(current):
            return jsonify({'error': 'failed to save settings.json'}), 500

        # ---------- Apply to subsystems ----------
        applied = []

        if 'thumbnails_enabled' in updated:
            try:
                from .thumbnails import set_thumbnails_enabled
                set_thumbnails_enabled(current['thumbnails_enabled'])
                applied.append('thumbnails')
            except Exception as e:
                errors['thumbnails_enabled'] = str(e)

        if 'coordinator_enabled' in updated:
            try:
                from .google_drive import set_coordinator_enabled
                set_coordinator_enabled(current['coordinator_enabled'])
                applied.append('coordinator')
            except Exception as e:
                errors['coordinator_enabled'] = str(e)

        if any(k.startswith('tg_log_') for k in updated):
            try:
                from .telegram_logger import reconfigure
                reconfigure()
                applied.append('telegram_logger')
            except Exception as e:
                errors['tg_log'] = str(e)

        # ui_theme is applied client-side; nothing to do server-side

        return jsonify({
            'status':  'ok',
            'updated': updated,
            'applied': applied,
            'errors':  errors or None,
            'current': {
                'ui_theme':            current.get('ui_theme', 'classic'),
                'thumbnails_enabled':  current.get('thumbnails_enabled', True),
                'coordinator_enabled': current.get('coordinator_enabled', True),
                'tg_log_enabled':      current.get('tg_log_enabled', False),
                'tg_log_channel':      current.get('tg_log_channel', ''),
                'tg_log_level':        current.get('tg_log_level', 'WARNING'),
            },
        })
