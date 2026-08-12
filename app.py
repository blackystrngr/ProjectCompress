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
    remove_subscriber
)
from features import register_all_features

HEARTBEAT_SECONDS = 15

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Flask app factory
# ------------------------------------------------------------
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

    # ---------- Global error handler ----------
    @app.errorhandler(Exception)
    def handle_exception(e):
        logger.exception("Unhandled exception")
        return jsonify({'error': 'Internal server error'}), 500

    # ---------- Task endpoints ----------
    @app.route('/get_tasks', methods=['GET'])
    def get_tasks():
        try:
            return jsonify(get_active_tasks())
        except Exception as e:
            logger.error(f"Failed to list tasks: {e}")
            return jsonify([])

    # ---------- EVENT‑DRIVEN SSE ENDPOINT ----------
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

    # ---------- Progress and cancel endpoints ----------
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

# ------------------------------------------------------------
# Main entry
# ------------------------------------------------------------
if __name__ == '__main__':
    for tid in get_all_task_ids():
        if not load_task(tid):
            try:
                os.remove(os.path.join(TASKS_DIR, f"{tid}.json"))
            except:
                pass

    app = create_app()
    logger.info("Starting server on 0.0.0.0:5000")
    serve(
        app,
        host='0.0.0.0',
        port=5000,
        threads=32,
        channel_timeout=120,
    )
