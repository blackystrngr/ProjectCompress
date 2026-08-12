#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import subprocess
import time
import json
import hashlib
import logging
import psutil
import threading
import hmac
import queue
import shutil
from flask import Flask, render_template, jsonify, request, send_from_directory, Response, stream_with_context
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

logger = logging.getLogger(__name__)

# ========== INSTALLATION HELPER ==========
def ensure_ffmpeg():
    """Check if ffmpeg/ffprobe are available; if not, download and install."""
    def is_installed(cmd):
        return shutil.which(cmd) is not None

    if is_installed('ffmpeg') and is_installed('ffprobe'):
        logger.info("✅ ffmpeg and ffprobe found")
        return True

    logger.warning("⚠️ ffmpeg not found – attempting to install...")
    try:
        # Download static build
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
        subprocess.run(['wget', '-q', '--show-progress', url], check=True)
        subprocess.run(['tar', '-xf', 'ffmpeg-master-latest-linux64-gpl.tar.xz'], check=True)
        # Move binaries to /usr/local/bin (needs sudo)
        subprocess.run(['sudo', 'mv', 'ffmpeg-master-latest-linux64-gpl/bin/ffmpeg', '/usr/local/bin/'], check=True)
        subprocess.run(['sudo', 'mv', 'ffmpeg-master-latest-linux64-gpl/bin/ffplay', '/usr/local/bin/'], check=True)
        subprocess.run(['sudo', 'mv', 'ffmpeg-master-latest-linux64-gpl/bin/ffprobe', '/usr/local/bin/'], check=True)
        # Clean up
        subprocess.run(['rm', '-rf', 'ffmpeg-master-latest-linux64-gpl', 'ffmpeg-master-latest-linux64-gpl.tar.xz'], check=True)
        logger.info("✅ ffmpeg installed successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to install ffmpeg: {e}")
        logger.error("Please install manually: sudo apt install ffmpeg")
        return False

def ensure_python_dependencies():
    """Check and install missing Python packages from requirements.txt."""
    req_file = 'requirements.txt'
    if not os.path.exists(req_file):
        return
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', req_file], check=True)
        logger.info("✅ Python dependencies installed")
    except Exception as e:
        logger.error(f"❌ Failed to install Python dependencies: {e}")

# ========== APP SETUP ==========
HEARTBEAT_SECONDS = 15
WEBHOOK_SECRET = "atomisfake"
GITHUB_DEPLOY_KEY = os.path.expanduser('~/.ssh/github_deploy')
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_FILE = os.path.join(REPO_DIR, 'requirements.txt')
REQUIREMENTS_HASH_FILE = os.path.join(REPO_DIR, '.requirements_hash')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# --- Install dependencies at startup ---
if not ensure_ffmpeg():
    logger.warning("⚠️ ffmpeg missing – video clipping/extraction will not work")
ensure_python_dependencies()

# ========== Rest of your existing app.py ==========
# (Keep all your routes and helper functions exactly as they are,
#  including the webhook, tasks, SSE, etc.)
#
# Note: the rest of app.py remains unchanged.
# Just paste your existing code here.

# ========== MAIN ==========
if __name__ == '__main__':
    for tid in get_all_task_ids():
        if not load_task(tid):
            try:
                os.remove(os.path.join(TASKS_DIR, f"{tid}.json"))
            except:
                pass

    app = create_app()   # your existing create_app() function
    logger.info("Starting server on 0.0.0.0:5000")
    serve(app, host='0.0.0.0', port=5000, threads=32, channel_timeout=120)
