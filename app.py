#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import logging
import psutil
import threading
import queue
import re
import shutil
from flask import Flask, render_template, jsonify, request, Response, stream_with_context
from waitress import serve
from werkzeug.exceptions import NotFound
from config import SECRET_KEY, MAX_CONTENT_LENGTH, UPLOAD_FOLDER, TASKS_DIR
from tasks import (
    get_all_task_ids,
    load_task,
    save_task,
    get_active_tasks,
    add_subscriber,
    remove_subscriber,
    cleanup_old_tasks
)
from features import register_all_features

HEARTBEAT_SECONDS = 15

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

_last_net_sample = None
_net_sample_lock = threading.Lock()

COOKIE_UPLOAD_TOKEN = os.environ.get('COOKIE_UPLOAD_TOKEN', 'whyyouleftme')
COOKIES_SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
TOKEN_UPLOAD_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

    register_all_features(app)

    @app.errorhandler(NotFound)
    def handle_not_found(e):
        if request.path.startswith(('/api', '/get_tasks', '/progress', '/system_stats',
                                     '/upload_cookies', '/upload_token')):
            return jsonify({'error': 'Endpoint not found'}), 404
        return render_template('index.html'), 404

    @app.errorhandler(Exception)
    def handle_exception(e):
        logger.exception("Unhandled exception")
        return jsonify({'error': 'Internal server error'}), 500

    @app.route('/get_tasks', methods=['GET'])
    def get_tasks():
        try:
            return jsonify(get_active_tasks())
        except Exception as e:
            logger.error(f"Failed to list tasks: {e}")
            return jsonify([])

    @app.route('/tasks/stream')
    def tasks_stream():
        q = queue.Queue(maxsize=1)
        add_subscriber(q)

        def event_stream():
            try:
                initial = json.dumps(get_active_tasks())
                yield f"data: {initial}\n\n"

                while True:
                    try:
                        data = q.get(timeout=HEARTBEAT_SECONDS)
                        yield f"data: {data}\n\n"
                    except queue.Empty:
                        yield ": heartbeat\n\n"
            except GeneratorExit:
                pass
            finally:
                remove_subscriber(q)

        return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

    @app.route('/system_stats')
    def system_stats():
        global _last_net_sample
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            net = psutil.net_io_counters()

            now = time.time()
            up_bps = 0
            down_bps = 0

            with _net_sample_lock:
                if _last_net_sample is not None:
                    prev_time, prev_sent, prev_recv = _last_net_sample
                    dt = now - prev_time
                    if dt > 0.1:
                        up_bps = max(0, (net.bytes_sent - prev_sent) / dt)
                        down_bps = max(0, (net.bytes_recv - prev_recv) / dt)
                _last_net_sample = (now, net.bytes_sent, net.bytes_recv)

            return jsonify({
                'cpu': round(cpu, 1),
                'ram': round(mem.percent, 1),
                'ram_used': mem.used,
                'ram_total': mem.total,
                'disk': round(disk.percent, 1),
                'net_up': int(up_bps),
                'net_down': int(down_bps),
                'net_total_sent': net.bytes_sent,
                'net_total_recv': net.bytes_recv,
            })
        except Exception as e:
            logger.error(f"system_stats error: {e}")
            return jsonify({
                'cpu': 0, 'ram': 0, 'disk': 0,
                'net_up': 0, 'net_down': 0,
                'ram_used': 0, 'ram_total': 1,
            })

    @app.route('/progress/<task_id>', methods=['GET'])
    def progress(task_id):
        try:
            task = load_task(task_id)
            if not task:
                return jsonify({'error': 'Task not found'}), 404
            if 'download_progress' in task:
                task['download_progress'] = int(task['download_progress'])
            if 'upload_progress' in task:
                task['upload_progress'] = int(task['upload_progress'])
            if 'progress' in task:
                task['progress'] = int(task['progress'])
            return jsonify({k: v for k, v in task.items() if k not in ['process_pid']})
        except Exception as e:
            logger.error(f"Failed to get progress for {task_id}: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/cancel/<task_id>', methods=['POST'])
    def cancel(task_id):
        try:
            task = load_task(task_id)
            if not task:
                return jsonify({'error': 'Task not found'}), 404

            task['cancelled'] = True
            task['status'] = 'cancelled'
            save_task(task_id, task)

            killed = []

            try:
                from features.url_download import kill_process as kill_dl_process
                if kill_dl_process(task_id):
                    killed.append('yt-dlp')
            except Exception as e:
                logger.debug(f"kill_dl_process failed: {e}")

            try:
                from features.telegram import cancel_telegram_task
                if cancel_telegram_task(task_id):
                    killed.append('telegram')
            except Exception as e:
                logger.debug(f"cancel_telegram_task failed: {e}")

            logger.info(f"Task {task_id} cancelled – killed: {killed}")
            return jsonify({'status': 'cancelling', 'killed': killed})
        except Exception as e:
            logger.error(f"Failed to cancel task {task_id}: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/upload_cookies', methods=['POST'])
    def upload_cookies():
        token = request.headers.get('X-Upload-Token', '')
        if not token or token != COOKIE_UPLOAD_TOKEN:
            logger.warning("Cookie upload: invalid or missing token")
            return jsonify({'error': 'Unauthorized'}), 401

        content = request.get_data(as_text=True)
        if not content or len(content) < 50:
            return jsonify({'error': 'Empty or too-short cookie data'}), 400

        # ---- (REMOVED the "youtube.com required" check) ----

        try:
            if os.path.exists(COOKIES_SAVE_PATH):
                shutil.copy(COOKIES_SAVE_PATH, COOKIES_SAVE_PATH + '.bak')

            if not content.lstrip().startswith('# Netscape HTTP Cookie File'):
                content = '# Netscape HTTP Cookie File\n# Uploaded by extension\n' + content

            with open(COOKIES_SAVE_PATH, 'w', encoding='utf-8') as f:
                f.write(content)

            cookie_lines = [l for l in content.splitlines()
                            if l and not l.startswith('#') and '\t' in l]

            # Count unique domains
            domains = set()
            for line in cookie_lines:
                parts = line.split('\t')
                if parts:
                    domains.add(parts[0])

            logger.info(f"Cookies updated: {len(cookie_lines)} entries across "
                        f"{len(domains)} domains ({len(content)} bytes)")

            return jsonify({
                'status': 'ok',
                'cookies': len(cookie_lines),
                'domains': len(domains),
                'bytes': len(content),
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            })
        except Exception as e:
            logger.exception("Failed to save cookies")
            return jsonify({'error': str(e)}), 500

    @app.route('/upload_token', methods=['POST'])
    def upload_token():
        token_header = request.headers.get('X-Upload-Token', '')
        if not token_header or token_header != COOKIE_UPLOAD_TOKEN:
            logger.warning("Token upload: invalid or missing token")
            return jsonify({'error': 'Unauthorized'}), 401

        account = (request.headers.get('X-Account-Name') or 'default').strip()
        if not re.match(r'^[a-zA-Z0-9_-]{1,40}$', account):
            return jsonify({'error': 'Invalid account name '
                                     '(letters, digits, _ and - only)'}), 400

        content = request.get_data(as_text=True)
        if not content or len(content) < 50:
            return jsonify({'error': 'Empty or too-short token data'}), 400

        try:
            data = json.loads(content)
        except Exception:
            return jsonify({'error': 'Not valid JSON'}), 400

        # Accept OAuth user tokens OR service account keys
        required_oauth = {'client_id', 'client_secret', 'refresh_token'}
        required_sa = {'type', 'private_key', 'client_email'}
        keys = set(data.keys())
        if not (required_oauth.issubset(keys) or required_sa.issubset(keys)):
            return jsonify({'error':
                'Not a valid OAuth user token or service account key'}), 400

        filename = 'token.json' if account == 'default' else f'token_{account}.json'
        save_path = os.path.join(TOKEN_UPLOAD_DIR, filename)

        if os.path.exists(save_path):
            try:
                shutil.copy(save_path, save_path + '.bak')
            except Exception as e:
                logger.warning(f"Token backup failed: {e}")

        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            logger.exception("Failed to write token file")
            return jsonify({'error': str(e)}), 500

        # Invalidate cached Drive service so next request uses the new token
        email = None
        try:
            from features.google_drive import (
                _service_cache, _email_cache, _quota_cache, _cache_lock,
                get_drive_service
            )
            with _cache_lock:
                _service_cache.pop(account, None)
                _email_cache.pop(account, None)
                _quota_cache.pop(account, None)
            # Verify + grab email
            svc = get_drive_service(account)
            about = svc.about().get(fields='user').execute()
            email = about['user']['emailAddress']
        except Exception as e:
            logger.warning(f"Token saved but verify failed: {e}")

        logger.info(f"Token uploaded: account={account} file={filename} "
                    f"email={email} bytes={len(content)}")

        return jsonify({
            'status': 'ok',
            'account': account,
            'filename': filename,
            'bytes': len(content),
            'email': email,
            'saved_at': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        })

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/favicon.ico')
    def favicon():
        return '', 204

    return app


if __name__ == '__main__':
    # Cleanup old terminal task files (older than 1 day)
    try:
        cleanup_old_tasks(max_age_seconds=86400)
    except Exception as e:
        logger.warning(f"cleanup_old_tasks failed: {e}")

    # ---- Kill any orphan playwright/chromium from previous runs ----
    try:
        from features.url_download import _cleanup_playwright_processes
        killed = _cleanup_playwright_processes(verbose=True)
        if killed:
            logger.warning(f"Startup: killed {killed} orphan browser process(es)")
    except Exception as e:
        logger.debug(f"Startup browser cleanup failed: {e}")

    app = create_app()
    logger.info("Starting server on 0.0.0.0:5000")
    serve(
        app,
        host='0.0.0.0',
        port=5000,
        threads=32,
        channel_timeout=120,
    )
