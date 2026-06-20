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

def get_domain_from_url(url):
    """Extract domain (e.g., sltmobitel.lk) from URL."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    # Remove www.
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain

def is_same_domain(url, target_domain):
    """Check if URL belongs to the target domain."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return False
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain == target_domain

def normalize_url(url, base):
    """Convert relative URL to absolute."""
    try:
        return urljoin(base, url)
    except:
        return None

def extract_internal_links(html, base_url, target_domain):
    """Extract all links that belong to the target domain."""
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for tag in soup.find_all(['a', 'link']):
        href = tag.get('href')
        if href:
            abs_url = normalize_url(href, base_url)
            if abs_url and is_same_domain(abs_url, target_domain):
                links.append(abs_url)
    return links

def get_path_from_url(url):
    """Extract the path part of a URL (for directory display)."""
    parsed = urlparse(url)
    path = parsed.path or '/'
    if not path.startswith('/'):
        path = '/' + path
    return path

def build_directory_tree(urls):
    """Build a nested directory structure from a list of URLs."""
    tree = {}
    for url in urls:
        parsed = urlparse(url)
        path = parsed.path or '/'
        if not path.startswith('/'):
            path = '/' + path
        parts = path.strip('/').split('/') if path != '/' else []
        current = tree
        for part in parts:
            if part not in current:
                current[part] = {}
            current = current[part]
    return tree

def render_tree(tree, indent=0):
    """Render directory tree as text (for display)."""
    lines = []
    for key, value in sorted(tree.items()):
        lines.append('  ' * indent + '📁 ' + key)
        if value:
            lines.extend(render_tree(value, indent + 1))
    return lines

def run_crawler(task_id, start_url, max_pages, max_depth):
    """Background crawler – only follows internal links."""
    logger.info(f"Crawler task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = get_domain_from_url(start_url)
    visited = set()
    internal_urls = set()
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

            # Update current_url
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

            links = extract_internal_links(html, url, target_domain)
            pages_visited += 1

            for link in links:
                if link not in visited:
                    visited.add(link)
                    internal_urls.add(link)
                    if depth + 1 <= max_depth:
                        queue.append((link, depth + 1))

            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if not task:
                    break
                task['progress'] = int(100 * pages_visited / max_pages)
                task['total_pages'] = pages_visited
                task['internal_urls'] = list(internal_urls)
                task['current_url'] = current_url
                save_task(task_id, task)

            task = load_task(task_id)
            if task and task.get('cancelled', False):
                logger.info(f"Crawler {task_id} cancelled")
                break

        # Done – build and save directory tree
        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            task['total_pages'] = pages_visited
            task['internal_urls'] = list(internal_urls)
            task['current_url'] = None
            save_task(task_id, task)

            # Build directory tree
            tree = build_directory_tree(internal_urls)
            tree_lines = render_tree(tree)

            # Save domain file
            domain_filename = f"{target_domain}_directories.txt"
            domain_filepath = os.path.join(UPLOAD_FOLDER, domain_filename)
            with open(domain_filepath, 'w') as f:
                f.write(f"Domain: {target_domain}\n")
                f.write(f"Total URLs: {len(internal_urls)}\n")
                f.write(f"Pages visited: {pages_visited}\n")
                f.write("\nDirectory Structure:\n")
                f.write('\n'.join(tree_lines))

            # Also save raw URLs
            urls_filename = f"{target_domain}_urls.txt"
            urls_filepath = os.path.join(UPLOAD_FOLDER, urls_filename)
            with open(urls_filepath, 'w') as f:
                f.write('\n'.join(sorted(internal_urls)))

            logger.info(f"Crawler {task_id} finished: {len(internal_urls)} URLs")

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

    @app.route('/crawler/download_directories/<task_id>')
    def crawler_download_directories(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        urls = task.get('internal_urls', [])
        if not urls:
            return jsonify({'error': 'No URLs discovered'}), 404
        domain = task.get('target_domain', 'unknown')
        filename = f"{domain}_directories.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            # Rebuild the directory tree
            tree = build_directory_tree(urls)
            tree_lines = render_tree(tree)
            with open(filepath, 'w') as f:
                f.write(f"Domain: {domain}\n")
                f.write(f"Total URLs: {len(urls)}\n")
                f.write("\nDirectory Structure:\n")
                f.write('\n'.join(tree_lines))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/list_files')
    def crawler_list_files():
        files = []
        for f in os.listdir(UPLOAD_FOLDER):
            if f.endswith('_urls.txt') or f.endswith('_directories.txt'):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
