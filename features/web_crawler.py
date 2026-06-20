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

# User‑Agent to avoid blocking
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

def is_valid_url(url):
    """Check if URL has HTTP/HTTPS scheme."""
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and parsed.netloc

def normalize_url(url, base):
    """Convert relative URL to absolute."""
    try:
        return urljoin(base, url)
    except:
        return None

def extract_links(html, base_url):
    """Extract all href links from HTML, return absolute URLs."""
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for tag in soup.find_all(['a', 'link']):
        href = tag.get('href')
        if href:
            abs_url = normalize_url(href, base_url)
            if abs_url and is_valid_url(abs_url):
                links.append(abs_url)
    return links

def extract_domain(url):
    parsed = urlparse(url)
    return parsed.netloc

def run_crawler(task_id, start_url, max_pages, max_depth):
    """Background crawler task."""
    logger.info(f"Crawler task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    # Initialize state
    visited = set()
    discovered_urls = set()   # all unique URLs found
    domains = set()
    queue = [(start_url, 0)]   # (url, depth)
    visited.add(start_url)
    discovered_urls.add(start_url)
    domains.add(extract_domain(start_url))
    pages_visited = 0

    # Update task with initial state
    task['status'] = 'running'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['discovered_urls'] = list(discovered_urls)
    task['domains'] = list(domains)
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    try:
        while queue and pages_visited < max_pages:
            url, depth = queue.pop(0)  # BFS
            if depth > max_depth:
                continue

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

            # Extract links
            links = extract_links(html, url)
            pages_visited += 1

            for link in links:
                if link not in visited:
                    visited.add(link)
                    discovered_urls.add(link)
                    domains.add(extract_domain(link))
                    if depth + 1 <= max_depth:
                        queue.append((link, depth + 1))

            # Update progress every few pages
            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if not task:
                    break
                task['progress'] = int(100 * pages_visited / max_pages)
                task['total_pages'] = pages_visited
                task['discovered_urls'] = list(discovered_urls)
                task['domains'] = list(domains)
                save_task(task_id, task)

            # Check cancellation
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
            task['discovered_urls'] = list(discovered_urls)
            task['domains'] = list(domains)
            save_task(task_id, task)
            logger.info(f"Crawler {task_id} finished: {len(discovered_urls)} URLs, {len(domains)} domains")

    except Exception as e:
        logger.exception(f"Crawler {task_id} failed")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)

def register_routes(app):
    @app.route('/crawler/start', methods=['POST'])
    def crawler_start():
        start_url = request.form.get('start_url', '').strip()
        if not start_url:
            return jsonify({'error': 'Start URL required'}), 400
        if not is_valid_url(start_url):
            return jsonify({'error': 'Invalid URL (must start with http:// or https://)'}), 400

        max_pages = int(request.form.get('max_pages', 100))
        max_pages = max(1, min(max_pages, 5000))  # limit 5000
        max_depth = int(request.form.get('max_depth', 2))
        max_depth = max(1, min(max_depth, 5))

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
        # Return only relevant fields
        return jsonify({
            'status': task.get('status'),
            'progress': task.get('progress', 0),
            'total_pages': task.get('total_pages', 0),
            'max_pages': task.get('max_pages', 0),
            'max_depth': task.get('max_depth', 0),
            'discovered_urls': task.get('discovered_urls', []),
            'domains': task.get('domains', []),
            'error_msg': task.get('error_msg'),
        })

    @app.route('/crawler/download/<task_id>')
    def crawler_download(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        urls = task.get('discovered_urls', [])
        if not urls:
            return jsonify({'error': 'No URLs discovered'}), 404
        # Create temporary file
        filename = f"crawled_urls_{task_id[:8]}.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, 'w') as f:
            f.write('\n'.join(urls))
        return send_file(filepath, as_attachment=True, download_name=filename)
