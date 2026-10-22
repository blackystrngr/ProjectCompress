import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# Directories
# ============================================================
UPLOAD_FOLDER    = os.path.join(BASE_DIR, 'downloads')
TASKS_DIR        = os.path.join(BASE_DIR, 'tasks')
PROXY_CACHE_DIR  = os.path.join(BASE_DIR, 'proxy_cache')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TASKS_DIR, exist_ok=True)
os.makedirs(PROXY_CACHE_DIR, exist_ok=True)

# ============================================================
# Secrets directory (outside project tree)
# ============================================================
SECRETS_DIR = os.environ.get('PROJECT_SECRETS_DIR', '/root/secrets')


def _ensure_secrets_dir():
    try:
        os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(SECRETS_DIR, 0o700)
        except OSError:
            pass
        return SECRETS_DIR
    except PermissionError:
        fallback = os.path.join(BASE_DIR, 'secrets')
        os.makedirs(fallback, mode=0o700, exist_ok=True)
        try:
            os.chmod(fallback, 0o700)
        except OSError:
            pass
        print(
            f"WARNING: Cannot write to {SECRETS_DIR} (not running as root?) "
            f"— falling back to {fallback}",
            file=sys.stderr,
        )
        return fallback


SECRETS_DIR = _ensure_secrets_dir()

SETTINGS_FILE        = os.path.join(SECRETS_DIR, 'settings.json')
LEGACY_SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')
SECRETS_PASSWORD_FILE = os.path.join(SECRETS_DIR, '.password')

# Auto-migrate legacy settings.json → secrets/ (one-time, idempotent)
if (not os.path.exists(SETTINGS_FILE)
        and os.path.exists(LEGACY_SETTINGS_FILE)):
    try:
        import shutil as _shutil
        _shutil.copy2(LEGACY_SETTINGS_FILE, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass
        print(f"[config] Migrated settings.json → {SETTINGS_FILE}",
              file=sys.stderr)
    except Exception as _e:
        print(f"[config] Could not migrate legacy settings: {_e}",
              file=sys.stderr)
        SETTINGS_FILE = LEGACY_SETTINGS_FILE
elif os.path.exists(SETTINGS_FILE):
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass

# ============================================================
# Media / network config
# ============================================================
ALLOWED_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v'}

PROXY_URL  = os.environ.get('PROXY_URL', '')
PROXY_DICT = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# ============================================================
# Google Drive
# ============================================================
DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID',
                                  '1pWRXvzo6KmnMfboTr4yfrD0AseRR0KEu')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
if not os.path.exists(TOKEN_FILE):
    print("WARNING: token.json not found. Google Drive features may not work.",
          file=sys.stderr)

# ============================================================
# Telegram
# ============================================================
TELEGRAM_SESSION_FILE = os.path.join(BASE_DIR, 'telegram_session')
TELEGRAM_CREDS_FILE   = os.path.join(BASE_DIR, 'telegram_creds.json')
TELEGRAM_PROXY        = None

# ============================================================
# Flask
# ============================================================
SECRET_KEY          = os.environ.get('SECRET_KEY', 'your-secret-key-here')
MAX_CONTENT_LENGTH  = 2 * 1024 * 1024 * 1024   # 2 GB

# ============================================================
# Multi-account Drive tokens
# ============================================================
import glob as _glob

def discover_drive_accounts():
    """Return {account_name: token_path}."""
    out = {}
    for path in sorted(_glob.glob(os.path.join(BASE_DIR, 'token*.json'))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name == 'token':
            out['default'] = path
        elif name.startswith('token_'):
            out[name[len('token_'):]] = path
    return out
