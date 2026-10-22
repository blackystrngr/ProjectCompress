import os
import sys
import glob
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# Base directories
# ============================================================
UPLOAD_FOLDER   = os.path.join(BASE_DIR, 'downloads')
TASKS_DIR       = os.path.join(BASE_DIR, 'tasks')
PROXY_CACHE_DIR = os.path.join(BASE_DIR, 'proxy_cache')

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


# ============================================================
# One-time idempotent migration of all secret files
# ============================================================
_SECRET_PATTERNS = [
    'cookies.txt',
    'telegram_creds.json',
    'token.json',
    'token_*.json',
    'telegram_session.session',
    'telegram_session.session-journal',
    'telegram_logger_session.session',
    'telegram_logger_session.session-journal',
    'telegram_logger_bot.session',
    'telegram_logger_bot.session-journal',
    'settings.json',
    '.password',
]


def _migrate_secrets():
    sources = [(BASE_DIR, _SECRET_PATTERNS)]
    # /root/.password is a common legacy location
    if os.path.exists('/root/.password'):
        sources.append(('/root', ['.password']))

    for base, patterns in sources:
        for pat in patterns:
            for legacy in glob.glob(os.path.join(base, pat)):
                name = os.path.basename(legacy)
                new = os.path.join(SECRETS_DIR, name)
                if os.path.exists(new):
                    continue  # already migrated, target wins
                try:
                    try:
                        os.rename(legacy, new)
                    except OSError:
                        shutil.copy2(legacy, new)
                        os.remove(legacy)
                    try:
                        os.chmod(new, 0o600)
                    except OSError:
                        pass
                    print(f"[config] Migrated {name} → {SECRETS_DIR}",
                          file=sys.stderr)
                except Exception as e:
                    print(f"[config] Migration failed for {name}: {e}",
                          file=sys.stderr)


_migrate_secrets()


# ============================================================
# Resolved file paths (secrets first, legacy fallback)
# ============================================================
def _resolve(name):
    new = os.path.join(SECRETS_DIR, name)
    if os.path.exists(new):
        return new
    legacy = os.path.join(BASE_DIR, name)
    if os.path.exists(legacy):
        return legacy
    return new  # default target for new files


SETTINGS_FILE         = _resolve('settings.json')
LEGACY_SETTINGS_FILE  = os.path.join(BASE_DIR, 'settings.json')
SECRETS_PASSWORD_FILE = os.path.join(SECRETS_DIR, '.password')

COOKIES_FILE         = _resolve('cookies.txt')
TELEGRAM_CREDS_FILE  = _resolve('telegram_creds.json')
TOKEN_FILE           = _resolve('token.json')

# Telegram session base paths (Telethon appends .session)
TELEGRAM_SESSION_FILE       = os.path.join(SECRETS_DIR, 'telegram_session')
TELEGRAM_LOGGER_SESSION     = os.path.join(SECRETS_DIR, 'telegram_logger_session')
TELEGRAM_LOGGER_BOT_SESSION = os.path.join(SECRETS_DIR, 'telegram_logger_bot')

# Tighten settings.json permissions if it exists
if os.path.exists(SETTINGS_FILE):
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

DRIVE_FOLDER_ID = os.environ.get(
    'DRIVE_FOLDER_ID', '1pWRXvzo6KmnMfboTr4yfrD0AseRR0KEu')

TELEGRAM_PROXY = None

SECRET_KEY         = os.environ.get('SECRET_KEY', 'whyyouleftme')
MAX_CONTENT_LENGTH = 2 * 1024 * 1024 * 1024  # 2 GB


# ============================================================
# Multi-account Drive tokens — search both dirs (secrets wins)
# ============================================================
def discover_drive_accounts():
    """Return {account_name: token_path}. Secrets dir overrides legacy."""
    out = {}
    for base in (BASE_DIR, SECRETS_DIR):
        if not os.path.isdir(base):
            continue
        for path in sorted(glob.glob(os.path.join(base, 'token*.json'))):
            name = os.path.splitext(os.path.basename(path))[0]
            if name == 'token':
                out['default'] = path
            elif name.startswith('token_'):
                out[name[len('token_'):]] = path
    return out
