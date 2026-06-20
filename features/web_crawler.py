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

# Default wordlist for bruteforce
DEFAULT_WORDLIST = [
    'admin', 'backup', 'config', 'wp-admin', 'wp-content', 'wp-includes',
    'uploads', 'images', 'css', 'js', 'assets', 'static', 'media',
    'data', 'logs', 'tmp', 'temp', 'cache', 'sessions', 'cgi-bin',
    'includes', 'lib', 'modules', 'plugins', 'themes', 'vendor',
    'robots.txt', 'sitemap.xml', 'sitemap_index.xml', 'sitemap.xml.gz',
    'humans.txt', 'crossdomain.xml', 'phpinfo.php', 'phpmyadmin',
    'mysql', 'database', 'sql', 'dump', 'backup.sql', 'config.php',
    'config.inc.php', 'settings.php', 'wp-config.php', '.env',
    '.htaccess', '.htpasswd', 'server-status', 'server-info',
    'phpinfo', 'test', 'example', 'demo', 'sample',
    'index.php', 'index.html', 'default.php', 'default.html',
    'home', 'main', 'public', 'private', 'restricted', 'secure',
    'user', 'users', 'profile', 'account', 'login', 'register',
    'contact', 'about', 'help', 'faq', 'support', 'terms', 'privacy',
    'dashboard', 'control', 'panel', 'manage', 'adminer',
]

# Common extensions for bruteforce
DEFAULT_EXTENSIONS = ['.php', '.html', '.txt', '.log', '.bak', '.old', '.zip', '.tar.gz', '.sql', '.env', '.ini', '.xml', '.json', '.yml', '.yaml', '.conf', '.config', '.htaccess', '.htpasswd', '.inc', '.class', '.js', '.css', '.jpg', '.png', '.gif', '.ico', '.svg', '.webp', '.woff', '.woff2', '.ttf', '.eot']

# --------------------------------
# Crawling helpers
# --------------------------------
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
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for tag in soup.find_all(['a', 'link']):
        href = tag.get('href')
        if href:
            abs_url = normalize_url(href, base_url)
            if abs_url:
                urls.add(abs_url)
    for tag in soup.find_all(['script', 'img', 'iframe', 'source']):
        src = tag.get('src')
        if src:
            abs_url = normalize_url(src, base_url)
            if abs_url:
                urls.add(abs_url)
    for tag in soup.find_all(['img', 'source']):
        srcset = tag.get('srcset')
        if srcset:
            for part in srcset.split(','):
                part = part.strip().split(' ')[0]
                if part:
                    abs_url = normalize_url(part, base_url)
                    if abs_url:
                        urls.add(abs_url)
    for tag in soup.find_all():
        for attr in tag.attrs:
            if attr.startswith('data-') and 'src' in attr:
                val = tag.get(attr)
                if val and (val.startswith('http') or val.startswith('/')):
                    abs_url = normalize_url(val, base_url)
                    if abs_url:
                        urls.add(abs_url)
    return urls

# --------------------------------
# Bruteforce helpers
# --------------------------------
def normalize_base_url(base):
    if not base.endswith('/'):
        base += '/'
    return base

def get_status_code(url, session):
    try:
        resp = session.head(url, allow_redirects=True, timeout=5)
        return resp.status_code, url
    except:
        return None, url

