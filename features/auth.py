# features/auth.py
import os
import hmac
import logging
from flask import request, Response

logger = logging.getLogger(__name__)

# Path to the password file — one line, plaintext.
# Project root: /root/ProjectCompress/.password
PASSWORD_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '.password'
)

# Cache with mtime check so editing the file takes effect immediately.
_cache = {'mtime': 0, 'password': None}

# Endpoints that must remain reachable without Basic Auth because they
# have their own header-token authentication (for the browser extension
# and the Colab coordinator).
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


def _is_exempt(path):
    if path in EXEMPT_PATHS:
        return True
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


def _load_password():
    """Return the current password, reloading if the file changed."""
    try:
        mtime = os.path.getmtime(PASSWORD_FILE)
    except OSError:
        return None

    if mtime != _cache['mtime'] or _cache['password'] is None:
        try:
            with open(PASSWORD_FILE, 'r', encoding='utf-8') as f:
                pw = f.readline().rstrip('\r\n')
            _cache['mtime'] = mtime
            _cache['password'] = pw
            if pw:
                logger.info("Auth password loaded/refreshed from .password")
            else:
                logger.warning(".password is empty — all requests will be denied")
        except Exception as e:
            logger.error(f"Failed to read .password: {e}")
            return None

    return _cache['password']


def _check_basic_auth(auth):
    """Validate the Basic Auth credentials against the password file."""
    if auth is None or auth.type != 'basic':
        return False

    configured = _load_password()
    if not configured:
        # No password configured → refuse everything (fail-closed)
        return False

    supplied = (auth.password or '').encode('utf-8')
    expected = configured.encode('utf-8')

    # Timing-safe comparison
    return hmac.compare_digest(supplied, expected)


def register_auth(app):
    """
    Install a before_request hook that requires HTTP Basic Auth on
    every route except the exempt paths above.
    """

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
