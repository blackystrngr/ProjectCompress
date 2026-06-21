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

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
URL_RE = re.compile(r'(https?://[^\s<>"\']+)', re.IGNORECASE)

# IP regex (strict – no leading zeros)
IPV4_RE = re.compile(r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b')
IPV6_RE = re.compile(r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b')

# All non‑text file extensions – we never treat these as domains
SKIP_EXTENSIONS = {
    # images
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico', '.webp', '.bmp', '.tiff',
    # videos
    '.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.wmv',
    # audio
    '.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma',
    # documents (binary)
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt',
    # archives
    '.zip', '.gz', '.rar', '.7z', '.tar', '.bz2',
    # fonts
    '.ttf', '.woff', '.woff2', '.eot', '.otf',
    # scripts & code – we don't want these as domains
    '.js', '.css', '.xml', '.json', '.php', '.asp', '.aspx', '.jsp',
    '.do', '.action', '.cgi', '.pl', '.py', '.rb', '.go', '.java',
    '.class', '.jar', '.war', '.ear',
    '.rss', '.atom', '.feed',
    # config files
    '.conf', '.cfg', '.ini', '.yaml', '.yml', '.toml',
    '.htaccess', '.htpasswd',
    '.sql', '.db', '.sqlite',
    '.log', '.tmp', '.bak',
    # others
    '.exe', '.dll', '.so', '.dylib',
    '.pem', '.key', '.crt', '.csr'
}

# ---------------------------------------------------------------------
# DOMAIN VALIDATION – extremely strict
# ---------------------------------------------------------------------

def is_valid_domain(domain):
    """
    Return True only if the string looks like a real domain name.
    Rejects:
      - IPs (handled separately)
      - file names / paths
      - strings with only 1 char before the TLD (e.g., 'e.foreach')
      - anything containing / ? = & % # ; : @
      - any extension in SKIP_EXTENSIONS
      - localhost / internal names
    """
    if not domain or not isinstance(domain, str):
        return False
    domain = domain.lower().strip()

    # Must contain a dot and not be an IP
    if '.' not in domain:
        return False
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', domain):
        return False

    # Must not contain path/query characters
    if any(c in domain for c in ['/', '?', '=', '&', '%', '#', ';', ':', '@']):
        return False

    # Must not end with a skipped extension
    for ext in SKIP_EXTENSIONS:
        if domain.endswith(ext):
            return False

    # Must match the basic domain pattern: at least two parts, TLD 2+ letters
    # and the part before the TLD must be at least 2 characters
    pattern = re.compile(r'^([a-z0-9]([a-z0-9-]*[a-z0-9])?)\.([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z]{2,}$')
    if not pattern.match(domain):
        return False

    # Ensure the first part (before the first dot) has at least 2 chars
    parts = domain.split('.')
    if len(parts) < 2:
        return False
    if len(parts[0]) < 2:
        return False

    # Ignore common internal/local names
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

# ---------------------------------------------------------------------
# Helpers (unchanged)
# ---------------------------------------------------------------------

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
    # regex fallback (only catches full http(s) URLs)
    for match in URL_RE.finditer(html):
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

def extract_domains_from_text(text, base_domain=None):
    domains = set()
    # 1) From full URLs – these are reliable
    for match in URL_RE.finditer(text):
        url = match.group(1)
        dom = extract_domain(url)
        if dom and dom != base_domain:
            domains.add(dom)

    # 2) From plain domain names – use a strict pattern and additional checks
    plain_re = re.compile(r'\b((?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,})\b')
    for match in plain_re.finditer(text):
        domain = match.group(1)
        # Quick pre‑filter: must not contain any chars that indicate code
        if any(c in domain for c in ['_', '$', '`', '~', '!', '@']):
            continue
        cleaned = clean_domain(domain)
        if cleaned and cleaned != base_domain:
            domains.add(cleaned)
    return domains

# ---------------------------------------------------------------------
# Main crawler – checks cancellation after every page
# ---------------------------------------------------------------------

def run_crawler(task_id, start_url, max_pages, max_depth, threads=None):
    logger.info(f"Crawler task {task_id} started: {start_url}")
    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return
    target_domain = extract_domain(start_url)
    if not target_domain:
        logger.error(f"Could not extract domain from {start_url}")
        return

    all_urls = set()
    domains = set()
    ip_addresses = set()
    processed = set()
    queue = [(start_url, 0)]
    all_urls.add(start_url)
    dom = extract_domain(start_url)
    if dom:
        domains.add(dom)
    pages_visited = 0
    current_url = start_url

    task['status'] = 'crawling'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['discovered_urls'] = list(all_urls)
    task['domains'] = list(domains)
    task['current_url'] = current_url
    task['last_sent_count'] = 0
    task['last_sent_domains_count'] = 0
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    while queue and (pages_visited < max_pages or max_pages == 0):
        # ---- CHECK CANCELLATION ----
        task = load_task(task_id)
        if task and task.get('cancelled', False):
            logger.info(f"Task {task_id} cancelled by user")
            task['status'] = 'cancelled'
            save_task(task_id, task)
            return

        url, depth = queue.pop(0)
        if url in processed:
            continue
        processed.add(url)
        current_url = url

        # Update current URL in task
        task = load_task(task_id)
        if task:
            task['current_url'] = current_url
            save_task(task_id, task)

        try:
            resp = session.get(url, allow_redirects=True, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch {url}: status {resp.status_code}")
                continue
            content_type = resp.headers.get('content-type', '').lower()
            text = resp.text
            logger.info(f"Fetched {url} (status {resp.status_code}, type {content_type}, length {len(text)})")

            ips = extract_ips_from_text(text)
            ip_addresses.update(ips)

            extracted_urls = set()
            if 'text/html' in content_type:
                extracted_urls = extract_urls_from_html(text, url)
                domains.update(extract_domains_from_text(text, target_domain))
            elif 'text/css' in content_type:
                for match in re.finditer(r'url\([\'"]?([^\'"\)]+)[\'"]?\)', text):
                    u = match.group(1).strip()
                    if u and not u.startswith('data:') and not u.startswith('#'):
                        abs_u = normalize_url(u, url)
                        if abs_u and is_valid_url(abs_u):
                            extracted_urls.add(abs_u)
                domains.update(extract_domains_from_text(text, target_domain))
            elif 'application/javascript' in content_type or 'text/javascript' in content_type:
                for match in re.finditer(r'[\'"](https?://[^\'"]+)[\'"]', text):
                    u = match.group(1).strip()
                    abs_u = normalize_url(u, url)
                    if abs_u and is_valid_url(abs_u):
                        extracted_urls.add(abs_u)
                domains.update(extract_domains_from_text(text, target_domain))
            elif 'application/json' in content_type or 'text/xml' in content_type or 'application/xml' in content_type:
                # These are text‑based but may contain URLs – extract domains
                domains.update(extract_domains_from_text(text, target_domain))
                extracted_urls = set()
            elif 'text/plain' in content_type:
                domains.update(extract_domains_from_text(text, target_domain))
                extracted_urls = set()
            else:
                # For other types, just extract domain from the URL itself
                dom = extract_domain(url)
                if dom and dom != target_domain:
                    domains.add(dom)
                continue

            # Also extract domain from the current URL
            dom = extract_domain(url)
            if dom and dom != target_domain:
                domains.add(dom)

            for extracted_url in extracted_urls:
                if extracted_url not in processed:
                    all_urls.add(extracted_url)
                    dom = extract_domain(extracted_url)
                    if dom and dom != target_domain:
                        domains.add(dom)
                    if is_same_domain(extracted_url, target_domain) and depth + 1 <= max_depth:
                        queue.append((extracted_url, depth + 1))

            pages_visited += 1

            # Update task progress
            task = load_task(task_id)
            if task:
                task['progress'] = int(100 * pages_visited / max_pages) if max_pages > 0 else 0
                task['total_pages'] = pages_visited
                task['discovered_urls'] = list(all_urls)
                task['domains'] = list(domains | ip_addresses)
                task['current_url'] = current_url
                save_task(task_id, task)

        except Exception as e:
            logger.warning(f"Exception fetching {url}: {e}")
            continue

    # Done
    combined = set(domains) | set(ip_addresses)
    task = load_task(task_id)
    if task:
        task['status'] = 'done'
        task['progress'] = 100
        task['total_pages'] = pages_visited
        task['discovered_urls'] = list(all_urls)
        task['domains'] = list(combined)
        task['current_url'] = None
        task['last_sent_count'] = len(all_urls)
        task['last_sent_domains_count'] = len(combined)
        save_task(task_id, task)

    # Save to files
    website_name = get_website_name_from_url(start_url)
    domain_filename = f"{website_name}_domains.txt"
    domain_filepath = os.path.join(UPLOAD_FOLDER, domain_filename)
    with open(domain_filepath, 'w') as f:
        f.write('\n'.join(sorted(combined)))
    logger.info(f"Domains/IPs saved to {domain_filename}")

    urls_filename = f"crawled_urls_{task_id[:8]}.txt"
    urls_filepath = os.path.join(UPLOAD_FOLDER, urls_filename)
    with open(urls_filepath, 'w') as f:
        f.write('\n'.join(sorted(all_urls)))

    logger.info(f"Crawler {task_id} finished: {len(all_urls)} URLs, {len(combined)} domains/IPs")

# ---------------------------------------------------------------------
# Flask routes – including cancellation
# ---------------------------------------------------------------------

def register_routes(app):
    @app.route('/crawler/start', methods=['POST'])
    def crawler_start():
        start_url = request.form.get('start_url', '').strip()
        if not start_url:
            return jsonify({'error': 'Start URL required'}), 400
        if not is_valid_url(start_url):
            return jsonify({'error': 'Invalid URL (must start with http:// or https://)'}), 400

        max_pages = int(request.form.get('max_pages', 0))
        if max_pages == 0:
            max_pages = 999999
        else:
            max_pages = max(1, min(max_pages, 5000))

        max_depth = int(request.form.get('max_depth', 3))
        max_depth = max(1, min(max_depth, 10))

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
            'discovered_urls': [],
            'domains': [],
            'current_url': None,
            'error_msg': None,
            'last_sent_count': 0,
            'last_sent_domains_count': 0,
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
            task = load_task(task_id)
            if not task:
                yield "event: error\ndata: Task not found\n\n"
                return

            last_url_count = 0
            last_domain_count = 0

            while True:
                task = load_task(task_id)
                if not task:
                    yield "event: error\ndata: Task disappeared\n\n"
                    break

                status = task.get('status')
                urls = task.get('discovered_urls', [])
                domains = task.get('domains', [])
                new_urls = urls[last_url_count:]
                new_domains = domains[last_domain_count:]

                if new_urls or new_domains:
                    payload = {
                        'new_urls': new_urls,
                        'new_domains': new_domains,
                        'total_urls': len(urls),
                        'total_domains': len(domains),
                        'progress': task.get('progress', 0),
                        'pages': task.get('total_pages', 0),
                        'current_url': task.get('current_url')
                    }
                    yield f"event: update\ndata: {json.dumps(payload)}\n\n"
                    last_url_count = len(urls)
                    last_domain_count = len(domains)

                if status in ('done', 'cancelled', 'error'):
                    yield f"event: {status}\ndata: \n\n"
                    break

                # Check every 2 seconds – low traffic
                time.sleep(2)

        return Response(stream_with_context(event_generator()), mimetype="text/event-stream")

    # Download endpoints (unchanged)
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
            if f.endswith('_domains.txt') or f.endswith('_urls.txt'):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
