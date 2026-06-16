#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import logging
import psutil
import time
import threading
import hmac
import hashlib
import subprocess
from flask import Flask, render_template, jsonify, request, send_from_directory
from waitress import serve
from config import SECRET_KEY, MAX_CONTENT_LENGTH, UPLOAD_FOLDER, TASKS_DIR
from tasks import get_all_task_ids, load_task, save_task
from features import register_all_features

# Webhook secret – set environment variable or use config
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'your-super-secret-webhook-key')

# Path to your SSH deploy key
GITHUB_DEPLOY_KEY = os.path.expanduser('~/.ssh/github_deploy')
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

    # Register all feature routes
    register_all_features(app)

    # ---------- Webhook endpoint – fetches and hard resets ----------
    @app.route('/webhook', methods=['POST'])
    def webhook():
        """GitHub webhook – fetches, resets to origin/main, and restarts."""
        # Verify signature (optional)
        signature = request.headers.get('X-Hub-Signature-256')
        if signature and WEBHOOK_SECRET:
            payload = request.get_data()
            expected = 'sha256=' + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                logger.warning("Invalid webhook signature")
                return jsonify({'error': 'Invalid signature'}), 401

        # Only react to 'push' events
        event = request.headers.get('X-GitHub-Event')
        if event != 'push':
            return jsonify({'message': 'Ignored event'}), 200

        logger.info("Received push event – fetching latest code...")

        # Set environment to use the deploy key
        env = os.environ.copy()
        env['GIT_SSH_COMMAND'] = f'ssh -i {GITHUB_DEPLOY_KEY} -o StrictHostKeyChecking=no'

        try:
            # 1. Fetch the latest changes
            fetch_cmd = ['git', 'fetch', 'origin', 'main']
            fetch_result = subprocess.run(
                fetch_cmd,
                cwd=REPO_DIR,
                capture_output=True,
                text=True,
                env=env
            )
            if fetch_result.returncode != 0:
                logger.error(f"Git fetch failed: {fetch_result.stderr}")
                return jsonify({'error': 'Git fetch failed', 'details': fetch_result.stderr}), 500
            logger.info(f"Git fetch succeeded: {fetch_result.stdout}")

            # 2. Reset the working directory to origin/main (discard local changes)
            reset_cmd = ['git', 'reset', '--hard', 'origin/main']
            reset_result = subprocess.run(
                reset_cmd,
                cwd=REPO_DIR,
                capture_output=True,
                text=True,
                env=env
            )
            if reset_result.returncode != 0:
                logger.error(f"Git reset failed: {reset_result.stderr}")
                return jsonify({'error': 'Git reset failed', 'details': reset_result.stderr}), 500
            logger.info(f"Git reset succeeded: {reset_result.stdout}")

        except Exception as e:
            logger.exception("Git operation exception")
            return jsonify({'error': str(e)}), 500

        # Restart the app in a background thread
        def restart():
            time.sleep(1)  # give response time to send
            logger.info("Restarting Flask app...")
            os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=restart, daemon=True).start()

        return jsonify({'status': 'updated, restarting...'}), 200

    # ---------- Global error handler ----------
    @app.errorhandler(Exception)
    def handle_exception(e):
        logger.exception("Unhandled exception")
        return jsonify({'error': 'Internal server error'}), 500

    # ---------- Task endpoints ----------
    @app.route('/get_tasks', methods=['GET'])
    def get_tasks():
        """Return only tasks that are NOT finished (terminal statuses)."""
        active = []
        # All statuses that should NOT appear in the Active Tasks pane
        terminal_statuses = {'done', 'error', 'cancelled', 'search_done', 'scan_done'}
        try:
            for tid in get_all_task_ids():
                try:
                    task = load_task(tid)
                    if task and task.get('status') not in terminal_statuses:
                        # Remove sensitive fields
                        safe_task = {k: v for k, v in task.items() if k not in ['process_pid']}
                        active.append(safe_task)
                except Exception as e:
                    logger.warning(f"Skipping invalid task {tid}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Failed to list tasks: {e}")
        return jsonify(active)

    @app.route('/progress/<task_id>', methods=['GET'])
    def progress(task_id):
        try:
            task = load_task(task_id)
            if not task:
                return jsonify({'error': 'Task not found'}), 404
            # Normalize progress values
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

    # ---------- Favicon: return 204 to avoid 404 errors ----------
    @app.route('/favicon.ico')
    def favicon():
        return '', 204

    return app

if __name__ == '__main__':
    # Clean up stale tasks
    for tid in get_all_task_ids():
        if not load_task(tid):
            try:
                os.remove(os.path.join(TASKS_DIR, f"{tid}.json"))
            except:
                pass

    app = create_app()
    logger.info("Starting server on 0.0.0.0:5000")
    serve(app, host='0.0.0.0', port=5000, threads=6)