# --------------------------------
# Main crawler + bruteforce task
# --------------------------------
def run_combined(task_id, start_url, max_pages, max_depth, enable_bruteforce, wordlist, extensions, threads):
    logger.info(f"Combined task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = get_domain_from_url(start_url)
    visited = set()
    internal_urls = set()      # all discovered internal URLs
    domains = set()
    queue = [(start_url, 0)]
    visited.add(start_url)
    internal_urls.add(start_url)
    pages_visited = 0
    current_url = start_url

    # Bruteforce state
    bf_discovered = []
    bf_checked = 0
    bf_total = 0
    bf_done = False

    task['status'] = 'crawling'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['internal_urls'] = list(internal_urls)
    task['domains'] = list(domains)
    task['current_url'] = current_url
    task['target_domain'] = target_domain
    task['enable_bruteforce'] = enable_bruteforce
    task['bf_discovered'] = []
    task['bf_checked'] = 0
    task['bf_total'] = 0
    task['bf_done'] = False
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 30)

    # ---------- PHASE 1: Crawl ----------
    try:
        while queue and (pages_visited < max_pages or max_pages == 0):
            url, depth = queue.pop(0)
            current_url = url
            if depth > max_depth:
                continue

            task = load_task(task_id)
            if task:
                task['current_url'] = current_url
                save_task(task_id, task)

            try:
                resp = session.get(url, allow_redirects=True, timeout=20)
                if resp.status_code != 200:
                    continue
                content_type = resp.headers.get('content-type', '')
                if 'text/html' not in content_type:
                    continue
                html = resp.text
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                continue

            urls = extract_urls_from_html(html, url)
            pages_visited += 1

            for extracted_url in urls:
                if extracted_url not in visited:
                    visited.add(extracted_url)
                    if is_same_domain(extracted_url, target_domain):
                        internal_urls.add(extracted_url)
                        # Queue for further crawling
                        if depth + 1 <= max_depth:
                            queue.append((extracted_url, depth + 1))
                    dom = get_domain_from_url(extracted_url)
                    if dom:
                        domains.add(dom)

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
                logger.info(f"Task {task_id} cancelled during crawl")
                break

        # Crawl done – save intermediate results
        task = load_task(task_id)
        if task:
            task['status'] = 'crawling_done'
            task['progress'] = 50
            task['internal_urls'] = list(internal_urls)
            task['domains'] = list(domains)
            save_task(task_id, task)

    except Exception as e:
        logger.exception(f"Crawl failed for {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = f"Crawl error: {str(e)}"
            save_task(task_id, task)
        return

    # ---------- PHASE 2: Bruteforce (if enabled) ----------
    if enable_bruteforce and not task.get('cancelled', False):
        logger.info(f"Starting bruteforce for {task_id} on {start_url}")
        task = load_task(task_id)
        if task:
            task['status'] = 'bruteforcing'
            task['bf_discovered'] = []
            task['bf_checked'] = 0
            task['bf_total'] = 0
            save_task(task_id, task)

        base = normalize_base_url(start_url)
        bf_session = requests.Session()
        bf_session.headers.update({'User-Agent': USER_AGENT})
        bf_session.timeout = (5, 10)

        # Build path list
        paths = []
        for word in wordlist:
            paths.append(word + '/')
            for ext in extensions:
                paths.append(word + ext)
        bf_total = len(paths)
        bf_checked = 0
        bf_discovered = []

        task = load_task(task_id)
        if task:
            task['bf_total'] = bf_total
            save_task(task_id, task)

        def process_path(path):
            url = normalize_base_url(base) + path
            try:
                resp = bf_session.head(url, allow_redirects=True, timeout=5)
                if resp.status_code in (200, 301, 302, 403, 401):
                    return url, resp.status_code
            except:
                pass
            return None, None

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {executor.submit(process_path, p): p for p in paths}
            for future in as_completed(futures):
                if load_task(task_id).get('cancelled', False):
                    executor.shutdown(wait=False)
                    break
                url, status = future.result()
                bf_checked += 1
                if url:
                    bf_discovered.append({'url': url, 'status': status})
                if bf_checked % 50 == 0:
                    task = load_task(task_id)
                    if task:
                        task['bf_discovered'] = bf_discovered
                        task['bf_checked'] = bf_checked
                        task['bf_total'] = bf_total
                        progress_bf = int(100 * bf_checked / bf_total) if bf_total > 0 else 0
                        task['progress'] = 50 + int(50 * progress_bf / 100)  # total progress 50-100%
                        save_task(task_id, task)

        # Bruteforce done
        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'done'
            task['progress'] = 100
            task['bf_discovered'] = bf_discovered
            task['bf_checked'] = bf_checked
            task['bf_total'] = bf_total
            task['bf_done'] = True
            save_task(task_id, task)

            # Save combined results to files
            # URLs (crawled + bruteforced)
            all_urls = list(internal_urls) + [item['url'] for item in bf_discovered]
            all_urls = sorted(set(all_urls))
            domain = target_domain
            urls_filename = f"{domain}_urls.txt"
            with open(os.path.join(UPLOAD_FOLDER, urls_filename), 'w') as f:
                f.write('\n'.join(all_urls))

            # Directories (from crawled URLs + bruteforced paths)
            dirs = set()
            for u in all_urls:
                path = urlparse(u).path
                if path:
                    dirs.add(path)
            dirs_filename = f"{domain}_directories.txt"
            with open(os.path.join(UPLOAD_FOLDER, dirs_filename), 'w') as f:
                f.write('\n'.join(sorted(dirs)))

            # Bruteforce results only
            if bf_discovered:
                bf_filename = f"{domain}_bruteforce.txt"
                with open(os.path.join(UPLOAD_FOLDER, bf_filename), 'w') as f:
                    for item in bf_discovered:
                        f.write(f"{item['status']} {item['url']}\n")

            logger.info(f"Combined task {task_id} finished: {len(all_urls)} total URLs, {len(bf_discovered)} bruteforced")

    else:
        # Bruteforce disabled or cancelled
        task = load_task(task_id)
        if task and not task.get('cancelled', False):
            task['status'] = 'done'
            task['progress'] = 100
            save_task(task_id, task)

# --------------------------------
# Routes
# --------------------------------
def register_routes(app):
    @app.route('/crawler/start', methods=['POST'])
    def crawler_start():
        start_url = request.form.get('start_url', '').strip()
        if not start_url:
            return jsonify({'error': 'Start URL required'}), 400
        if not start_url.startswith(('http://', 'https://')):
            start_url = 'https://' + start_url

        max_pages = int(request.form.get('max_pages', 0))
        max_depth = int(request.form.get('max_depth', 3))
        enable_bruteforce = request.form.get('enable_bruteforce', 'false').lower() == 'true'
        wordlist_raw = request.form.get('wordlist', '').strip()
        wordlist = [w.strip() for w in wordlist_raw.split(',') if w.strip()] if wordlist_raw else DEFAULT_WORDLIST
        extensions_raw = request.form.get('extensions', '').strip()
        if extensions_raw:
            extensions = [e.strip() for e in extensions_raw.split(',') if e.strip()]
        else:
            extensions = DEFAULT_EXTENSIONS
        threads = int(request.form.get('threads', 20))

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
            'enable_bruteforce': enable_bruteforce,
            'wordlist': wordlist,
            'extensions': extensions,
            'threads': threads,
            'total_pages': 0,
            'internal_urls': [],
            'domains': [],
            'current_url': None,
            'target_domain': None,
            'bf_discovered': [],
            'bf_checked': 0,
            'bf_total': 0,
            'bf_done': False,
            'error_msg': None,
        }
        save_task(task_id, task_data)

        threading.Thread(target=run_combined, args=(task_id, start_url, max_pages, max_depth, enable_bruteforce, wordlist, extensions, threads), daemon=True).start()
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
            'enable_bruteforce': task.get('enable_bruteforce', False),
            'bf_discovered': task.get('bf_discovered', []),
            'bf_checked': task.get('bf_checked', 0),
            'bf_total': task.get('bf_total', 0),
            'bf_done': task.get('bf_done', False),
            'error_msg': task.get('error_msg'),
        })

    @app.route('/crawler/download_urls/<task_id>')
    def crawler_download_urls(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        domain = task.get('target_domain', 'unknown')
        filename = f"{domain}_urls.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            # Rebuild if missing
            all_urls = list(task.get('internal_urls', [])) + [item['url'] for item in task.get('bf_discovered', [])]
            all_urls = sorted(set(all_urls))
            with open(filepath, 'w') as f:
                f.write('\n'.join(all_urls))
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

    @app.route('/crawler/download_directories/<task_id>')
    def crawler_download_directories(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        urls = task.get('internal_urls', []) + [item['url'] for item in task.get('bf_discovered', [])]
        if not urls:
            return jsonify({'error': 'No URLs discovered'}), 404
        dirs = set()
        for u in urls:
            path = urlparse(u).path
            if path:
                dirs.add(path)
        if not dirs:
            return jsonify({'error': 'No directories found'}), 404
        domain = task.get('target_domain', 'unknown')
        filename = f"{domain}_directories.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, 'w') as f:
            f.write('\n'.join(sorted(dirs)))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/download_bruteforce/<task_id>')
    def crawler_download_bruteforce(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        bf = task.get('bf_discovered', [])
        if not bf:
            return jsonify({'error': 'No bruteforce results'}), 404
        domain = task.get('target_domain', 'unknown')
        filename = f"{domain}_bruteforce.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                for item in bf:
                    f.write(f"{item['status']} {item['url']}\n")
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/list_files')
    def crawler_list_files():
        files = []
        for f in os.listdir(UPLOAD_FOLDER):
            if any(f.endswith(ext) for ext in ['_urls.txt', '_domains.txt', '_directories.txt', '_bruteforce.txt']):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
