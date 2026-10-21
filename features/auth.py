# features/auth.py
import os
import hmac
import logging
from flask import request, Response

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# Password file search order (first existing wins):
#   1. $AUTH_PASSWORD_FILE          (explicit override — useful for Docker/k8s)
#   2. /root/.password              (system-wide for the root user)
#   3. ~/.password                  (current user's home — $HOME expansion)
#   4. <project_root>/.password     (legacy, kept for backward compat)
# ============================================================
def _candidate_paths():
    paths = []
    env_override = os.environ.get('AUTH_PASSWORD_FILE', '').strip()
    if env_override:
        paths.append(env_override)
    paths.append('/root/.password')
    try:
        home = os.path.expanduser('~')
        if home and home not in ('~', '/'):
            paths.append(os.path.join(home, '.password'))
    except Exception:
        pass
    paths.append(os.path.join(PROJECT_ROOT, '.password'))

    # De-dup while preserving order
    seen = set()
    out = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve_password_file():
    """Return the first existing candidate path, or None."""
    for p in _candidate_paths():
        try:
            if os.path.isfile(p):
                return p
        except OSError:
            continue
    return None


# Cache keyed by (path, mtime) so edits take effect immediately,
# and moving the file is picked up too.
_cache = {'path': None, 'mtime': 0, 'password': None}

# Endpoints that bypass Basic Auth (they have their own header-token auth).
EXEMPT_PATHS = {
    '/favicon.ico',
    '/upload_cookies',
    '/upload_token',
    '/processed_list',
}

EXEMPT_PREFIXES = (
    '/mark_processing/',
    '/mark_done/',
    '/mark_failed/',
)


def _is_exempt(path: str) -> bool:
    if path in EXEMPT_PATHS:
        return True
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


def _load_password():
    """Return the current password, reloading whenever the file changes."""
    path = _resolve_password_file()
    if not path:
        if _cache['path'] is not None:
            # File was removed since last read
            logger.warning(f"Password file disappeared ({_cache['path']}); "
                           "all requests will be denied")
            _cache.update(path=None, mtime=0, password=None)
        return None

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    if mtime != _cache['mtime'] or _cache['path'] != path or _cache['password'] is None:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                pw = f.readline().rstrip('\r\n')
            _cache.update(path=path, mtime=mtime, password=pw)
            if pw:
                logger.info(f"Auth password loaded from {path}")
            else:
                logger.warning(f"{path} is empty — all requests will be denied")
        except Exception as e:
            logger.error(f"Failed to read {path}: {e}")
            return None

    return _cache['password']


def _check_basic_auth(auth) -> bool:
    if auth is None or auth.type != 'basic':
        return False

    configured = _load_password()
    if not configured:
        # Fail closed
        return False

    supplied = (auth.password or '').encode('utf-8')
    expected = configured.encode('utf-8')
    return hmac.compare_digest(supplied, expected)


def register_auth(app):
    """Require HTTP Basic Auth on every route except exempt paths."""

    # Log the resolved path once at startup
    path = _resolve_password_file()
    if path:
        logger.info(f"Auth enabled — password file: {path}")
    else:
        logger.warning(
            "Auth is ENABLED but no password file found. "
            f"Looked in: {', '.join(_candidate_paths())}. "
            "All requests will be rejected with 401 until you create one."
        )

    @app.before_request
    def _require_auth():
        if _is_exempt(request.path):
            return None
        if _check_basic_auth(request.authorization):
            return None
        return Response(
            'Authentication required',
            401,
            {'WWW-Authenticate': 'Basic realm="ProjectCompress", charset="UTF-8"'}
        )
