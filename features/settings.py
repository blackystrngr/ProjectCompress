# features/settings.py
"""
Central settings store + JSON API for the slide-out settings panel.
"""

import os
import json
import threading
import logging
from flask import request, jsonify

logger = logging.getLogger(__name__)

from config import SETTINGS_FILE, SECRETS_DIR
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_write_lock = threading.Lock()


# ============================================================
# I/O
# ============================================================
def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.warning(f"settings.json is not a dict: {type(data)}")
        except Exception as e:
            logger.warning(f"Failed to load settings.json: {e}")
    return {}


def save_settings(data: dict) -> tuple[bool, str]:
    """Return (ok, error_message)."""
    with _write_lock:
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            tmp = SETTINGS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
            logger.info(f"settings.json saved: {sorted(data.keys())}")
            return True, ''
        except PermissionError as e:
            msg = f"Permission denied writing {SETTINGS_FILE}: {e}"
            logger.error(msg)
            return False, msg
        except Exception as e:
            msg = f"Write failed: {type(e).__name__}: {e}"
            logger.exception(msg)
            return False, msg


# ============================================================
# Schema
# ============================================================
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
    t = spec['type']

    if t == 'bool':
        if isinstance(value, str):
            return True, value.strip().lower() in ('true', '1', 'yes', 'on')
        return True, bool(value)

    if t == 'str':
        s = '' if value is None else str(value).strip()
        if 'max' in spec and len(s) > spec['max']:
            return False, f'must be ≤ {spec["max"]} chars'
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

    logger.info(f"Settings file: {SETTINGS_FILE} (secrets dir: {SECRETS_DIR})")

    @app.route('/settings', methods=['GET'])
    def settings_get():
        cfg = get_effective_config()
        token = cfg.get('tg_log_bot_token') or ''
        masked = ''
        if token:
            masked = (token[:8] + '…' + token[-4:]) if len(token) > 16 else '••••••••'

        env_info = {k: bool(os.environ.get(k))
                    for k in ('TG_LOG_CHANNEL', 'TG_LOG_BOT_TOKEN',
                              'TG_LOG_LEVEL', 'TG_LOG_ENABLED')}

        return jsonify({
            'ui_theme':                cfg['ui_theme'],
            'thumbnails_enabled':      cfg['thumbnails_enabled'],
            'coordinator_enabled':     cfg['coordinator_enabled'],
            'tg_log_enabled':          cfg['tg_log_enabled'],
            'tg_log_channel':          cfg['tg_log_channel'],
            'tg_log_bot_token':        '',
            'tg_log_bot_token_set':    bool(token),
            'tg_log_bot_token_masked': masked,
            'tg_log_level':            cfg['tg_log_level'],
            'env':                     env_info,
            'settings_file':           SETTINGS_FILE,
        })

    @app.route('/settings', methods=['POST'])
    def settings_post():
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict()
        if not data:
            return jsonify({'error': 'No data received'}), 400

        logger.info(f"settings POST received keys: {sorted(data.keys())}")

        current = load_settings()
        errors = {}
        updated = []

        for key, raw in data.items():
            if key not in ALLOWED_KEYS:
                errors[key] = 'unknown setting (not in ALLOWED_KEYS)'
                logger.warning(f"Rejected unknown setting: {key}")
                continue
            ok, val = _coerce(key, raw, ALLOWED_KEYS[key])
            if not ok:
                errors[key] = val
                logger.warning(f"Rejected {key}={raw!r}: {val}")
                continue
            current[key] = val
            updated.append(key)

        if not updated:
            return jsonify({
                'error':   'no valid settings in request',
                'errors':  errors,
            }), 400

        ok, err = save_settings(current)
        if not ok:
            return jsonify({
                'error':  f'failed to save settings.json: {err}',
                'errors': errors,
            }), 500

        # Apply to subsystems
        applied = []

        if 'thumbnails_enabled' in updated:
            try:
                from .thumbnails import set_thumbnails_enabled
                set_thumbnails_enabled(current['thumbnails_enabled'])
                applied.append('thumbnails')
            except Exception as e:
                logger.exception("Failed to apply thumbnails_enabled")
                errors['thumbnails_enabled'] = str(e)

        if 'coordinator_enabled' in updated:
            try:
                from .google_drive import set_coordinator_enabled
                set_coordinator_enabled(current['coordinator_enabled'])
                applied.append('coordinator')
            except Exception as e:
                logger.exception("Failed to apply coordinator_enabled")
                errors['coordinator_enabled'] = str(e)

        if any(k.startswith('tg_log_') for k in updated):
            try:
                from .telegram_logger import reconfigure
                reconfigure()
                applied.append('telegram_logger')
            except Exception as e:
                logger.exception("Failed to apply tg_log settings")
                errors['tg_log'] = str(e)

        logger.info(f"settings saved: updated={updated}, applied={applied}")

        return jsonify({
            'status':  'ok',
            'updated': updated,
            'applied': applied,
            'errors':  errors or None,
            'current': get_effective_config_without_token(),
        })

    @app.route('/settings/debug', methods=['GET'])
    def settings_debug():
        """Hit this to diagnose why saves might fail."""
        import sys
        info = {
            'settings_file':  SETTINGS_FILE,
            'file_exists':    os.path.exists(SETTINGS_FILE),
            'dir_writable':   os.access(PROJECT_ROOT, os.W_OK),
            'cwd':            os.getcwd(),
            'python':         sys.version.split()[0],
        }

        if os.path.exists(SETTINGS_FILE):
            try:
                info['file_size']    = os.path.getsize(SETTINGS_FILE)
                info['file_mtime']   = os.path.getmtime(SETTINGS_FILE)
                info['file_writable']= os.access(SETTINGS_FILE, os.W_OK)
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    contents = json.load(f)
                # Mask the token
                if isinstance(contents, dict) and contents.get('tg_log_bot_token'):
                    tok = contents['tg_log_bot_token']
                    contents['tg_log_bot_token'] = (
                        tok[:8] + '…' + tok[-4:] if len(tok) > 16 else '••••')
                info['contents'] = contents
            except Exception as e:
                info['read_error'] = f"{type(e).__name__}: {e}"

        # Live write test
        try:
            test = dict(load_settings())
            test['__test__'] = int(os.times().elapsed)
            ok, err = save_settings(test)
            test.pop('__test__', None)
            if ok:
                # Restore (removes the __test__ key)
                save_settings(test)
                info['write_test'] = 'ok'
            else:
                info['write_test'] = f'FAILED: {err}'
        except Exception as e:
            info['write_test'] = f'EXCEPTION: {type(e).__name__}: {e}'

        info['effective_config'] = get_effective_config_without_token()
        info['env'] = {
            'TG_LOG_CHANNEL':         os.environ.get('TG_LOG_CHANNEL') or None,
            'TG_LOG_ENABLED':         os.environ.get('TG_LOG_ENABLED') or None,
            'TG_LOG_LEVEL':           os.environ.get('TG_LOG_LEVEL') or None,
            'TG_LOG_BOT_TOKEN_set':   bool(os.environ.get('TG_LOG_BOT_TOKEN')),
        }

        return jsonify(info)


def get_effective_config_without_token() -> dict:
    """Same as get_effective_config but with token replaced by a bool."""
    cfg = get_effective_config()
    token = cfg.get('tg_log_bot_token') or ''
    cfg['tg_log_bot_token'] = ''
    cfg['tg_log_bot_token_set'] = bool(token)
    return cfg
