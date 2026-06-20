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

# IP Regex (IPv4)
IP_RE = re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')

# Domain TLD list (common ones)
TLD_LIST = {'.com', '.org', '.net', '.edu', '.gov', '.mil', '.io', '.co', '.uk', '.au', '.ca', '.de', '.fr', '.jp', '.cn', '.in', '.br', '.mx', '.it', '.nl', '.kr', '.se', '.no', '.fi', '.dk', '.ch', '.at', '.be', '.pl', '.ru', '.za', '.eg', '.sa', '.ae', '.lk', '.my', '.sg', '.th', '.vn', '.ph', '.pk', '.bd', '.np', '.lk'}

def is_valid_domain(domain):
    """Check if domain is valid (has at least one dot and a valid TLD)."""
    if not domain or not isinstance(domain, str):
        return False
    domain = domain.lower()
    # Skip if it's just "http" or "https"
    if domain in ('http', 'https'):
        return False
    # Skip if it contains spaces or special chars except dot and dash
    if re.search(r'[^a-z0-9\.\-]', domain):
        return False
    # Must have a dot
    if '.' not in domain:
        return False
    # Check if any TLD is present
    has_tld = any(domain.endswith(tld) for tld in TLD_LIST)
    # Also allow domains like "localhost" or "example" if we want, but we require a TLD.
    # For IP, we'll handle separately.
    return has_tld

def is_valid_ip(ip):
    """Check if string is a valid IPv4 address."""
    if not ip:
        return False
    parts = ip.split('.')
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        if int(p) < 0 or int(p) > 255:
            return False
    return True

def clean_url(url):
    """Remove trailing punctuation and clean the URL."""
    if not url:
        return None
    # Remove trailing punctuation that might be appended (e.g., ").", ")", "]" etc.)
    url = url.rstrip('.,;:!?)]}')
    # Remove leading/trailing spaces
    url = url.strip()
    return url

def normalize_url(url, base):
    """Convert relative URL to absolute and clean it."""
    try:
        url = clean_url(url)
        if not url:
            return None
        # Handle protocol-relative URLs (//example.com)
        if url.startswith('//'):
            url = 'https:' + url
        return urljoin(base, url)
    except:
        return None

