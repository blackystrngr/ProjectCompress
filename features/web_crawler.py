import os
import re
import uuid
import threading
import time
import logging
import json
import requests
from urllib.parse import urljoin, urlparse
from flask import request, jsonify, send_file, Response, stream_with_context
from bs4 import BeautifulSoup
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

# ====================== GROK-STYLE OPTIMIZATIONS ======================
UPDATE_INTERVAL = 7
MAX_BATCH_SIZE = 40
INACTIVITY_TIMEOUT = 30
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

IPV4_RE = re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')
IPV6_RE = re.compile(r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b')

SKIP_EXTENSIONS = {'.jpg','.png','.gif','.svg','.mp4','.pdf','.zip','.js','.css','.json'}

def is_valid_domain(domain):
    if not domain or len(domain) < 4:
        return False
    domain = domain.lower().strip()
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', domain):
        return False
    pattern = re.compile(r'^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$')
    return bool(pattern.match(domain)) and not any(domain.endswith(ext) for ext in SKIP_EXTENSIONS)

def clean_domain(domain):
    if not domain: return None
    domain = domain.lower()
    if domain.startswith('www.'): domain = domain[4:]
    domain = domain.rstrip('.')
    return domain if is_valid_domain(domain) else None

def extract_domain(url):
    try:
        return clean_domain(urlparse(url).netloc)
    except:
        return None

def is_valid_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ('http', 'https') and p.netloc
    except:
        return False

def normalize_url(url, base):
    try:
        if url.startswith('//'): url = 'https:' + url
        return urljoin(base, url)
    except:
        return None

def extract_urls_from_html(html, base_url):
    urls = set()
    soup = BeautifulSoup(html, 'html.parser', parse_only=True)
    for tag in soup.find_all(['a', 'link', 'script', 'img', 'iframe', 'source']):
        for attr in ['href', 'src', 'data-src']:
            val = tag.get(attr)
            if val:
                u = normalize_url(val, base_url)
                if u and is_valid_url(u):
                    urls.add(u)
    return urls

def run_crawler(task_id, start_url, max_pages, max_depth):
    task = load_task(task_id)
    if not task: return

    target = extract_domain(start_url)
    domains = set()
    processed = set()
    queue = [(start_url, 0)]
    all_urls = {start_url}
    if d := extract_domain(start_url):
        domains.add(d)

    pages = 0
    last_update = time.time()
    last_count = 0

    task.update({'status': 'crawling', 'progress': 0, 'current_url': start_url, 'last_client_seen': time.time()})
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    while queue and (pages < max_pages or max_pages == 0):
        task = load_task(task_id)
        if not task or task.get('cancelled') or time.time() - task.get('last_client_seen', 0) > INACTIVITY_TIMEOUT:
            task['status'] = 'cancelled' if not task.get('cancelled') else task['status']
            save_task(task_id, task)
            return

        url, depth = queue.pop(0)
        if url in processed: continue
        processed.add(url)

        try:
            resp = session.get(url, allow_redirects=True, timeout=10)
            if resp.status_code == 200 and 'text/html' in resp.headers.get('content-type', ''):
                for u in extract_urls_from_html(resp.text, url):
                    all_urls.add(u)
                    dom = extract_domain(u)
                    if dom and dom != target:
                        domains.add(dom)
                    if is_same_domain(u, target) and depth + 1 <= max_depth:
                        queue.append((u, depth + 1))

            pages += 1

            # Grok-style smart update
            now = time.time()
            if (now - last_update > UPDATE_INTERVAL and len(domains) - last_count >= 5) or pages % 30 == 0:
                batch = sorted(list(domains))[-MAX_BATCH_SIZE:]
                task.update({
                    'progress': int(100 * pages / max_pages) if max_pages > 0 else min(98, pages),
                    'total_pages': pages,
                    'domains': batch,
                    'current_url': url,
                    'last_client_seen': time.time()
                })
                save_task(task_id, task)
                last_update = now
                last_count = len(domains)

        except:
            continue

    # Final
    final_domains = sorted(domains)
    task.update({'status': 'done', 'progress': 100, 'domains': final_domains, 'total_pages': pages})
    save_task(task_id, task)

    # Save results
    name = urlparse(start_url).netloc.replace('www.', '').split('.')[0]
    with open(os.path.join(UPLOAD_FOLDER, f"{name}_domains.txt"), 'w', encoding='utf-8') as f:
        f.write('\n'.join(final_domains))

def is_same_domain(url, target):
    try:
        d = urlparse(url).netloc.lower()
        if d.startswith('www.'): d = d[4:]
        t = target.lower()
        if t.startswith('www.'): t = t[4:]
        return d == t or d.endswith('.' + t)
    except:
        return False

# ====================== ROUTES ======================
def register_routes(app):
    @app.route('/crawler/start', methods=['POST'])
    def crawler_start():
        url = request.form.get('start_url', '').strip()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url.lstrip(':/')
        if not is_valid_url(url):
            return jsonify({'error': 'Invalid URL'}), 400

        max_pages = int(request.form.get('max_pages', 0)) or 999999
        max_depth = max(1, min(int(request.form.get('max_depth', 3)), 10))

        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'start_url': url,
            'max_pages': max_pages,
            'max_depth': max_depth,
            'last_client_seen': time.time()
        })

        threading.Thread(target=run_crawler, args=(task_id, url, max_pages, max_depth), daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/crawler/cancel/<task_id>', methods=['POST'])
    def crawler_cancel(task_id):
        task = load_task(task_id)
        if task:
            task['cancelled'] = True
            save_task(task_id, task)
        return jsonify({'status': 'ok'})

    @app.route('/crawler/stream/<task_id>')
    def crawler_stream(task_id):
        def generate():
            last_sent = 0
            while True:
                task = load_task(task_id)
                if not task: 
                    yield "event: error\ndata: Task not found\n\n"
                    break

                task['last_client_seen'] = time.time()
                save_task(task_id, task)

                status = task.get('status')
                domains = task.get('domains', [])
                new_domains = domains[last_sent:]

                if new_domains or status in ('done', 'cancelled'):
                    payload = {
                        'domains': new_domains[:MAX_BATCH_SIZE],
                        'path': urlparse(task.get('current_url', '')).path or '/',
                        'progress': task.get('progress', 0),
                        'pages': task.get('total_pages', 0)
                    }
                    yield f"event: update\ndata: {json.dumps(payload)}\n\n"
                    if new_domains:
                        last_sent = len(domains)

                if status in ('done', 'cancelled'):
                    yield f"event: {status}\ndata: \n\n"
                    break

                time.sleep(6)

        return Response(stream_with_context(generate()), mimetype="text/event-stream")

    @app.route('/crawler/download_domains/<task_id>')
    def download_domains(task_id):
        task = load_task(task_id)
        if not task: return jsonify({'error': 'Not found'}), 404
        name = urlparse(task.get('start_url', '')).netloc.replace('www.', '').split('.')[0]
        path = os.path.join(UPLOAD_FOLDER, f"{name}_domains.txt")
        if os.path.exists(path):
            return send_file(path, as_attachment=True)
        return jsonify({'error': 'File not found'}), 404
