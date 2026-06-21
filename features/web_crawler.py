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

# ====================== GROK-STYLE LOW BANDWIDTH OPTIMIZATIONS ======================
UPDATE_INTERVAL = 7                    # seconds
MIN_DOMAINS_PER_UPDATE = 5
MAX_BATCH_SIZE = 40
INACTIVITY_TIMEOUT = 30                # auto-cancel if no client

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

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
    # Keep the rest of your extraction logic (srcset, data-*, meta, scripts) as-is
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

def run_crawler(task_id, start_url, max_pages, max_depth, threads=None):
    logger.info(f"Crawler task {task_id} started: {start_url}")
    task = load_task(task_id)
    if not task:
        return

    target_domain = extract_domain(start_url)
    if not target_domain:
        logger.error(f"Could not extract domain from {start_url}")
        return

    all_urls = set([start_url])
    domains = set()
    ip_addresses = set()
    processed = set()
    queue = [(start_url, 0)]
    if dom := extract_domain(start_url):
        domains.add(dom)

    pages_visited = 0
    last_update = time.time()
    last_domain_count = 0

    task.update({
        'status': 'crawling',
        'progress': 0,
        'total_pages': 0,
        'max_pages': max_pages,
        'max_depth': max_depth,
        'domains': sorted(domains),
        'current_url': start_url,
        'last_client_seen': time.time()
    })
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    while queue and (pages_visited < max_pages or max_pages == 0):
        task = load_task(task_id)
        if not task or task.get('cancelled', False):
            return
        if time.time() - task.get('last_client_seen', 0) > INACTIVITY_TIMEOUT:
            logger.info(f"Task {task_id} auto-cancelled (client inactive)")
            task['status'] = 'cancelled'
            save_task(task_id, task)
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

                for extracted_url in extracted_urls:
                    all_urls.add(extracted_url)
                    dom = extract_domain(extracted_url)
                    if dom and dom != target_domain:
                        domains.add(dom)
                    if is_same_domain(extracted_url, target_domain) and depth + 1 <= max_depth:
                        queue.append((extracted_url, depth + 1))

            pages_visited += 1

            # === LOW BANDWIDTH UPDATE ===
            now = time.time()
            new_count = len(domains) - last_domain_count
            if (now - last_update > UPDATE_INTERVAL and new_count >= MIN_DOMAINS_PER_UPDATE) or pages_visited % 25 == 0:
                batch = sorted(list(domains))[-MAX_BATCH_SIZE:]
                task.update({
                    'progress': int(100 * pages_visited / max_pages) if max_pages > 0 else min(98, pages_visited),
                    'total_pages': pages_visited,
                    'domains': batch,
                    'current_url': url,
                    'last_client_seen': time.time()
                })
                save_task(task_id, task)
                last_update = now
                last_domain_count = len(domains)

        except Exception as e:
            logger.debug(f"Fetch error {url}: {e}")
            continue

    # Final save
    combined = sorted(set(domains) | set(ip_addresses))
    task.update({
        'status': 'done',
        'progress': 100,
        'total_pages': pages_visited,
        'domains': combined,
        'current_url': None
    })
    save_task(task_id, task)

    # Save files
    website_name = get_website_name_from_url(start_url)
    domain_filename = f"{website_name}_domains.txt"
    with open(os.path.join(UPLOAD_FOLDER, domain_filename), 'w', encoding='utf-8') as f:
        f.write('\n'.join(combined))

    logger.info(f"Crawler {task_id} finished: {len(all_urls)} URLs, {len(combined)} domains/IPs")

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
            'domains': [],
            'current_url': None,
            'last_client_seen': time.time()
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
        def event_generator():
            last_domain_count = 0
            while True:
                task = load_task(task_id)
                if not task:
                    yield "event: error\ndata: Task not found\n\n"
                    break

                task['last_client_seen'] = time.time()
                save_task(task_id, task)   # keep client alive

                status = task.get('status')
                domains = task.get('domains', [])
                current_url = task.get('current_url', '')
                current_path = urlparse(current_url).path or '/' if current_url else '/'

                new_domains = domains[last_domain_count:]

                if new_domains or status in ('done', 'cancelled'):
                    payload = {
                        'domains': new_domains[:MAX_BATCH_SIZE],
                        'path': current_path,
                        'progress': task.get('progress', 0),
                        'pages': task.get('total_pages', 0)
                    }
                    yield f"event: update\ndata: {json.dumps(payload)}\n\n"
                    if new_domains:
                        last_domain_count = len(domains)

                if status in ('done', 'cancelled', 'error'):
                    yield f"event: {status}\ndata: \n\n"
                    break

                time.sleep(6)   # relaxed interval

        return Response(stream_with_context(event_generator()), mimetype="text/event-stream")

    # Keep your download and list_files routes unchanged
    @app.route('/crawler/download_urls/<task_id>')
    def crawler_download_urls(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        urls = task.get('discovered_urls', [])
        if not urls:
            return jsonify({'error': 'No URLs discovered'}), 404
        filename = f"crawled_urls_{task_id[:8]}.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                f.write('\n'.join(urls))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/download_domains/<task_id>')
    def crawler_download_domains(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        domains = task.get('domains', [])
        if not domains:
            return jsonify({'error': 'No domains discovered'}), 404
        website_name = get_website_name_from_url(task.get('start_url'))
        filename = f"{website_name}_domains.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                f.write('\n'.join(sorted(domains)))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/list_files')
    def crawler_list_files():
        files = []
        for f in os.listdir(UPLOAD_FOLDER):
            if f.endswith(('_domains.txt', '_urls.txt')):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
