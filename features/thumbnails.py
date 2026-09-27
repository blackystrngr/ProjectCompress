import os
import re
import json
import shutil
import subprocess
import hashlib
import logging
import threading
from flask import request, jsonify, send_file, abort
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

THUMB_CACHE_DIR = os.path.join(UPLOAD_FOLDER, '.thumbnails')
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)

VIDEO_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v', '.ts', '.mpg', '.mpeg')

# ============================================================
# Settings (persisted to project root)
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(PROJECT_ROOT, 'settings.json')
_settings_lock = threading.Lock()


def _load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")
    return {'thumbnails_enabled': True}


def _save_settings(settings):
    with _settings_lock:
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(settings, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")


def is_thumbnails_enabled():
    return bool(_load_settings().get('thumbnails_enabled', True))


def set_thumbnails_enabled(enabled):
    settings = _load_settings()
    settings['thumbnails_enabled'] = bool(enabled)
    _save_settings(settings)


# ============================================================
# Concurrency guard (max 2 ffmpeg at once)
# ============================================================
_thumb_semaphore = threading.Semaphore(2)


# ============================================================
# Helpers
# ============================================================
def _get_ffmpeg():
    p = shutil.which('ffmpeg')
    if p:
        return p
    for c in ('/usr/local/bin/ffmpeg', '/usr/bin/ffmpeg'):
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


def _get_ffprobe():
    p = shutil.which('ffprobe')
    if p:
        return p
    for c in ('/usr/local/bin/ffprobe', '/usr/bin/ffprobe'):
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


def _duration(video_path):
    ffprobe = _get_ffprobe()
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        logger.warning(f"ffprobe failed for {video_path}: {e}")
    return None


def _cache_path(video_path):
    rel = os.path.relpath(video_path, UPLOAD_FOLDER)
    key = hashlib.sha1(rel.encode('utf-8')).hexdigest()
    return os.path.join(THUMB_CACHE_DIR, f"{key}.jpg")


def generate_thumbnail(video_path, force=False):
    """Generate thumbnail at duration-30s. Respects the semaphore."""
    if not os.path.exists(video_path):
        return None

    cache = _cache_path(video_path)

    if not force and os.path.exists(cache):
        try:
            if os.path.getmtime(cache) >= os.path.getmtime(video_path):
                return cache
        except OSError:
            pass

    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        logger.warning("ffmpeg not found – cannot generate thumbnails")
        return None

    dur = _duration(video_path)
    if dur is None or dur <= 0:
        seek = 1
    else:
        seek = max(0.0, dur - 30.0)
        if dur < 30:
            seek = max(0.0, dur - 1.0)

    seeks_to_try = [seek, 1, 0]

    with _thumb_semaphore:
        for s in seeks_to_try:
            try:
                cmd = [
                    ffmpeg,
                    '-ss', f"{s:.2f}",
                    '-i', video_path,
                    '-frames:v', '1',
                    '-vf', 'scale=320:-1',
                    '-q:v', '4',
                    '-y', cache
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and os.path.exists(cache) and os.path.getsize(cache) > 0:
                    logger.info(f"Thumbnail generated for {os.path.basename(video_path)} at {s:.1f}s")
                    return cache
            except subprocess.TimeoutExpired:
                logger.warning(f"Thumbnail timeout for {video_path} at {s}s")
                continue
            except Exception as e:
                logger.warning(f"Thumbnail generation failed at {s}s: {e}")
                continue

    logger.error(f"Could not generate thumbnail for {video_path}")
    return None


def delete_thumbnail(video_path):
    cache = _cache_path(video_path)
    if os.path.exists(cache):
        try:
            os.remove(cache)
        except Exception:
            pass


# ============================================================
# Routes
# ============================================================
def register_routes(app):
    @app.route('/thumbnail')
    def thumbnail():
        """Return thumbnail for a given file path (relative to UPLOAD_FOLDER)."""
        # ---- Check if thumbnails are enabled ----
        if not is_thumbnails_enabled():
            abort(404)

        rel_path = request.args.get('path', '')
        if not rel_path or '..' in rel_path or rel_path.startswith('/'):
            abort(400)

        full_path = os.path.join(UPLOAD_FOLDER, rel_path)
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            abort(404)

        if not rel_path.lower().endswith(VIDEO_EXTS):
            abort(400)

        cache = generate_thumbnail(full_path)
        if not cache or not os.path.exists(cache):
            abort(404)

        return send_file(cache, mimetype='image/jpeg', max_age=3600)

    @app.route('/thumbnail/regenerate', methods=['POST'])
    def thumbnail_regenerate():
        if not is_thumbnails_enabled():
            return jsonify({'error': 'Thumbnails are paused'}), 403
        rel_path = request.form.get('path', '')
        if not rel_path or '..' in rel_path or rel_path.startswith('/'):
            return jsonify({'error': 'Invalid path'}), 400
        full_path = os.path.join(UPLOAD_FOLDER, rel_path)
        if not os.path.exists(full_path):
            return jsonify({'error': 'File not found'}), 404
        cache = generate_thumbnail(full_path, force=True)
        if cache:
            return jsonify({'success': True})
        return jsonify({'error': 'Could not generate thumbnail'}), 500

    @app.route('/thumbnail/clear', methods=['POST'])
    def thumbnail_clear():
        """Delete all cached thumbnails."""
        count = 0
        for f in os.listdir(THUMB_CACHE_DIR):
            if f.endswith('.jpg'):
                try:
                    os.remove(os.path.join(THUMB_CACHE_DIR, f))
                    count += 1
                except Exception:
                    pass
        return jsonify({'cleared': count})

    @app.route('/thumbnail/settings', methods=['GET'])
    def thumbnail_settings_get():
        return jsonify({'enabled': is_thumbnails_enabled()})

    @app.route('/thumbnail/settings', methods=['POST'])
    def thumbnail_settings_post():
        enabled = request.form.get('enabled', 'true').lower() in ('true', '1', 'yes', 'on')
        set_thumbnails_enabled(enabled)
        logger.info(f"Thumbnails {'enabled' if enabled else 'paused'}")
        return jsonify({'enabled': enabled})
