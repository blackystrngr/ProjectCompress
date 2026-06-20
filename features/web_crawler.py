import os
import re
import uuid
import threading
import time
import logging
import requests
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import request, jsonify, send_file
from bs4 import BeautifulSoup
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# Regex for URLs
URL_RE = re.compile(r'(https?://[^\s<>"\']+)', re.IGNORECASE)
# IPv4 regex
IPV4_RE = re.compile(r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b')
# IPv6 regex (simplified)
IPV6_RE = re.compile(r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b')

def is_valid_url(url):
    if not url or not isinstance(url, str):
        return False
    url = url.rstrip('.,;:!?)]}')
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        if not parsed.netloc:
            return False
        return True
    except:
        return False

def normalize_url(url, base):
    try:
        if url.startswith('//'):
            url = 'https:' + url
        url = url.rstrip('.,;:!?)]}')
        return urljoin(base, url)
    except:
        return None

def extract_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        if ':' in domain:
            domain = domain.split(':')[0]
        if not domain or domain in ('http', 'https'):
            return None
        return domain
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
    return domain == target_domain or domain.endswith('.' + target_domain)

def extract_urls_from_html(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for tag in soup.find_all(['a', 'link', 'script', 'img', 'iframe', 'source']):
        src = tag.get('href') or tag.get('src')
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
    return urls

def extract_urls_from_css(css_text, base_url):
    urls = set()
    css_url_re = re.compile(r'url\([\'"]?([^\'"\)]+)[\'"]?\)', re.IGNORECASE)
    for match in css_url_re.finditer(css_text):
        url = match.group(1).strip()
        if url and not url.startswith('data:') and not url.startswith('#'):
            abs_url = normalize_url(url, base_url)
            if abs_url and is_valid_url(abs_url):
                urls.add(abs_url)
    import_re = re.compile(r'@import\s+[\'"]([^\'"]+)[\'"]', re.IGNORECASE)
    for match in import_re.finditer(css_text):
        url = match.group(1).strip()
        abs_url = normalize_url(url, base_url)
        if abs_url and is_valid_url(abs_url):
            urls.add(abs_url)
    return urls

def extract_urls_from_js(js_text, base_url):
    urls = set()
    js_url_re = re.compile(r'[\'"](https?://[^\s<>"\']+)[\'"]', re.IGNORECASE)
    for match in js_url_re.finditer(js_text):
        url = match.group(1).strip()
        abs_url = normalize_url(url, base_url)
        if abs_url and is_valid_url(abs_url):
            urls.add(abs_url)
    return urls

def extract_ips_from_text(text):
    """Extract IPv4 and IPv6 addresses from text."""
    ips = set()
    ips.update(IPV4_RE.findall(text))
    ips.update(IPV6_RE.findall(text))
    return ips

def run_crawler(task_id, start_url, max_pages, max_depth, threads=20):
    logger.info(f"Crawler task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = extract_domain(start_url)
    if not target_domain:
        logger.error(f"Could not extract domain from {start_url}")
        return

    visited = set()
    processed = set()
    all_urls = set()
    domains = set()
    ip_addresses = set()
    pages_visited = 0
    current_url = start_url

    task['status'] = 'running'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['discovered_urls'] = list(all_urls)
    task['domains'] = list(domains)
    task['current_url'] = current_url
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    def fetch_url(url, depth):
        """Fetch a URL and extract URLs and IPs."""
        nonlocal pages_visited
        if url in processed:
            return None, None, depth
        processed.add(url)
        try:
            resp = session.get(url, allow_redirects=True, timeout=15)
            if resp.status_code != 200:
                return None, None, depth
            content_type = resp.headers.get('content-type', '').lower()
            text = resp.text
            # Extract IPs from text
            ips = extract_ips_from_text(text)
            # Extract URLs based on content type
            extracted_urls = set()
            if 'text/html' in content_type:
                extracted_urls = extract_urls_from_html(text, url)
            elif 'text/css' in content_type:
                extracted_urls = extract_urls_from_css(text, url)
            elif 'application/javascript' in content_type or 'text/javascript' in content_type:
                extracted_urls = extract_urls_from_js(text, url)
            else:
                return None, None, depth
            # Add IPs from the URL itself
            ips.update(extract_ips_from_text(url))
            return extracted_urls, ips, depth
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            return None, None, depth

    try:
        from collections import deque
        queue = deque([(start_url, 0)])
        visited.add(start_url)
        all_urls.add(start_url)
        dom = extract_domain(start_url)
        if dom:
            domains.add(dom)

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()

            def submit_task(url, depth):
                if url in visited:
                    return
                visited.add(url)
                task = load_task(task_id)
                if task:
                    task['current_url'] = url
                    save_task(task_id, task)
                future = executor.submit(fetch_url, url, depth)
                futures.add(future)

            # Start with the initial URL
            submit_task(start_url, 0)

            # Main loop: process completed futures and submit new tasks from queue
            while queue or futures:
                # Check for completed futures
                to_remove = set()
                for future in list(futures):
                    if future.done():
                        result = future.result()
                        futures.remove(future)
                        if result is not None:
                            extracted_urls, ips, depth = result
                            if ips:
                                ip_addresses.update(ips)
                            if extracted_urls:
                                for extracted_url in extracted_urls:
                                    if extracted_url not in visited:
                                        visited.add(extracted_url)
                                        all_urls.add(extracted_url)
                                        dom = extract_domain(extracted_url)
                                        if dom:
                                            domains.add(dom)
                                        # Add to queue if same domain and within depth limit
                                        if is_same_domain(extracted_url, target_domain):
                                            if depth + 1 <= max_depth:
                                                queue.append((extracted_url, depth + 1))
                                pages_visited += 1

                # Submit new tasks from queue (up to max_workers)
                while queue and len(futures) < threads:
                    url, depth = queue.popleft()
                    if url in visited:
                        continue
                    submit_task(url, depth)

                # Update task progress periodically
                if len(visited) % 10 == 0:
                    task = load_task(task_id)
                    if task:
                        task['total_pages'] = len(visited)
                        task['progress'] = int(100 * len(visited) / max_pages) if max_pages > 0 else 0
                        task['discovered_urls'] = list(all_urls)
                        task['domains'] = list(domains | ip_addresses)
                        task['current_url'] = current_url
                        save_task(task_id, task)

                # Small sleep to avoid busy loop
                time.sleep(0.1)

            # Done
            task = load_task(task_id)
            if task:
                task['status'] = 'done'
                task['progress'] = 100
                task['total_pages'] = len(visited)
                task['discovered_urls'] = list(all_urls)
                task['domains'] = list(domains | ip_addresses)
                task['current_url'] = None
                save_task(task_id, task)

            website_name = get_website_name_from_url(start_url)
            domain_filename = f"{website_name}_domains.txt"
            domain_filepath = os.path.join(UPLOAD_FOLDER, domain_filename)
            with open(domain_filepath, 'w') as f:
                f.write('\n'.join(sorted(domains | ip_addresses)))
            logger.info(f"Domains/IPs saved to {domain_filename}")

            urls_filename = f"crawled_urls_{task_id[:8]}.txt"
            urls_filepath = os.path.join(UPLOAD_FOLDER, urls_filename)
            with open(urls_filepath, 'w') as f:
                f.write('\n'.join(sorted(all_urls)))

            logger.info(f"Crawler {task_id} finished: {len(all_urls)} URLs, {len(domains | ip_addresses)} domains/IPs")

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
        if not is_valid_url(start_url):
            return jsonify({'error': 'Invalid URL (must start with http:// or https://)'}), 400

        max_pages = int(request.form.get('max_pages', 0))
        if max_pages == 0:
            max_pages = 999999
        else:
            max_pages = max(1, min(max_pages, 5000))

        max_depth = int(request.form.get('max_depth', 3))
        max_depth = max(1, min(max_depth, 10))

        threads = int(request.form.get('threads', 20))
        threads = max(1, min(threads, 50))

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
            'threads': threads,
            'total_pages': 0,
            'discovered_urls': [],
            'domains': [],
            'current_url': None,
            'error_msg': None,
        }
        save_task(task_id, task_data)

        threading.Thread(target=run_crawler, args=(task_id, start_url, max_pages, max_depth, threads), daemon=True).start()
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
            'discovered_urls': task.get('discovered_urls', []),
            'domains': task.get('domains', []),
            'current_url': task.get('current_url'),
            'error_msg': task.get('error_msg'),
        })

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
