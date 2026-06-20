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

# ---- Constants ----
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

DEFAULT_EXTENSIONS = [
    '.php', '.html', '.txt', '.log', '.bak', '.old', '.zip', '.tar.gz',
    '.sql', '.env', '.ini', '.xml', '.json', '.yml', '.yaml', '.conf',
    '.config', '.htaccess', '.htpasswd', '.inc', '.class', '.js', '.css',
    '.jpg', '.png', '.gif', '.ico', '.svg', '.webp', '.woff', '.woff2',
    '.ttf', '.eot'
]

# ---- Domain extraction helpers ----
def extract_domain_from_url(url):
    try:
        parsed = urlparse(url)
        if parsed.netloc:
            domain = parsed.netloc.lower()
            if domain.startswith('www.'):
                domain = domain[4:]
            return domain
    except:
        pass
    return None

def extract_domains_from_text(text, base_domain=None):
    url_pattern = re.compile(r'(https?://[^\s<>"\']+)', re.IGNORECASE)
    domains = set()
    for match in url_pattern.finditer(text):
        url = match.group(1)
        domain = extract_domain_from_url(url)
        if domain and domain != base_domain:
            domains.add(domain)
    return domains

def normalize_url(url, base):
    try:
        return urljoin(base, url)
    except:
        return None

def is_same_domain(url, target_domain):
    domain = extract_domain_from_url(url)
    return domain == target_domain

def fetch_and_extract_domains(url, session, base_domain, visited_files, queue_for_fetch):
    if url in visited_files:
        return set()
    visited_files.add(url)
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return set()
        content_type = resp.headers.get('content-type', '').lower()
        text = resp.text
        domains = set()
        if 'text/html' in content_type:
            soup = BeautifulSoup(text, 'html.parser')
            for tag in soup.find_all(['a', 'link', 'script', 'img', 'iframe', 'source']):
                src = tag.get('href') or tag.get('src')
                if src:
                    abs_url = normalize_url(src, url)
                    if abs_url:
                        domains.update(extract_domain_from_url(abs_url))
                        if is_same_domain(abs_url, base_domain) and abs_url not in visited_files:
                            queue_for_fetch.append(abs_url)
            domains.update(extract_domains_from_text(text, base_domain))
        elif 'text/css' in content_type:
            domains.update(extract_domains_from_text(text, base_domain))
        elif 'application/javascript' in content_type or 'text/javascript' in content_type:
            domains.update(extract_domains_from_text(text, base_domain))
        domains.update(extract_domain_from_url(url))
        return domains
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return set()

