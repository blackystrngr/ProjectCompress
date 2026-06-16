import os
import re
import uuid
import threading
import time
import requests
import subprocess
import json
import glob
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, PROXY_DICT

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.m3u8', '.ts', '.mpd')
VIDEO_PATTERNS = [
    r'(https?://[^\s]+\.(?:mp4|webm|mkv|avi|mov|flv|m3u8|ts|mpd)(?:\?[^\s]*)?)',
    r'(https?://[^\s]+/video(?:_|\/)[^\s]+\.(?:mp4|m3u8))',
    r'(https?://[^\s]+\.cloudfront\.net/[^\s]+\.(?:mp4|m3u8))',
    r'(https?://[^\s]+\.akamaihd\.net/[^\s]+\.(?:mp4|m3u8))',
]

def extract_video_urls_from_html(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for video in soup.find_all('video'):
        src = video.get('src')
        if src:
            urls.add(urljoin(base_url, src))
        for source in video.find_all('source'):
            src = source.get('src')
            if src:
                urls.add(urljoin(base_url, src))
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src')
        if src:
            urls.add(urljoin(base_url, src))
    for a in soup.find_all('a', href=True):
        href = a['href']
        if any(href.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            urls.add(urljoin(base_url, href))
    for script in soup.find_all('script'):
        if script.string:
            text = script.string
            for pattern in VIDEO_PATTERNS:
                matches = re.findall(pattern, text, re.IGNORECASE)
                for m in matches:
                    urls.add(m)
            json_urls = re.findall(r'"(?:file|url|videoUrl|src|source|playlist)"\s*:\s*"([^"]+\.(?:mp4|m3u8|webm|mov|flv)[^"]*)"', text, re.IGNORECASE)
            for u in json_urls:
                urls.add(u)
    text = soup.get_text()
    for pattern in VIDEO_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            urls.add(m)
    absolute = set()
    for u in urls:
        if u.startswith(('http://', 'https://')):
            absolute.add(u)
        elif u.startswith('/'):
            absolute.add(urljoin(base_url, u))
    return list(absolute)

def extract_best_url(page_url, task_id):
    cmd = ['yt-dlp', '--get-url', '-f', 'best', '--no-warnings', '--ignore-errors', page_url]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=90)
        if process.returncode != 0:
            raise Exception(stderr[:200])
        urls = [line.strip() for line in stdout.splitlines() if line.strip().startswith(('http://', 'https://'))]
        task = load_task(task_id)
        task['video_urls'] = urls
        task['count'] = len(urls)
        task['combined_url'] = urls[0] if urls else None
        task['status'] = 'done'
        save_task(task_id, task)
    except Exception as e:
        task = load_task(task_id)
        task['status'] = 'error'
        task['error_msg'] = str(e)
        save_task(task_id, task)

def extract_with_ytdlp(page_url, task_id):
    task = load_task(task_id)
    task['status'] = 'ytdlp_extract'
    save_task(task_id, task)
    # Try to get a single combined format URL
    combined_cmds = [
        ['yt-dlp', '--get-url', '-f', 'best[ext=mp4]', '--no-warnings', '--ignore-errors', page_url],
        ['yt-dlp', '--get-url', '-f', 'best', '--no-warnings', '--ignore-errors', page_url],
    ]
    combined_url = None
    for cmd in combined_cmds:
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate(timeout=90)
            if process.returncode == 0:
                lines = [l.strip() for l in stdout.splitlines() if l.strip().startswith(('http://', 'https://'))]
                if len(lines) == 1:
                    combined_url = lines[0]
                    break
        except:
            continue
    if combined_url:
        task = load_task(task_id)
        task['video_urls'] = [combined_url]
        task['count'] = 1
        task['combined_url'] = combined_url
        task['status'] = 'done'
        save_task(task_id, task)
        return
    # fallback: get all URLs
    cmd = ['yt-dlp', '--get-url', '--flat-playlist', '--no-warnings', '--ignore-errors', page_url]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=90)
        if process.returncode != 0:
            raise Exception(stderr[:200])
        urls = [line.strip() for line in stdout.splitlines() if line.strip().startswith(('http://', 'https://'))]
        urls = list(dict.fromkeys(urls))
        task = load_task(task_id)
        task['video_urls'] = urls
        task['count'] = len(urls)
        task['combined_url'] = None
        task['status'] = 'done'
        save_task(task_id, task)
    except Exception as e:
        task = load_task(task_id)
        task['status'] = 'error'
        task['error_msg'] = str(e)
        save_task(task_id, task)

def fetch_and_extract(page_url, task_id, use_ytdlp):
    task = load_task(task_id)
    task['status'] = 'fetching'
    save_task(task_id, task)
    if use_ytdlp:
        extract_with_ytdlp(page_url, task_id)
        return
    proxies = PROXY_DICT if PROXY_DICT else None
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(page_url, headers=headers, proxies=proxies, timeout=30)
        resp.raise_for_status()
        html = resp.text
        urls = extract_video_urls_from_html(html, page_url)
        if urls:
            task = load_task(task_id)
            task['video_urls'] = urls
            task['count'] = len(urls)
            task['combined_url'] = urls[0] if len(urls) == 1 else None
            task['status'] = 'done'
            save_task(task_id, task)
            return
    except:
        pass
    extract_with_ytdlp(page_url, task_id)