def extract_domain_from_url(url):
    """Extract domain from a full URL, return None if invalid."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        # Remove port numbers
        if ':' in domain:
            domain = domain.split(':')[0]
        # Skip if domain is empty or just "http" / "https"
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

def extract_links_from_html(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for tag in soup.find_all(['a', 'link', 'script', 'img', 'iframe', 'source']):
        src = tag.get('href') or tag.get('src')
        if src:
            abs_url = normalize_url(src, base_url)
            if abs_url:
                links.append(abs_url)
    # srcset
    for tag in soup.find_all(['img', 'source']):
        srcset = tag.get('srcset')
        if srcset:
            for part in srcset.split(','):
                part = part.strip().split(' ')[0]
                if part:
                    abs_url = normalize_url(part, base_url)
                    if abs_url:
                        links.append(abs_url)
    # data-* attributes
    for tag in soup.find_all():
        for attr in tag.attrs:
            if attr.startswith('data-') and ('src' in attr or 'url' in attr):
                val = tag.get(attr)
                if val and isinstance(val, str) and (val.startswith('http') or val.startswith('/')):
                    abs_url = normalize_url(val, base_url)
                    if abs_url:
                        links.append(abs_url)
    return links

def extract_urls_from_css(css_text, base_url):
    urls = []
    css_url_re = re.compile(r'url\([\'"]?([^\'"\)]+)[\'"]?\)', re.IGNORECASE)
    for match in css_url_re.finditer(css_text):
        url = match.group(1).strip()
        if url and not url.startswith('data:') and not url.startswith('#'):
            abs_url = normalize_url(url, base_url)
            if abs_url:
                urls.append(abs_url)
    import_re = re.compile(r'@import\s+[\'"]([^\'"]+)[\'"]', re.IGNORECASE)
    for match in import_re.finditer(css_text):
        url = match.group(1).strip()
        abs_url = normalize_url(url, base_url)
        if abs_url:
            urls.append(abs_url)
    return urls

def extract_urls_from_js(js_text, base_url):
    urls = []
    js_url_re = re.compile(r'[\'"](https?://[^\s<>"\']+)[\'"]', re.IGNORECASE)
    for match in js_url_re.finditer(js_text):
        url = match.group(1).strip()
        abs_url = normalize_url(url, base_url)
        if abs_url:
            urls.append(abs_url)
    return urls

def run_crawler(task_id, start_url, max_pages, max_depth):
    logger.info(f"Crawler task {task_id} started: {start_url}")

    task = load_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    target_domain = extract_domain_from_url(start_url)
    if not target_domain:
        logger.error(f"Could not extract domain from {start_url}")
        return

    visited = set()
    processed_urls = set()
    all_urls = set()
    domains = set()
    ips = set()
    queue = [(start_url, 0)]
    visited.add(start_url)
    all_urls.add(start_url)
    pages_visited = 0
    current_url = start_url

    task['status'] = 'running'
    task['progress'] = 0
    task['total_pages'] = 0
    task['max_pages'] = max_pages
    task['max_depth'] = max_depth
    task['discovered_urls'] = list(all_urls)
    task['domains'] = list(domains)
    task['ips'] = list(ips)
    task['current_url'] = current_url
    save_task(task_id, task)

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    session.timeout = (10, 20)

    try:
        while queue and (pages_visited < max_pages or max_pages == 0):
            url, depth = queue.pop(0)
            if url in processed_urls:
                continue
            processed_urls.add(url)
            current_url = url

            task = load_task(task_id)
            if task:
                task['current_url'] = current_url
                save_task(task_id, task)

            try:
                resp = session.get(url, allow_redirects=True, timeout=15)
                if resp.status_code != 200:
                    continue
                content_type = resp.headers.get('content-type', '').lower()
                text = resp.text
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                continue

            extracted_links = []
            if 'text/html' in content_type:
                extracted_links = extract_links_from_html(text, url)
                # Also extract domains and IPs from text
                domains_from_text = re.findall(r'https?://([^\s<>"\']+)', text, re.IGNORECASE)
                for d in domains_from_text:
                    domain = extract_domain_from_url('https://' + d)
                    if domain and is_valid_domain(domain):
                        domains.add(domain)
                ips_from_text = re.findall(IP_RE, text)
                for ip in ips_from_text:
                    if is_valid_ip(ip):
                        ips.add(ip)
            elif 'text/css' in content_type:
                extracted_links = extract_urls_from_css(text, url)
            elif 'application/javascript' in content_type or 'text/javascript' in content_type:
                extracted_links = extract_urls_from_js(text, url)
            else:
                # For other types, just extract domain from the URL itself
                dom = extract_domain_from_url(url)
                if dom and is_valid_domain(dom):
                    domains.add(dom)
                continue

            pages_visited += 1

            for link in extracted_links:
                if link not in visited:
                    visited.add(link)
                    all_urls.add(link)
                    # If same domain (including subdomains), add to queue
                    if is_same_domain(link, target_domain):
                        if depth + 1 <= max_depth:
                            queue.append((link, depth + 1))
                    # Extract domain
                    dom = extract_domain_from_url(link)
                    if dom and is_valid_domain(dom):
                        domains.add(dom)
                    # Extract IP from link if any
                    ip_match = re.search(IP_RE, link)
                    if ip_match and is_valid_ip(ip_match.group()):
                        ips.add(ip_match.group())

            # Update progress
            if pages_visited % 5 == 0:
                task = load_task(task_id)
                if not task:
                    break
                task['progress'] = int(100 * pages_visited / max_pages) if max_pages > 0 else 0
                task['total_pages'] = pages_visited
                task['discovered_urls'] = list(all_urls)
                task['domains'] = list(domains)
                task['ips'] = list(ips)
                task['current_url'] = current_url
                save_task(task_id, task)

            task = load_task(task_id)
            if task and task.get('cancelled', False):
                logger.info(f"Crawler {task_id} cancelled")
                break

        # Done – save results
        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            task['total_pages'] = pages_visited
            task['discovered_urls'] = list(all_urls)
            task['domains'] = list(domains)
            task['ips'] = list(ips)
            task['current_url'] = None
            save_task(task_id, task)

            website_name = get_website_name_from_url(start_url)
            # Save domains file
            domain_filename = f"{website_name}_domains.txt"
            domain_filepath = os.path.join(UPLOAD_FOLDER, domain_filename)
            with open(domain_filepath, 'w') as f:
                f.write('\n'.join(sorted(domains)))

            # Save IPs file
            ip_filename = f"{website_name}_ips.txt"
            ip_filepath = os.path.join(UPLOAD_FOLDER, ip_filename)
            if ips:
                with open(ip_filepath, 'w') as f:
                    f.write('\n'.join(sorted(ips)))

            # Save URLs file
            urls_filename = f"crawled_urls_{task_id[:8]}.txt"
            urls_filepath = os.path.join(UPLOAD_FOLDER, urls_filename)
            with open(urls_filepath, 'w') as f:
                f.write('\n'.join(sorted(all_urls)))

            logger.info(f"Crawler {task_id} finished: {len(all_urls)} URLs, {len(domains)} domains, {len(ips)} IPs")

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
        max_depth = int(request.form.get('max_depth', 3))

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
            'ips': [],
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
            'ips': task.get('ips', []),
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

    @app.route('/crawler/download_ips/<task_id>')
    def crawler_download_ips(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        ips = task.get('ips', [])
        if not ips:
            return jsonify({'error': 'No IPs discovered'}), 404
        website_name = get_website_name_from_url(task.get('start_url'))
        filename = f"{website_name}_ips.txt"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                f.write('\n'.join(sorted(ips)))
        return send_file(filepath, as_attachment=True, download_name=filename)

    @app.route('/crawler/list_files')
    def crawler_list_files():
        files = []
        for f in os.listdir(UPLOAD_FOLDER):
            if f.endswith('_domains.txt') or f.endswith('_ips.txt') or f.endswith('_urls.txt'):
                files.append({
                    'name': f,
                    'path': f,
                    'size': os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
                })
        return jsonify(files)
