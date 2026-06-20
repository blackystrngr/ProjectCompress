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

# Regex for CSS url(...)
CSS_URL_RE = re.compile(r'url\([\'"]?([^\'"\)]+)[\'"]?\)', re.IGNORECASE)
JS_URL_RE = re.compile(r'[\'"](https?://[^\'"]+)[\'"]', re.IGNORECASE)
IPV4_RE = re.compile(r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b')
IPV6_RE = re.compile(r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b')

# Default wordlist and extensions for bruteforce
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
    for match in CSS_URL_RE.finditer(css_text):
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
    for match in JS_URL_RE.finditer(js_text):
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

def fetch_and_extract(url, session, processed, target_domain, all_urls, domains, ip_addresses, queue, max_depth, depth):
    if url in processed:
        return
    processed.add(url)
    try:
        resp = session.get(url, allow_redirects=True, timeout=15)
        if resp.status_code != 200:
            return
        content_type = resp.headers.get('content-type', '').lower()
        text = resp.text
        ips = extract_ips_from_text(text)
        ip_addresses.update(ips)
        extracted_urls = set()
        if 'text/html' in content_type:
            extracted_urls = extract_urls_from_html(text, url)
        elif 'text/css' in content_type:
            extracted_urls = extract_urls_from_css(text, url)
        elif 'application/javascript' in content_type or 'text/javascript' in content_type:
            extracted_urls = extract_urls_from_js(text, url)
        else:
            return
        # Extract domains from the URL itself
        dom = extract_domain(url)
        if dom:
            domains.add(dom)
        # Process extracted URLs
        for extracted_url in extracted_urls:
            all_urls.add(extracted_url)
            dom = extract_domain(extracted_url)
            if dom:
                domains.add(dom)
            if is_same_domain(extracted_url, target_domain) and depth + 1 <= max_depth:
                queue.append((extracted_url, depth + 1))
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")

def run_crawler(task_id, start_url, max_pages, max_depth, enable_bruteforce, wordlist, extensions, threads):
    logger.info(f"Crawler task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = extract_domain(start_url)
    if not target_domain:
        logger.error(f"Could not extract domain from {start_url}")
        return

    visited_urls = set()
    processed_urls = set()
    all_urls = set()
    domains = set()
    ip_addresses = set()
    queue = [(start_url, 0)]
    visited_urls.add(start_url)
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
    task['enable_bruteforce'] = enable_bruteforce
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    # Phase 1: Recursive crawling
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = set()
        # Helper to submit a task
        def submit_task(url, depth):
            if url in processed_urls:
                return
            processed_urls.add(url)
            future = executor.submit(fetch_and_extract, url, session, processed_urls, target_domain, all_urls, domains, ip_addresses, queue, max_depth, depth)
            futures.add(future)

        # Submit initial
        submit_task(start_url, 0)

        # Process queue dynamically
        while queue or futures:
            # Submit new tasks from queue (up to threads limit)
            while queue and len(futures) < threads:
                url, depth = queue.pop(0)
                if url in processed_urls:
                    continue
                submit_task(url, depth)

            # Check for completed futures
            done = set()
            for future in futures:
                if future.done():
                    done.add(future)
            for future in done:
                futures.remove(future)

            # Update progress
            pages_visited = len(processed_urls)
            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if task:
                    task['progress'] = int(100 * pages_visited / max_pages) if max_pages > 0 else 0
                    task['total_pages'] = pages_visited
                    task['discovered_urls'] = list(all_urls)
                    task['domains'] = list(domains | ip_addresses)
                    task['current_url'] = current_url
                    save_task(task_id, task)

            # Check cancellation
            task = load_task(task_id)
            if task and task.get('cancelled', False):
                logger.info(f"Task {task_id} cancelled")
                executor.shutdown(wait=False)
                return

            time.sleep(0.05)  # small delay to avoid busy loop

    # Phase 2: Bruteforce (if enabled)
    if enable_bruteforce and not load_task(task_id).get('cancelled', False):
        logger.info(f"Starting bruteforce for {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'bruteforcing'
            save_task(task_id, task)

        base_url = start_url if start_url.endswith('/') else start_url + '/'
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

        def check_path(path):
            full_url = normalize_url(path, base_url)
            if not full_url:
                return None
            try:
                resp = bf_session.head(full_url, allow_redirects=True, timeout=5)
                if resp.status_code in (200, 301, 302, 403, 401):
                    # Fetch the file if it's text-based to extract domains
                    if any(full_url.endswith(ext) for ext in ['.css', '.js', '.json', '.xml', '.txt', '.php', '.html']):
                        # We'll fetch it later; just record the URL
                        return full_url, resp.status_code
                    else:
                        # For images/fonts, we only want the domain
                        dom = extract_domain(full_url)
                        if dom:
                            return full_url, resp.status_code, dom
                return None
            except:
                return None

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {executor.submit(check_path, p): p for p in paths}
            for future in as_completed(futures):
                if load_task(task_id).get('cancelled', False):
                    executor.shutdown(wait=False)
                    break
                result = future.result()
                bf_checked += 1
                if result:
                    full_url, status = result[0], result[1]
                    bf_discovered.append({'url': full_url, 'status': status})
                    all_urls.add(full_url)
                    dom = extract_domain(full_url)
                    if dom:
                        domains.add(dom)
                    # If it's a text file, fetch it to extract more domains
                    if any(full_url.endswith(ext) for ext in ['.css', '.js', '.json', '.xml', '.txt', '.php', '.html']):
                        # We could fetch here, but we'll do it in a separate small loop after bruteforce
                        pass
                if bf_checked % 50 == 0:
                    task = load_task(task_id)
                    if task:
                        task['progress'] = 50 + int(25 * bf_checked / bf_total)  # total progress 50-75%
                        task['discovered_urls'] = list(all_urls)
                        task['domains'] = list(domains | ip_addresses)
                        save_task(task_id, task)

        # After bruteforce, fetch found text files to extract more domains
        for item in bf_discovered:
            url = item['url']
            if any(url.endswith(ext) for ext in ['.css', '.js', '.json', '.xml', '.txt', '.php', '.html']):
                # Use the existing function
                fetch_and_extract(url, bf_session, set(), target_domain, all_urls, domains, ip_addresses, [], 0, 0)

    # Done
    task = load_task(task_id)
    if task:
        task['status'] = 'done'
        task['progress'] = 100
        task['total_pages'] = len(processed_urls)
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

        enable_bruteforce = request.form.get('enable_bruteforce', 'false').lower() == 'true'
        wordlist_raw = request.form.get('wordlist', '').strip()
        if wordlist_raw:
            wordlist = [w.strip() for w in wordlist_raw.split(',') if w.strip()]
        else:
            wordlist = DEFAULT_WORDLIST
        extensions_raw = request.form.get('extensions', '').strip()
        if extensions_raw:
            extensions = [e.strip() for e in extensions_raw.split(',') if e.strip()]
        else:
            extensions = DEFAULT_EXTENSIONS
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
            'enable_bruteforce': enable_bruteforce,
            'wordlist': wordlist,
            'extensions': extensions,
            'threads': threads,
            'total_pages': 0,
            'discovered_urls': [],
            'domains': [],
            'current_url': None,
            'error_msg': None,
        }
        save_task(task_id, task_data)

        threading.Thread(target=run_crawler, args=(task_id, start_url, max_pages, max_depth, enable_bruteforce, wordlist, extensions, threads), daemon=True).start()
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
            'enable_bruteforce': task.get('enable_bruteforce', False),
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