# ---- Main crawler + bruteforce + domain extraction ----
def run_domain_crawler(task_id, start_url, max_pages, max_depth, enable_bruteforce, wordlist, extensions, threads):
    logger.info(f"Domain crawler {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = extract_domain_from_url(start_url)
    if not target_domain:
        logger.error(f"Could not extract domain from {start_url}")
        return

    visited_pages = set()
    visited_files = set()
    all_domains = set()
    queue = [(start_url, 0)]
    visited_pages.add(start_url)
    visited_files.add(start_url)
    pages_visited = 0
    current_url = start_url
    asset_queue = []
    bf_discovered_paths = []

    task['status'] = 'crawling'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['target_domain'] = target_domain
    task['current_url'] = current_url
    task['all_domains'] = []
    task['enable_bruteforce'] = enable_bruteforce
    task['bf_discovered'] = []
    task['bf_checked'] = 0
    task['bf_total'] = 0
    task['bf_done'] = False
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 30)

    # ---- Phase 1: Crawl HTML pages ----
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

            new_domains = fetch_and_extract_domains(url, session, target_domain, visited_files, asset_queue)
            all_domains.update(new_domains)
            pages_visited += 1

            # Extract links for further crawling
            try:
                resp = session.get(url, timeout=20)
                if resp.status_code == 200 and 'text/html' in resp.headers.get('content-type', ''):
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        abs_url = normalize_url(href, url)
                        if abs_url and is_same_domain(abs_url, target_domain) and abs_url not in visited_pages:
                            visited_pages.add(abs_url)
                            if depth + 1 <= max_depth:
                                queue.append((abs_url, depth + 1))
            except:
                pass

            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if not task:
                    break
                task['progress'] = int(100 * pages_visited / max_pages) if max_pages > 0 else 0
                task['total_pages'] = pages_visited
                task['all_domains'] = list(all_domains)
                task['current_url'] = current_url
                save_task(task_id, task)

            task = load_task(task_id)
            if task and task.get('cancelled', False):
                logger.info(f"Task {task_id} cancelled")
                break

        # Phase 1 done – now fetch all discovered assets
        asset_count = 0
        while asset_queue and asset_count < 5000:
            asset_url = asset_queue.pop(0)
            if asset_url in visited_files:
                continue
            new_domains = fetch_and_extract_domains(asset_url, session, target_domain, visited_files, asset_queue)
            all_domains.update(new_domains)
            asset_count += 1
            if asset_count % 50 == 0:
                task = load_task(task_id)
                if task:
                    task['all_domains'] = list(all_domains)
                    save_task(task_id, task)

        # ---- Phase 2: Bruteforce (if enabled) ----
        if enable_bruteforce and not task.get('cancelled', False):
            logger.info(f"Starting bruteforce for {task_id}")
            task = load_task(task_id)
            if task:
                task['status'] = 'bruteforcing'
                task['bf_discovered'] = []
                task['bf_checked'] = 0
                task['bf_total'] = 0
                save_task(task_id, task)

            base = start_url if start_url.endswith('/') else start_url + '/'
            bf_session = requests.Session()
            bf_session.headers.update({'User-Agent': USER_AGENT})
            bf_session.timeout = (5, 10)

            paths = []
            for word in wordlist:
                paths.append(word + '/')
                for ext in extensions:
                    paths.append(word + ext)
            bf_total = len(paths)
            bf_checked = 0
            bf_discovered = []

            def process_bf_path(path):
                full_url = normalize_url(path, base)
                try:
                    resp = bf_session.head(full_url, allow_redirects=True, timeout=5)
                    if resp.status_code in (200, 301, 302, 403, 401):
                        return full_url, resp.status_code
                except:
                    pass
                return None, None

            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {executor.submit(process_bf_path, p): p for p in paths}
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
                            task['progress'] = 50 + int(50 * progress_bf / 100)
                            save_task(task_id, task)

            # After bruteforce, fetch discovered CSS/JS files to extract domains
            for item in bf_discovered:
                url = item['url']
                if url not in visited_files:
                    if any(url.endswith(ext) for ext in ['.css', '.js', '.json', '.xml', '.txt']):
                        new_domains = fetch_and_extract_domains(url, session, target_domain, visited_files, [])
                        all_domains.update(new_domains)

            # Done with bruteforce
            task = load_task(task_id)
            if task and not task.get('cancelled', False):
                task['status'] = 'done'
                task['progress'] = 100
                task['bf_discovered'] = bf_discovered
                task['bf_checked'] = bf_checked
                task['bf_total'] = bf_total
                task['bf_done'] = True
                task['all_domains'] = list(all_domains)
                save_task(task_id, task)

    except Exception as e:
        logger.exception(f"Domain crawler {task_id} failed")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)

    # Final save
    task = load_task(task_id)
    if task:
        task['all_domains'] = list(all_domains)
        save_task(task_id, task)

    # Save domain list to file
    domain_filename = f"{target_domain}_domains.txt"
    with open(os.path.join(UPLOAD_FOLDER, domain_filename), 'w') as f:
        f.write('\n'.join(sorted(all_domains)))
    logger.info(f"Domain crawler {task_id} finished: {len(all_domains)} domains found")

# ---- Flask routes ----
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
            'target_domain': None,
            'total_pages': 0,
            'current_url': None,
            'all_domains': [],
            'bf_discovered': [],
            'bf_checked': 0,
            'bf_total': 0,
            'bf_done': False,
            'error_msg': None,
        }
        save_task(task_id, task_data)

        threading.Thread(target=run_domain_crawler, args=(task_id, start_url, max_pages, max_depth, enable_bruteforce, wordlist, extensions, threads), daemon=True).start()
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
            'target_domain': task.get('target_domain'),
            'current_url': task.get('current_url'),
            'all_domains': task.get('all_domains', []),
            'enable_bruteforce': task.get('enable_bruteforce', False),
            'bf_discovered': task.get('bf_discovered', []),
            'bf_checked': task.get('bf_checked', 0),
            'bf_total': task.get('bf_total', 0),
            'bf_done': task.get('bf_done', False),
            'error_msg': task.get('error_msg'),
        })

    @app.route('/crawler/download_domains/<task_id>')
    def crawler_download_domains(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        domains = task.get('all_domains', [])
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
            if f.endswith('_domains.txt') or f.endswith('_bruteforce.txt'):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
