#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import hashlib
import logging
import psutil
import threading
import hmac
import subprocess
from flask import Flask, render_template, jsonify, request, send_from_directory, Response, stream_with_context
from waitress import serve
from werkzeug.exceptions import NotFound
from config import SECRET_KEY, MAX_CONTENT_LENGTH, UPLOAD_FOLDER, TASKS_DIR
from tasks import get_all_task_ids, load_task, save_task
from features import register_all_features

WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'your-super-secret-webhook-key')
GITHUB_DEPLOY_KEY = os.path.expanduser('~/.ssh/github_deploy')
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_FILE = os.path.join(REPO_DIR, 'requirements.txt')
REQUIREMENTS_HASH_FILE = os.path.join(REPO_DIR, '.requirements_hash')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def get_file_hash(path):
    """Return SHA-256 hash of a file, or None if not exists."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except:
        return None

def install_requirements():
    """Run pip install -r requirements.txt and save hash."""
    logger.info("Installing/updating dependencies from requirements.txt")
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-r', REQUIREMENTS_FILE],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode == 0:
            logger.info("Dependencies installed successfully.")
            # Save hash for future checks
            current_hash = get_file_hash(REQUIREMENTS_FILE)
            if current_hash:
                with open(REQUIREMENTS_HASH_FILE, 'w') as f:
                    f.write(current_hash)
        else:
            logger.error(f"pip install failed: {result.stderr}")
    except Exception as e:
        logger.exception(f"Failed to install requirements: {e}")

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

    register_all_features(app)

    # ---------- 404 handler ----------
    @app.errorhandler(NotFound)
    def handle_not_found(e):
        if request.path.startswith(('/api', '/get_tasks', '/progress')):
            return jsonify({'error': 'Endpoint not found'}), 404
        return render_template('index.html'), 404

    # ---------- Webhook endpoint (full renewal) ----------
    @app.route('/webhook', methods=['POST'])
    def webhook():
        payload = request.get_data()
        signature = request.headers.get('X-Hub-Signature-256')

        # Verify signature if secret is set
        if WEBHOOK_SECRET and WEBHOOK_SECRET != 'your-super-secret-webhook-key':
            if not signature:
                logger.warning("Webhook signature missing")
                return jsonify({'error': 'Signature missing'}), 401
            expected = 'sha256=' + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                logger.warning(f"Invalid webhook signature. Expected {expected}, got {signature}")
                return jsonify({'error': 'Invalid signature'}), 401
        else:
            logger.info("Webhook secret not set – skipping signature verification (development mode)")

        event = request.headers.get('X-GitHub-Event')
        if event != 'push':
            return jsonify({'message': 'Ignored event'}), 200

        logger.info("Received push event – starting full project renewal...")

        # Prepare Git environment
        env = os.environ.copy()
        if os.path.exists(GITHUB_DEPLOY_KEY):
            env['GIT_SSH_COMMAND'] = f'ssh -i {GITHUB_DEPLOY_KEY} -o StrictHostKeyChecking=no'
        else:
            logger.warning(f"Deploy key not found at {GITHUB_DEPLOY_KEY} – using default SSH.")

        # 1. Pull latest code
        try:
            fetch_cmd = ['git', 'fetch', 'origin', 'main']
            fetch_result = subprocess.run(fetch_cmd, cwd=REPO_DIR, capture_output=True, text=True, env=env)
            if fetch_result.returncode != 0:
                logger.error(f"Git fetch failed: {fetch_result.stderr}")
                return jsonify({'error': 'Git fetch failed', 'details': fetch_result.stderr}), 500

            reset_cmd = ['git', 'reset', '--hard', 'origin/main']
            reset_result = subprocess.run(reset_cmd, cwd=REPO_DIR, capture_output=True, text=True, env=env)
            if reset_result.returncode != 0:
                logger.error(f"Git reset failed: {reset_result.stderr}")
                return jsonify({'error': 'Git reset failed', 'details': reset_result.stderr}), 500

            logger.info(f"Git reset succeeded: {reset_result.stdout}")
        except Exception as e:
            logger.exception("Git operation exception")
            return jsonify({'error': str(e)}), 500

        # 2. Check if requirements.txt changed (reinstall if needed)
        old_hash = None
        if os.path.exists(REQUIREMENTS_HASH_FILE):
            with open(REQUIREMENTS_HASH_FILE, 'r') as f:
                old_hash = f.read().strip()
        current_hash = get_file_hash(REQUIREMENTS_FILE)

        if current_hash and current_hash != old_hash:
            logger.info("requirements.txt changed – reinstalling dependencies.")
            install_requirements()
        else:
            logger.info("requirements.txt unchanged – skipping pip install.")

        # 3. Clean up stale task files (optional)
        for tid in get_all_task_ids():
            if not load_task(tid):
                try:
                    os.remove(os.path.join(TASKS_DIR, f"{tid}.json"))
                except:
                    pass

        # 4. Restart the app
        def restart():
            time.sleep(1)
            logger.info("Restarting Flask app...")
            os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=restart, daemon=True).start()
        return jsonify({'status': 'updated, dependencies installed (if needed), restarting...'}), 200

    # ---------- Global error handler ----------
    @app.errorhandler(Exception)
    def handle_exception(e):
        logger.exception("Unhandled exception")
        return jsonify({'error': 'Internal server error'}), 500

    # ---------- Task endpoints ----------
    @app.route('/get_tasks', methods=['GET'])
    def get_tasks():
        active = []
        terminal_statuses = {'done', 'error', 'cancelled', 'search_done', 'scan_done'}
        try:
            for tid in get_all_task_ids():
                try:
                    task = load_task(tid)
                    if task and task.get('status') not in terminal_statuses:
                        safe_task = {k: v for k, v in task.items() if k not in ['process_pid']}
                        active.append(safe_task)
                except Exception as e:
                    logger.warning(f"Skipping invalid task {tid}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Failed to list tasks: {e}")
        return jsonify(active)

    @app.route('/tasks/stream')
    def tasks_stream():
        def event_stream():
            last_sent = None
            while True:
                active = []
                terminal_statuses = {'done', 'error', 'cancelled', 'search_done', 'scan_done'}
                try:
                    for tid in get_all_task_ids():
                        task = load_task(tid)
                        if task and task.get('status') not in terminal_statuses:
                            safe = {k: v for k, v in task.items() if k not in ['process_pid']}
                            active.append(safe)
                except Exception as e:
                    logger.error(f"Error in SSE stream: {e}")
                    active = []
                current = json.dumps(active)
                if current != last_sent:
                    last_sent = current
                    yield f"data: {current}\n\n"
                time.sleep(1)
        return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

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
            logger.info(f"Task {task_id} cancelled")
            return jsonify({'status': 'cancelling'})
        except Exception as e:
            logger.error(f"Failed to cancel task {task_id}: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/kill_all_ffmpeg', methods=['POST'])
    def kill_all_ffmpeg():
        killed = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'ffmpeg' in (proc.info['name'] or '') or 'ffmpeg' in ' '.join(proc.info['cmdline'] or []):
                    proc.terminate()
                    killed.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        logger.info(f"Killed ffmpeg: {killed}")
        return jsonify({'status': 'killed', 'pids': killed})

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/favicon.ico')
    def favicon():
        return '', 204

    return app

if __name__ == '__main__':
    for tid in get_all_task_ids():
        if not load_task(tid):
            try:
                os.remove(os.path.join(TASKS_DIR, f"{tid}.json"))
            except:
                pass

    app = create_app()
    logger.info("Starting server on 0.0.0.0:5000")
    serve(app, host='0.0.0.0', port=5000, threads=6)
