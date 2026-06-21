import os
import re
import uuid
import threading
import time
import logging
import json
import queue
import requests
from urllib.parse import urljoin, urlparse
from flask import request, jsonify, send_file, Response, stream_with_context, abort
from bs4 import BeautifulSoup
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

# ====================== CONSTANTS ======================
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
MAX_BATCH_SIZE = 50   # max new domains sent per SSE update

IPV4_RE = re.compile(r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b')
IPV6_RE = re.compile(r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b')

SKIP_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico', '.webp', '.bmp',
    '.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv',
    '.mp3', '.wav', '.flac', '.aac', '.ogg',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.gz', '.rar', '.7z',
    '.ttf', '.woff', '.woff2', '.eot',
    '.js', '.css', '.xml', '.json', '.php', '.asp', '.aspx', '.jsp',
    '.do', '.action', '.cgi', '.pl', '.py', '.rb', '.go', '.java',
    '.class', '.jar', '.war', '.ear',
    '.rss', '.atom', '.feed', '.conf', '.cfg', '.ini', '.yaml', '.yml',
    '.toml', '.htaccess', '.htpasswd', '.sql', '.db', '.sqlite',
    '.log', '.tmp', '.bak', '.exe', '.dll', '.so', '.dylib',
    '.pem', '.key', '.crt', '.csr'
}

# ---------- SSE queues per task ----------
_crawler_queues = {}
_queue_lock = threading.Lock()

def get_queue(task_id):
    """Get or create a queue for a crawler task."""
    with _queue_lock:
        if task_id not in _crawler_queues:
            _crawler_queues[task_id] = queue.Queue(maxsize=100)
        return _crawler_queues[task_id]

def remove_queue(task_id):
    """Remove queue when task finishes or client disconnects."""
    with _queue_lock:
        if task_id in _crawler_queues:
            del _crawler_queues[task_id]

def push_update(task_id, payload):
    """Push an update to the task's SSE queue (non‑blocking)."""
    q = get_queue(task_id)
    try:
        q.put_nowait(payload)
    except queue.Full:
        # If queue is full, discard oldest message to keep it fresh
        try:
            q.get_nowait()
            q.put_nowait(payload)
        except:
            pass

# ---------- Helpers ----------
def is_valid_domain(domain):
    if not domain or not isinstance(domain, str):
        return False
    domain = domain.lower().strip()
    if '.' not in domain:
        return False
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', domain):
        return False
    if any(c in domain for c in ['/', '?', '=', '&', '%', '#', ';', ':', '@']):
        return False
    for ext in SKIP_EXTENSIONS:
        if domain.endswith(ext):
            return False
    pattern = re.compile(r'^([a-z0-9]([a-z0-9-]*[a-z0-9])?)\.([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z]{2,}$')
    if not pattern.match(domain):
        return False
    parts = domain.split('.')
    if len(parts) < 2 or len(parts[0]) < 2:
        return False
    if domain in ('localhost', 'local', 'internal', 'intranet', 'router', 'gateway'):
        return False
    return True

def clean_domain(domain):
    if not domain:
        return None
    domain = domain.lower()
    if ':' in domain:
        domain = domain.split(':')[0]
    if domain.startswith('www.'):
        domain = domain[4:]
    domain = domain.rstrip('.')
    if is_valid_domain(domain):
        return domain
    return None

def extract_domain(url):
    try:
        parsed = urlparse(url)
        dom = parsed.netloc.lower()
        if not dom:
            return None
        return clean_domain(dom)
    except:
        return None

def is_valid_url(url):
    if not url:
        return False
    url = url.rstrip('.,;:!?)]}')
    try:
        parsed = urlparse(url)
        return parsed.scheme in ('http', 'https') and parsed.netloc
    except:
        return False

def normalize_url(url, base):
    try:
        if url.startswith('//'):
            url = 'https:' + url
        return urljoin(base, url)
    except:
        return None

def get_website_name_from_url(url):
    parsed = urlparse(url)
    domain = parsed.netloc
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain

def is_same_domain(url, target_domain):
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return False
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        if ':' in domain:
            domain = domain.split(':')[0]
        target = target_domain.lower()
        if target.startswith('www.'):
            target = target[4:]
        return domain == target or domain.endswith('.' + target)
    except:
        return False

def extract_urls_from_html(html, base_url):
    urls = set()
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.find_all(['a', 'link', 'script', 'img', 'iframe', 'source', 'object', 'embed']):
        src = tag.get('href') or tag.get('src') or tag.get('data')
        if src:
            abs_url = normalize_url(src, base_url)
            if abs_url and is_valid_url(abs_url):
                urls.add(abs_url)
    for tag in soup.find_all(['img', 'source']):
        srcset = tag.get('srcset')
        if srcset:
            for part in srcset.split(','):
                part = part.strip().split(' ')[0]
                if part:
                    abs_url = normalize_url(part, base_url)
                    if abs_url and is_valid_url(abs_url):
                        urls.add(abs_url)
    for tag in soup.find_all():
        for attr in tag.attrs:
            if attr.startswith('data-') and ('src' in attr or 'url' in attr):
                val = tag.get(attr)
                if val and isinstance(val, str) and (val.startswith('http') or val.startswith('/')):
                    abs_url = normalize_url(val, base_url)
                    if abs_url and is_valid_url(abs_url):
                        urls.add(abs_url)
    base = soup.find('base')
    if base and base.get('href'):
        abs_url = normalize_url(base['href'], base_url)
        if abs_url and is_valid_url(abs_url):
            urls.add(abs_url)
    for meta in soup.find_all('meta'):
        content = meta.get('content')
        if content:
            prop = meta.get('property') or meta.get('name')
            if prop and prop.lower() in ('og:url', 'twitter:url'):
                abs_url = normalize_url(content, base_url)
                if abs_url and is_valid_url(abs_url):
                    urls.add(abs_url)
    for script in soup.find_all('script'):
        if script.string:
            for match in re.finditer(r'[\'"](https?://[^\'"]+)[\'"]', script.string):
                url = match.group(1).strip()
                abs_url = normalize_url(url, base_url)
                if abs_url and is_valid_url(abs_url):
                    urls.add(abs_url)
    return urls

def extract_ips_from_text(text):
    ips = set()
    ips.update(IPV4_RE.findall(text))
    ips.update(IPV6_RE.findall(text))
    return ips

# ---------- File writing (thread‑safe) ----------
_file_write_lock = threading.Lock()

def append_to_file(filepath, lines):
    with _file_write_lock:
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                for line in lines:
                    f.write(line.rstrip() + '\n')
        except Exception as e:
            logger.error(f"Error writing to {filepath}: {e}")

# ---------- Crawler core ----------
def run_crawler(task_id, start_url, max_pages, max_depth):
    logger.info(f"Crawler task {task_id} started: {start_url}")
    task = load_task(task_id)
    if not task:
        return

    target_domain = extract_domain(start_url)
    if not target_domain:
        logger.error(f"Could not extract domain from {start_url}")
        return

    # Prepare output file (real‑time)
    website_name = get_website_name_from_url(start_url)
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', website_name)
    base_filename = f"{safe_name}_{task_id[:8]}_domains.txt"
    filepath = os.path.join(UPLOAD_FOLDER, base_filename)

    # Empty the file at start
    with open(filepath, 'w', encoding='utf-8') as f:
        pass

    all_urls = set([start_url])
    domains = set()
    ip_addresses = set()
    processed = set()
    queue = [(start_url, 0)]

    if dom := extract_domain(start_url):
        domains.add(dom)
        append_to_file(filepath, [dom])

    pages_visited = 0
    last_domain_count = 0

    # Initial task state
    task.update({
        'status': 'crawling',
        'progress': 0,
        'total_pages': 0,
        'total_urls': len(all_urls),
        'max_pages': max_pages,
        'max_depth': max_depth,
        'domains': sorted(domains),
        'current_url': start_url,
        'output_file': base_filename
    })
    save_task(task_id, task)

    # Push initial update
    push_update(task_id, {
        'domains': sorted(domains),
        'path': '/',
        'progress': 0,
        'pages': 0,
        'urls_count': len(all_urls)
    })

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    while queue and (pages_visited < max_pages or max_pages == 0):
        task = load_task(task_id)
        if not task or task.get('cancelled', False):
            return

        url, depth = queue.pop(0)
        if url in processed:
            continue
        processed.add(url)

        try:
            resp = session.get(url, allow_redirects=True, timeout=15)
            if resp.status_code != 200:
                continue

            content_type = resp.headers.get('content-type', '').lower()
            text = resp.text

            if 'text/html' in content_type:
                ips = extract_ips_from_text(text)
                ip_addresses.update(ips)
                extracted_urls = extract_urls_from_html(text, url)

                new_domains = []
                for extracted_url in extracted_urls:
                    all_urls.add(extracted_url)
                    dom = extract_domain(extracted_url)
                    if dom and dom != target_domain and dom not in domains:
                        domains.add(dom)
                        new_domains.append(dom)
                    if is_same_domain(extracted_url, target_domain) and depth + 1 <= max_depth:
                        queue.append((extracted_url, depth + 1))

                # Write new domains immediately
                if new_domains:
                    append_to_file(filepath, new_domains)

                # Also write IPs if any
                if ips:
                    append_to_file(filepath, [f"IP: {ip}" for ip in ips])
                    ip_addresses.update(ips)

                # --- PUSH REAL‑TIME UPDATE ---
                if new_domains or ips:
                    # Prepare a batch of up to MAX_BATCH_SIZE new domains
                    batch = new_domains[:MAX_BATCH_SIZE]
                    payload = {
                        'domains': batch,
                        'path': urlparse(url).path or '/',
                        'progress': int(100 * pages_visited / max_pages) if max_pages > 0 else min(98, pages_visited),
                        'pages': pages_visited + 1,
                        'urls_count': len(all_urls)
                    }
                    push_update(task_id, payload)

                    # Update task for persistence (and global /progress)
                    task = load_task(task_id)
                    if task:
                        task['total_pages'] = pages_visited + 1
                        task['total_urls'] = len(all_urls)
                        task['domains'] = sorted(domains)
                        task['progress'] = payload['progress']
                        task['current_url'] = url
                        save_task(task_id, task)

            pages_visited += 1

        except Exception as e:
            logger.debug(f"Fetch error {url}: {e}")
            continue

    # Final save
    combined = sorted(set(domains) | set(ip_addresses))
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(combined))
        if combined:
            f.write('\n')

    task = load_task(task_id)
    task.update({
        'status': 'done',
        'progress': 100,
        'total_pages': pages_visited,
        'total_urls': len(all_urls),
        'domains': combined,
        'current_url': None,
        'output_file': base_filename
    })
    save_task(task_id, task)

    # Push final update
    push_update(task_id, {
        'domains': combined[-MAX_BATCH_SIZE:],  # send last batch
        'path': '/',
        'progress': 100,
        'pages': pages_visited,
        'urls_count': len(all_urls)
    })

    logger.info(f"Crawler {task_id} finished: {len(all_urls)} URLs, {len(combined)} domains/IPs")

    # Clean up queue after a short delay (allow clients to receive final message)
    time.sleep(2)
    remove_queue(task_id)

