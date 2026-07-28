import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Directories
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'downloads')
TASKS_DIR = os.path.join(BASE_DIR, 'tasks')
PROXY_CACHE_DIR = os.path.join(BASE_DIR, 'proxy_cache')

# Create directories if they don't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TASKS_DIR, exist_ok=True)
os.makedirs(PROXY_CACHE_DIR, exist_ok=True)

# Allowed video extensions (for backward compatibility)
ALLOWED_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v'}

# HTTP proxy (for direct URL downloads)
PROXY_URL = os.environ.get('PROXY_URL', '')
PROXY_DICT = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# Google Drive
DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID', '1pWRXvzo6KmnMfboTr4yfrD0AseRR0KEu')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
if not os.path.exists(TOKEN_FILE):
    print("WARNING: token.json not found. Google Drive features may not work.", file=sys.stderr)

# Telegram
TELEGRAM_SESSION_FILE = os.path.join(BASE_DIR, 'telegram_session')
TELEGRAM_CREDS_FILE = os.path.join(BASE_DIR, 'telegram_creds.json')
TELEGRAM_PROXY = None   # Disable proxy for Telegram

# Flask
SECRET_KEY = os.environ.get('SECRET_KEY', 'your-secret-key-here')
MAX_CONTENT_LENGTH = 2 * 1024 * 1024 * 1024  # 2 GB