def merge_fragments(base_path, fragment_pattern):
    fragments = sorted(glob.glob(fragment_pattern))
    if not fragments:
        return False
    concat_file = base_path + "_concat.txt"
    with open(concat_file, 'w') as f:
        for frag in fragments:
            f.write(f"file '{frag}'\n")
    output_path = base_path.replace('.part', '.mp4')
    cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_file, '-c', 'copy', '-y', output_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        for frag in fragments:
            os.remove(frag)
        os.remove(concat_file)
        if os.path.exists(base_path):
            os.remove(base_path)
        return output_path
    except:
        return False

def download_with_ytdlp(url, task_id):
    task = load_task(task_id)
    task['status'] = 'downloading'
    task['progress'] = 0
    save_task(task_id, task)

    temp_output = os.path.join(UPLOAD_FOLDER, f"{task_id}_download.mp4")
    cmd = [
        'yt-dlp', '-o', temp_output,
        '-f', 'bestvideo+bestaudio/best',
        '--merge-output-format', 'mp4',
        '--no-part',
        '--no-mtime',
        '--no-warnings', '--ignore-errors',
        url
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        line = process.stderr.readline()
        if not line and process.poll() is not None:
            break
        if '[download]' in line and '%' in line:
            match = re.search(r'(\d+(?:\.\d+)?)%', line)
            if match:
                pct = float(match.group(1))
                task = load_task(task_id)
                if task:
                    task['progress'] = int(pct)
                    save_task(task_id, task)
    process.wait()
    if process.returncode != 0:
        fragment_pattern = os.path.join(UPLOAD_FOLDER, f"{task_id}_download.mp4.part-Frag*.part")
        merged = merge_fragments(os.path.join(UPLOAD_FOLDER, f"{task_id}_download.mp4.part"), fragment_pattern)
        if merged:
            final_name = _get_unique_filename(os.path.basename(merged))
            final_path = os.path.join(UPLOAD_FOLDER, final_name)
            os.rename(merged, final_path)
            task = load_task(task_id)
            task['status'] = 'done'
            task['output_file'] = final_name
            task['progress'] = 100
            save_task(task_id, task)
            return
        else:
            task = load_task(task_id)
            task['status'] = 'error'
            task['error_msg'] = f"yt-dlp failed with code {process.returncode}"
            save_task(task_id, task)
            return
    if os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
        final_name = _get_unique_filename(os.path.basename(temp_output))
        final_path = os.path.join(UPLOAD_FOLDER, final_name)
        os.rename(temp_output, final_path)
        task = load_task(task_id)
        task['status'] = 'done'
        task['output_file'] = final_name
        task['progress'] = 100
        save_task(task_id, task)
    else:
        fragment_pattern = os.path.join(UPLOAD_FOLDER, f"{task_id}_download.mp4.part-Frag*.part")
        merged = merge_fragments(os.path.join(UPLOAD_FOLDER, f"{task_id}_download.mp4.part"), fragment_pattern)
        if merged:
            final_name = _get_unique_filename(os.path.basename(merged))
            final_path = os.path.join(UPLOAD_FOLDER, final_name)
            os.rename(merged, final_path)
            task = load_task(task_id)
            task['status'] = 'done'
            task['output_file'] = final_name
            task['progress'] = 100
            save_task(task_id, task)
        else:
            task = load_task(task_id)
            task['status'] = 'error'
            task['error_msg'] = "Download completed but no valid output file found"
            save_task(task_id, task)

def _get_unique_filename(filename):
    base, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(UPLOAD_FOLDER, new_name)):
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    return new_name

def register_routes(app):
    @app.route('/extract/fetch', methods=['POST'])
    def extract_fetch():
        page_url = request.form.get('url', '').strip()
        use_ytdlp = request.form.get('use_ytdlp', 'true').lower() == 'true'
        if not page_url:
            return jsonify({'error': 'URL required'}), 400
        if not page_url.startswith(('http://', 'https://')):
            page_url = 'https://' + page_url
        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'created_at': time.time(),
            'cancelled': False, 'page_url': page_url, 'use_ytdlp': use_ytdlp,
            'video_urls': [], 'count': 0, 'combined_url': None
        }
        save_task(task_id, task_data)
        threading.Thread(target=fetch_and_extract, args=(page_url, task_id, use_ytdlp), daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/extract/results/<task_id>')
    def extract_results(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({
            'status': task.get('status'),
            'video_urls': task.get('video_urls', []),
            'count': task.get('count', 0),
            'combined_url': task.get('combined_url'),
            'error_msg': task.get('error_msg')
        })

    @app.route('/extract/download', methods=['POST'])
    def extract_download():
        url = request.form.get('url')
        if not url:
            return jsonify({'error': 'URL required'}), 400
        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False
        }
        save_task(task_id, task_data)
        threading.Thread(target=download_with_ytdlp, args=(url, task_id), daemon=True).start()
        return jsonify({'task_id': task_id})