# ---------- Flask routes ----------
def register_routes(app):

    @app.route('/crawler/start', methods=['POST'])
    def crawler_start():
        start_url = request.form.get('start_url', '').strip()
        if not start_url:
            return jsonify({'error': 'Start URL required'}), 400
        if not is_valid_url(start_url):
            return jsonify({'error': 'Invalid URL'}), 400

        max_pages = int(request.form.get('max_pages', 0))
        max_pages = 999999 if max_pages == 0 else max(1, min(max_pages, 5000))
        max_depth = max(1, min(int(request.form.get('max_depth', 3)), 10))

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
            'start_url': start_url,
            'max_pages': max_pages,
            'max_depth': max_depth,
            'total_pages': 0,
            'total_urls': 0,
            'domains': [],
            'current_url': None,
            'output_file': None
        }
        save_task(task_id, task_data)

        threading.Thread(target=run_crawler, args=(task_id, start_url, max_pages, max_depth), daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/crawler/cancel/<task_id>', methods=['POST'])
    def crawler_cancel(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        task['cancelled'] = True
        save_task(task_id, task)
        return jsonify({'status': 'cancellation requested'})

    @app.route('/crawler/stream/<task_id>')
    def crawler_stream(task_id):
        q = get_queue(task_id)

        def event_generator():
            try:
                # Send the current state from task file immediately (catch‑up)
                task = load_task(task_id)
                if task:
                    domains = task.get('domains', [])
                    payload = {
                        'domains': domains[-MAX_BATCH_SIZE:],
                        'path': urlparse(task.get('current_url', '')).path or '/',
                        'progress': task.get('progress', 0),
                        'pages': task.get('total_pages', 0),
                        'urls_count': task.get('total_urls', 0)
                    }
                    yield f"event: update\ndata: {json.dumps(payload)}\n\n"

                # Then listen for real‑time pushes
                while True:
                    try:
                        data = q.get(timeout=10)  # timeout to allow detecting task completion
                        yield f"event: update\ndata: {json.dumps(data)}\n\n"
                    except queue.Empty:
                        # Check if task is done
                        task = load_task(task_id)
                        if not task or task.get('status') in ('done', 'cancelled', 'error'):
                            # Send final status and break
                            yield f"event: {task.get('status', 'done')}\ndata: \n\n"
                            break
                        # Still crawling, but no new data – keep connection alive with a ping
                        yield ": keepalive\n\n"
            except GeneratorExit:
                # Client disconnected – remove queue if task is done, else keep it for reconnects
                task = load_task(task_id)
                if task and task.get('status') in ('done', 'cancelled', 'error'):
                    remove_queue(task_id)
                pass

        return Response(stream_with_context(event_generator()), mimetype="text/event-stream")

    @app.route('/crawler/download_domains/<task_id>')
    def crawler_download_domains(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        output_file = task.get('output_file')
        if not output_file:
            return jsonify({'error': 'No output file generated'}), 404
        filepath = os.path.join(UPLOAD_FOLDER, output_file)
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        return send_file(filepath, as_attachment=True, download_name=output_file)

    @app.route('/crawler/download_domains_by_filename')
    def crawler_download_by_filename():
        filename = request.args.get('filename')
        if not filename or not filename.endswith('_domains.txt'):
            abort(400)
        if '..' in filename:
            abort(403)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            abort(404)
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/list_files')
    def crawler_list_files():
        files = []
        for f in os.listdir(UPLOAD_FOLDER):
            if f.endswith('_domains.txt'):
                filepath = os.path.join(UPLOAD_FOLDER, f)
                stat = os.stat(filepath)
                files.append({
                    'name': f,
                    'path': f,
                    'size': stat.st_size,
                    'size_str': get_file_size_str(stat.st_size),
                    'mtime': stat.st_mtime
                })
        return jsonify(files)

    @app.route('/crawler/view/<filename>')
    def crawler_view_file(filename):
        if '..' in filename or not filename.endswith('_domains.txt'):
            abort(400)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            abort(404)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            return Response(content, mimetype='text/plain')
        except Exception as e:
            return jsonify({'error': str(e)}), 500

def get_file_size_str(size):
    if size < 1024:
        return f"{size} B"
    elif size < 1024*1024:
        return f"{size/1024:.1f} KB"
    elif size < 1024*1024*1024:
        return f"{size/(1024*1024):.1f} MB"
    else:
        return f"{size/(1024*1024*1024):.2f} GB"
