import os
import re
import uuid
import threading
import time
import logging
import requests
from urllib.parse import urljoin, urlparse
from flask import request, jsonify, send_file
from bs4 import BeautifulSoup
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# Regex to extract URLs from CSS url(...)
CSS_URL_RE = re.compile(r'url\([\'"]?([^\'"\)]+)[\'"]?\)', re.IGNORECASE)
# Regex to extract URLs from JS strings (simple)
JS_URL_RE = re.compile(r'[\'"](https?://[^\'"]+)[\'"]', re.IGNORECASE)

def get_domain_from_url(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except:
        return None

def is_same_domain(url, target_domain):
    parsed = urlparse(url)
    if not parsed.netloc:
        return False
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain == target_domain

def normalize_url(url, base):
    try:
        return urljoin(base, url)
    except:
        return None

def extract_urls_from_html(html, base_url):
    """Extract all URLs from HTML: href, src, srcset, data-* attributes, etc."""
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    # Tags with href
    for tag in soup.find_all(['a', 'link']):
        href = tag.get('href')
        if href:
            abs_url = normalize_url(href, base_url)
            if abs_url:
                urls.add(abs_url)
    # Tags with src
    for tag in soup.find_all(['script', 'img', 'iframe', 'source', 'audio', 'video', 'track']):
        src = tag.get('src')
        if src:
            abs_url = normalize_url(src, base_url)
            if abs_url:
                urls.add(abs_url)
    # srcset (images)
    for tag in soup.find_all(['img', 'source']):
        srcset = tag.get('srcset')
        if srcset:
            for part in srcset.split(','):
                part = part.strip().split(' ')[0]
                if part:
                    abs_url = normalize_url(part, base_url)
                    if abs_url:
                        urls.add(abs_url)
    # data-* attributes that might contain URLs (common in lazy loading)
    for tag in soup.find_all():
        for attr in tag.attrs:
            if attr.startswith('data-') and 'src' in attr:
                val = tag.get(attr)
                if val and (val.startswith('http') or val.startswith('/')):
                    abs_url = normalize_url(val, base_url)
                    if abs_url:
                        urls.add(abs_url)
    return urls

def extract_urls_from_css(css_text, base_url):
    """Extract URLs from CSS url(...) and also from @import."""
    urls = set()
    # url(...)
    for match in CSS_URL_RE.finditer(css_text):
        url = match.group(1).strip()
        if url and not url.startswith('data:') and not url.startswith('#'):
            abs_url = normalize_url(url, base_url)
            if abs_url:
                urls.add(abs_url)
    # @import url(...)
    import_re = re.compile(r'@import\s+[\'"]([^\'"]+)[\'"]', re.IGNORECASE)
    for match in import_re.finditer(css_text):
        url = match.group(1).strip()
        abs_url = normalize_url(url, base_url)
        if abs_url:
            urls.add(abs_url)
    return urls

def extract_urls_from_js(js_text, base_url):
    """Extract URLs from JS strings that look like URLs."""
    urls = set()
    for match in JS_URL_RE.finditer(js_text):
        url = match.group(1).strip()
        if url:
            abs_url = normalize_url(url, base_url)
            if abs_url:
                urls.add(abs_url)
    return urls

def get_content_type(resp):
    """Extract content type from headers."""
    ct = resp.headers.get('content-type', '')
    return ct.split(';')[0].strip().lower()

def run_crawler(task_id, start_url, max_pages, max_depth, follow_all=False):
    """Crawler that extracts URLs from HTML, CSS, JS."""
    logger.info(f"Crawler {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = get_domain_from_url(start_url)
    visited = set()
    internal_urls = set()
    domains = set()
    queue = [(start_url, 0)]
    visited.add(start_url)
    internal_urls.add(start_url)
    pages_visited = 0
    current_url = start_url

    task['status'] = 'running'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['internal_urls'] = list(internal_urls)
    task['domains'] = list(domains)
    task['current_url'] = current_url
    task['target_domain'] = target_domain
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 30)

    try:
        while queue and pages_visited < max_pages:
            url, depth = queue.pop(0)
            current_url = url
            if depth > max_depth:
                continue

            # Update progress
            task = load_task(task_id)
            if task:
                task['current_url'] = current_url
                save_task(task_id, task)

            try:
                resp = session.get(url, allow_redirects=True, timeout=20)
                if resp.status_code != 200:
                    continue
                content_type = get_content_type(resp)
                if 'text/html' in content_type:
                    # Extract URLs from HTML
                    urls = extract_urls_from_html(resp.text, url)
                    # Also parse inline CSS? Not needed; we'll fetch CSS separately.
                elif 'text/css' in content_type:
                    urls = extract_urls_from_css(resp.text, url)
                elif 'application/javascript' in content_type or 'text/javascript' in content_type:
                    urls = extract_urls_from_js(resp.text, url)
                else:
                    # Skip other types (images, fonts, etc.) – we don't need to parse them
                    continue
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                continue

            pages_visited += 1

            # Process extracted URLs
            for extracted_url in urls:
                if extracted_url not in visited:
                    visited.add(extracted_url)
                    # Always add to internal_urls if same domain, even if it's an asset
                    if is_same_domain(extracted_url, target_domain):
                        internal_urls.add(extracted_url)
                        # Queue if it's HTML or CSS/JS (to extract more domains) and depth allows
                        if depth + 1 <= max_depth:
                            queue.append((extracted_url, depth + 1))
                    # Extract domain from all URLs (including external)
                    dom = get_domain_from_url(extracted_url)
                    if dom:
                        domains.add(dom)

            # Update every few pages
            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if not task:
                    break
                task['progress'] = int(100 * pages_visited / max_pages) if max_pages > 0 else 0
                task['total_pages'] = pages_visited
                task['internal_urls'] = list(internal_urls)
                task['domains'] = list(domains)
                task['current_url'] = current_url
                save_task(task_id, task)

            task = load_task(task_id)
            if task and task.get('cancelled', False):
                logger.info(f"Crawler {task_id} cancelled")
                break

        # Done
        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            task['total_pages'] = pages_visited
            task['internal_urls'] = list(internal_urls)
            task['domains'] = list(domains)
            task['current_url'] = None
            save_task(task_id, task)

            # Save domain file
            domain_filename = f"{target_domain}_domains.txt"
            domain_filepath = os.path.join(UPLOAD_FOLDER, domain_filename)
            with open(domain_filepath, 'w') as f:
                f.write('\n'.join(sorted(domains)))

            # Save URLs file
            urls_filename = f"{target_domain}_urls.txt"
            urls_filepath = os.path.join(UPLOAD_FOLDER, urls_filename)
            with open(urls_filepath, 'w') as f:
                f.write('\n'.join(sorted(internal_urls)))

            logger.info(f"Crawler {task_id} finished: {len(internal_urls)} URLs, {len(domains)} domains")

    except Exception as e:
        logger.exception(f"Crawler {task_id} failed")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            task['current_url'] = None
            save_task(task_id, task)

def register_routes(app):
    @app.route('/crawler/start', methods=['POST'])
    def crawler_start():
        start_url = request.form.get('start_url', '').strip()
        if not start_url:
            return jsonify({'error': 'Start URL required'}), 400
        if not start_url.startswith(('http://', 'https://')):
            start_url = 'https://' + start_url

        max_pages = int(request.form.get('max_pages', 0))
        if max_pages == 0:
            max_pages = 999999  # effectively unlimited
        else:
            max_pages = max(1, min(max_pages, 10000))

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
            'internal_urls': [],
            'domains': [],
            'current_url': None,
            'error_msg': None,
        }
        save_task(task_id, task_data)

        threading.Thread(target=run_crawler, args=(task_id, start_url, max_pages, max_depth), daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/crawler/status/<task_id>')
    def crawler_status(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        return jsonify({
            'status': task.get('status'),
            'progress': task.get('progress', 0),
            'total_pages': task.get('total_pages', 0),
            'max_pages': task.get('max_pages', 0),
            'max_depth': task.get('max_depth', 0),
            'internal_urls': task.get('internal_urls', []),
            'domains': task.get('domains', []),
            'current_url': task.get('current_url'),
            'target_domain': task.get('target_domain'),
            'error_msg': task.get('error_msg'),
        })

    @app.route('/crawler/download_urls/<task_id>')
    def crawler_download_urls(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        urls = task.get('internal_urls', [])
        if not urls:
            return jsonify({'error': 'No URLs discovered'}), 404
        domain = task.get('target_domain', 'unknown')
        filename = f"{domain}_urls.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                f.write('\n'.join(sorted(urls)))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/download_domains/<task_id>')
    def crawler_download_domains(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        domains = task.get('domains', [])
        if not domains:
            return jsonify({'error': 'No domains discovered'}), 404
        domain = task.get('target_domain', 'unknown')
        filename = f"{domain}_domains.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                f.write('\n'.join(sorted(domains)))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/list_files')
    def crawler_list_files():
        files = []
        for f in os.listdir(UPLOAD_FOLDER):
            if f.endswith('_urls.txt') or f.endswith('_domains.txt'):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
