import os
import re
import uuid
import threading
import time
import logging
import requests
from urllib.parse import urljoin, urlparse, urlunparse
from flask import request, jsonify, send_file
from bs4 import BeautifulSoup
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

def get_domain_from_url(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
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
    return domain == target_domain

def normalize_url(url, base):
    try:
        return urljoin(base, url)
    except:
        return None

def extract_urls_from_text(text, base_url):
    """Extract URLs from text using regex (for CSS/JS)."""
    # Pattern for URLs in CSS: url("...") or url('...') or url(...)
    # Also @import "..." or @import url(...)
    patterns = [
        r'url\s*\(\s*["\']?(.*?)["\']?\s*\)',
        r'@import\s+["\'](.*?)["\']',
        r'src\s*=\s*["\'](.*?)["\']',
        r'href\s*=\s*["\'](.*?)["\']',
        r'["\'](https?://[^"\']+)["\']',
    ]
    urls = set()
    for pat in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            url = match.group(1)
            if url:
                abs_url = normalize_url(url, base_url)
                if abs_url and abs_url.startswith(('http://', 'https://')):
                    urls.add(abs_url)
    return urls

def extract_internal_links(html, base_url, target_domain):
    """Extract all links from HTML that belong to the target domain."""
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for tag in soup.find_all(['a', 'link']):
        href = tag.get('href')
        if href:
            abs_url = normalize_url(href, base_url)
            if abs_url and is_same_domain(abs_url, target_domain):
                links.add(abs_url)
    return links

def extract_external_domains_from_resources(html, base_url, target_domain):
    """Extract all domains (including external) from HTML, CSS, and JS resources."""
    domains = set()
    # HTML: extract all links (including external)
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.find_all(['a', 'link', 'script', 'img', 'source']):
        src = tag.get('src') or tag.get('href')
        if src:
            abs_url = normalize_url(src, base_url)
            if abs_url:
                domain = get_domain_from_url(abs_url)
                if domain:
                    domains.add(domain)
    # Also parse inline CSS and JS for URLs
    for script in soup.find_all('script'):
        if script.string:
            urls = extract_urls_from_text(script.string, base_url)
            for u in urls:
                domain = get_domain_from_url(u)
                if domain:
                    domains.add(domain)
    for style in soup.find_all('style'):
        if style.string:
            urls = extract_urls_from_text(style.string, base_url)
            for u in urls:
                domain = get_domain_from_url(u)
                if domain:
                    domains.add(domain)
    return domains

def run_crawler(task_id, start_url, max_pages, max_depth):
    logger.info(f"Crawler task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = get_domain_from_url(start_url)
    visited = set()
    internal_urls = set()
    all_domains = set()
    queue = [(start_url, 0)]
    visited.add(start_url)
    internal_urls.add(start_url)
    all_domains.add(target_domain)
    pages_visited = 0
    current_url = start_url

    task['status'] = 'running'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['internal_urls'] = list(internal_urls)
    task['domains'] = list(all_domains)
    task['current_url'] = current_url
    task['target_domain'] = target_domain
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    try:
        while queue and pages_visited < max_pages:
            url, depth = queue.pop(0)
            current_url = url
            if depth > max_depth:
                continue

            task = load_task(task_id)
            if task:
                task['current_url'] = current_url
                save_task(task_id, task)

            try:
                resp = session.get(url, allow_redirects=True, timeout=15)
                if resp.status_code != 200:
                    continue
                content_type = resp.headers.get('content-type', '')
                if 'text/html' not in content_type:
                    continue
                html = resp.text
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                continue

            # Extract internal links
            links = extract_internal_links(html, url, target_domain)
            pages_visited += 1

            # Add internal links to queue
            for link in links:
                if link not in visited:
                    visited.add(link)
                    internal_urls.add(link)
                    if depth + 1 <= max_depth:
                        queue.append((link, depth + 1))

            # Extract domains from all resources (CSS, JS, etc.)
            new_domains = extract_external_domains_from_resources(html, url, target_domain)
            all_domains.update(new_domains)

            # Update task every few pages
            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if not task:
                    break
                task['progress'] = int(100 * pages_visited / max_pages)
                task['total_pages'] = pages_visited
                task['internal_urls'] = list(internal_urls)
                task['domains'] = list(all_domains)
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
            task['domains'] = list(all_domains)
            task['current_url'] = None
            save_task(task_id, task)

            # Save URLs file
            urls_filename = f"{target_domain}_urls.txt"
            urls_filepath = os.path.join(UPLOAD_FOLDER, urls_filename)
            with open(urls_filepath, 'w') as f:
                f.write('\n'.join(sorted(internal_urls)))

            # Save domains file
            domains_filename = f"{target_domain}_domains.txt"
            domains_filepath = os.path.join(UPLOAD_FOLDER, domains_filename)
            with open(domains_filepath, 'w') as f:
                f.write('\n'.join(sorted(all_domains)))

            logger.info(f"Crawler {task_id} finished: {len(internal_urls)} URLs, {len(all_domains)} domains")

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

        max_pages = int(request.form.get('max_pages', 200))
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
