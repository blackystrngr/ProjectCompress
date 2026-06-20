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

def is_valid_url(url):
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and parsed.netloc

def normalize_url(url, base):
    try:
        return urljoin(base, url)
    except:
        return None

def extract_links(html, base_url):
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

def get_website_name_from_url(url):
    parsed = urlparse(url)
    domain = parsed.netloc
    # Remove www.
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain

def run_crawler(task_id, start_url, max_pages, max_depth):
    logger.info(f"Crawler task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    visited = set()
    discovered_urls = set()
    domains = set()
    queue = [(start_url, 0)]
    visited.add(start_url)
    discovered_urls.add(start_url)
    domains.add(extract_domain(start_url))
    pages_visited = 0
    current_url = start_url

    task['status'] = 'running'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['discovered_urls'] = list(discovered_urls)
    task['domains'] = list(domains)
    task['current_url'] = current_url
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

            links = extract_links(html, url)
            pages_visited += 1

            for link in links:
                if link not in visited:
                    visited.add(link)
                    discovered_urls.add(link)
                    domains.add(extract_domain(link))
                    if depth + 1 <= max_depth:
                        queue.append((link, depth + 1))

            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if not task:
                    break
                task['progress'] = int(100 * pages_visited / max_pages)
                task['total_pages'] = pages_visited
                task['discovered_urls'] = list(discovered_urls)
                task['domains'] = list(domains)
                task['current_url'] = current_url
                save_task(task_id, task)

            task = load_task(task_id)
            if task and task.get('cancelled', False):
                logger.info(f"Crawler {task_id} cancelled")
                break

        # Done – save domain file
        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            task['total_pages'] = pages_visited
            task['discovered_urls'] = list(discovered_urls)
            task['domains'] = list(domains)
            task['current_url'] = None
            save_task(task_id, task)

            # Save domains file with website name
            website_name = get_website_name_from_url(start_url)
            domain_filename = f"{website_name}_domains.txt"
            domain_filepath = os.path.join(UPLOAD_FOLDER, domain_filename)
            with open(domain_filepath, 'w') as f:
                f.write('\n'.join(sorted(domains)))
            logger.info(f"Domains saved to {domain_filename}")

            # Also save full URLs file (already does via download endpoint)
            urls_filename = f"crawled_urls_{task_id[:8]}.txt"
            urls_filepath = os.path.join(UPLOAD_FOLDER, urls_filename)
            with open(urls_filepath, 'w') as f:
                f.write('\n'.join(sorted(discovered_urls)))

            logger.info(f"Crawler {task_id} finished: {len(discovered_urls)} URLs, {len(domains)} domains")

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

        max_pages = int(request.form.get('max_pages', 100))
        max_pages = max(1, min(max_pages, 5000))
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
        # The file is already saved in run_crawler, but we can also create on demand
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
        # Ensure the file exists (it should have been saved in run_crawler)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                f.write('\n'.join(sorted(domains)))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/list_domain_files')
    def crawler_list_domain_files():
        files = []
        for f in os.listdir(UPLOAD_FOLDER):
            if f.endswith('_domains.txt'):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
