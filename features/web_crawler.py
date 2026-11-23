"""
features/web_crawler.py

Concurrent, robots-aware recursive domain crawler.

Features:
  • Worker pool (1–32 concurrent fetchers)
  • Robots.txt respect (opt-out)
  • Sitemap.xml seeding (opt-out)
  • Optional JS rendering via Playwright (worker cap 4)
  • curl_cffi TLS impersonation when available
  • Cookie persistence from cookies.txt
  • Per-host rate limiting
  • Retry with exponential backoff
  • Full IPv4 + RFC-compliant IPv6 extraction
  • Bounded URL memory (max_seen cap)
  • SSE streaming + proper cancel finalization
  • Scope: exact host or base domain (all subdomains)
"""

import os
import re
import json
import time
import uuid
import queue
import random
import ipaddress
import logging
import threading
from urllib.parse import urljoin, urlparse, urlunparse, urldefrag
from urllib.robotparser import RobotFileParser
from collections import defaultdict

import requests
from bs4 import BeautifulSoup
from flask import request, jsonify, send_file, Response, stream_with_context, abort

from tasks import save_task, load_task
from config import UPLOAD_FOLDER, PROXY_DICT

logger = logging.getLogger(__name__)

# ─── Optional dependencies ───────────────────────────────────────
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI = True
except ImportError:
    CURL_CFFI = False
    logger.info("curl_cffi not installed — using stock requests (slower on Cloudflare)")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT = True
except ImportError:
    PLAYWRIGHT = False

try:
    from config import COOKIES_FILE as _CF
    COOKIES_FILE = _CF or ''
except Exception:
    COOKIES_FILE = ''


# ─── Constants ───────────────────────────────────────────────────
DEFAULT_WORKERS = 4
MAX_WORKERS = 32
JS_MAX_WORKERS = 4          # each worker opens its own Chromium
DEFAULT_TIMEOUT = 20
DEFAULT_DELAY_MS = 0
DEFAULT_MAX_SEEN = 100_000
IDLE_THRESHOLD_SEC = 8.0    # quiescence window before finalizing

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/120.0.0.0 Safari/537.36')

UA_POOL = [
    DEFAULT_UA,
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) '
    'Gecko/20100101 Firefox/122.0',
]

SKIP_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico', '.webp', '.bmp',
    '.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv',
    '.mp3', '.wav', '.flac', '.aac', '.ogg',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.gz', '.rar', '.7z',
    '.ttf', '.woff', '.woff2', '.eot',
    '.js', '.css', '.xml', '.json', '.php', '.asp', '.aspx', '.jsp',
    '.do', '.action', '.cgi', '.pl', '.py', '.rb', '.go', '.java',
    '.class', '.jar', '.war', '.ear',
    '.rss', '.atom', '.feed', '.conf', '.cfg', '.ini', '.yaml', '.yml',
    '.toml', '.htaccess', '.htpasswd', '.sql', '.db', '.sqlite',
    '.log', '.tmp', '.bak', '.exe', '.dll', '.so', '.dylib',
    '.pem', '.key', '.crt', '.csr',
}

IPV4_RE = re.compile(
    r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}'
    r'(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b'
)

# Full RFC 4291 IPv6 (handles compressed :: forms)
IPV6_RE = re.compile(
    r'(?<![0-9a-fA-F:])(?:'
    r'(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|'
    r'(?:[0-9a-fA-F]{1,4}:){1,7}:|'
    r'(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|'
    r'(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}|'
    r'(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}|'
    r'(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}|'
    r'(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}|'
    r'[0-9a-fA-F]{1,4}:(?::[0-9a-fA-F]{1,4}){1,6}|'
    r':(?::[0-9a-fA-F]{1,4}){1,7}|'
    r'::'
    r')(?![0-9a-fA-F:])'
)


# ─── URL / domain helpers ────────────────────────────────────────
def _canonicalize(url, base):
    """Resolve against base, strip fragment, lowercase scheme/host."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip().rstrip('.,;:!?)]}')
    try:
        if url.startswith('//'):
            url = 'https:' + url
        resolved = urljoin(base, url)
        parsed = urlparse(resolved)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return None
        resolved, _ = urldefrag(resolved)
        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or '/',
            parsed.params,
            parsed.query,
            '',
        ))
    except Exception:
        return None


def _clean_domain(host):
    if not host:
        return None
    host = host.lower()
    if ':' in host:
        host = host.split(':')[0]
    if host.startswith('www.'):
        host = host[4:]
    host = host.rstrip('.')
    return host if _is_valid_domain(host) else None


def _is_valid_domain(domain):
    if not domain or not isinstance(domain, str):
        return False
    domain = domain.lower().strip()
    if '.' not in domain:
        return False
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', domain):
        return False
    if any(c in domain for c in '/?=&%#;:@'):
        return False
    for ext in SKIP_EXTENSIONS:
        if domain.endswith(ext):
            return False
    pattern = re.compile(
        r'^([a-z0-9]([a-z0-9-]*[a-z0-9])?)\.([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z]{2,}$'
    )
    if not pattern.match(domain):
        return False
    parts = domain.split('.')
    if len(parts) < 2 or len(parts[0]) < 2:
        return False
    if domain in ('localhost', 'local', 'internal', 'intranet', 'router', 'gateway'):
        return False
    return True


def _extract_domain(url):
    try:
        return _clean_domain(urlparse(url).netloc.lower())
    except Exception:
        return None


def _same_scope(url, target_domain, scope):
    """True if URL is within crawl scope (exact host or base-domain subtree)."""
    try:
        h = urlparse(url).netloc.lower().split(':')[0]
        if h.startswith('www.'):
            h = h[4:]
        target = target_domain[4:] if target_domain.startswith('www.') else target_domain
        if scope == 'exact':
            return h == target
        return h == target or h.endswith('.' + target)
    except Exception:
        return False


def _extract_ips(text):
    """Extract valid, non-loopback IPv4 + IPv6 addresses."""
    out = set()
    for m in IPV4_RE.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(0))
            if not (ip.is_loopback or ip.is_multicast or ip.is_unspecified):
                out.add(str(ip))
        except ValueError:
            pass
    for m in IPV6_RE.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(0))
            if not (ip.is_loopback or ip.is_multicast or ip.is_unspecified):
                out.add(str(ip))
        except ValueError:
            pass
    return out


# ─── Session factory ─────────────────────────────────────────────
def _make_session(worker_id=0, use_impersonate=True):
    """Build a session with cookies, proxy, UA, and TLS fingerprint."""
    if CURL_CFFI and use_impersonate:
        try:
            s = curl_requests.Session(impersonate='chrome120')
        except Exception:
            try:
                s = curl_requests.Session(impersonate='chrome')
            except Exception:
                s = requests.Session()
    else:
        s = requests.Session()

    if PROXY_DICT:
        try:
            s.proxies.update(PROXY_DICT)
        except Exception:
            pass

    if not CURL_CFFI:
        s.headers.update({
            'User-Agent': UA_POOL[worker_id % len(UA_POOL)],
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })

    _apply_cookies(s)
    return s


def _apply_cookies(session):
    if not COOKIES_FILE or not os.path.exists(COOKIES_FILE):
        return
    try:
        with open(COOKIES_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    dom, _, pth, _, _, name, val = parts[:7]
                    try:
                        session.cookies.set(
                            name, val,
                            domain=dom.lstrip('.'),
                            path=pth or '/',
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.debug(f"cookie load failed: {e}")


# ─── HTML extraction ────────────────────────────────────────────
def _extract_urls_from_html(html, base):
    """Extract every plausible URL from tags, JS, meta, and raw text."""
    urls = set()

    if not html:
        return urls

    soup = None
    try:
        soup = BeautifulSoup(html, 'html.parser')
    except Exception:
        pass

    if soup:
        tag_attrs = [
            ('a', 'href'), ('link', 'href'), ('script', 'src'),
            ('img', 'src'), ('iframe', 'src'), ('source', 'src'),
            ('video', 'src'), ('audio', 'src'), ('embed', 'src'),
            ('object', 'data'), ('form', 'action'),
        ]
        for tag_name, attr in tag_attrs:
            for tag in soup.find_all(tag_name):
                v = tag.get(attr)
                if v:
                    u = _canonicalize(v, base)
                    if u:
                        urls.add(u)

        # srcset
        for tag in soup.find_all(['img', 'source']):
            srcset = tag.get('srcset')
            if srcset:
                for part in srcset.split(','):
                    p = part.strip().split(' ')[0]
                    if p:
                        u = _canonicalize(p, base)
                        if u:
                            urls.add(u)

        # data-* attributes
        for tag in soup.find_all():
            for a in tag.attrs:
                if a.startswith('data-') and ('src' in a or 'url' in a or 'href' in a):
                    v = tag.get(a)
                    if v and isinstance(v, str):
                        u = _canonicalize(v, base)
                        if u:
                            urls.add(u)

        # <base>
        base_tag = soup.find('base')
        if base_tag and base_tag.get('href'):
            u = _canonicalize(base_tag['href'], base)
            if u:
                urls.add(u)

        # <meta og:url / twitter:url>
        for meta in soup.find_all('meta'):
            c = meta.get('content')
            prop = (meta.get('property') or meta.get('name') or '').lower()
            if c and prop in ('og:url', 'twitter:url'):
                u = _canonicalize(c, base)
                if u:
                    urls.add(u)

        # inline <script> URLs
        for script in soup.find_all('script'):
            if script.string:
                for m in re.finditer(
                    r'[\'"](https?://[^\'"\s<>]{4,}?)[\'"]',
                    script.string,
                ):
                    u = _canonicalize(m.group(1), base)
                    if u:
                        urls.add(u)

    # Raw regex scan of the full HTML (catches URLs in JSON blobs etc.)
    for m in re.finditer(r'https?://[^\s\'"<>\\]+', html):
        u = _canonicalize(m.group(0), base)
        if u:
            urls.add(u)

    return urls


# ─── Robots.txt & sitemap ───────────────────────────────────────
def _fetch_robots(session, base_url, timeout):
    rp = RobotFileParser()
    try:
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        resp = session.get(robots_url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            return rp
    except Exception as e:
        logger.debug(f"robots.txt fetch failed: {e}")
    rp.allow_all = True
    return rp


def _fetch_sitemap_urls(session, base_url, timeout, max_items=5000):
    """Try /sitemap.xml + any Sitemap: entries from robots.txt."""
    parsed = urlparse(base_url)
    sitemaps = [f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"]

    try:
        resp = session.get(
            f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=timeout,
        )
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.lower().startswith('sitemap:'):
                    sm = line.split(':', 1)[1].strip()
                    if sm:
                        sitemaps.append(sm)
    except Exception:
        pass

    urls = set()
    visited = set()
    queue_sm = list(sitemaps)

    while queue_sm and len(urls) < max_items:
        sm = queue_sm.pop(0)
        if sm in visited:
            continue
        visited.add(sm)
        try:
            resp = session.get(sm, timeout=timeout)
            if resp.status_code != 200:
                continue
            text = resp.text
            for m in re.finditer(r'<loc>\s*([^<\s]+)\s*</loc>', text):
                loc = m.group(1).strip()
                if loc.endswith(('.xml', '.xml.gz')):
                    if loc not in visited and len(queue_sm) < 20:
                        queue_sm.append(loc)
                else:
                    u = _canonicalize(loc, base_url)
                    if u:
                        urls.add(u)
                        if len(urls) >= max_items:
                            break
        except Exception as e:
            logger.debug(f"sitemap {sm} failed: {e}")

    return urls


# ─── Fetching (with retries + optional JS) ──────────────────────
_tl = threading.local()  # thread-local Playwright state


def _fetch_js(url, timeout, worker_id):
    if not PLAYWRIGHT:
        return None, None
    try:
        if not hasattr(_tl, 'pw'):
            _tl.pw = sync_playwright().start()
            _tl.browser = _tl.pw.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox', '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                ],
            )
            _tl.context = _tl.browser.new_context(
                user_agent=UA_POOL[worker_id % len(UA_POOL)],
                viewport={'width': 1280, 'height': 720},
            )
        page = _tl.context.new_page()
        try:
            page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
            page.wait_for_timeout(1200)
            html = page.content()
            final_url = page.url
            return html, final_url
        finally:
            try:
                page.close()
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"JS fetch failed {url}: {e}")
        return None, None


def _fetch_with_retries(ctx, session, url, worker_id):
    retries = ctx.cfg['retries']
    timeout = ctx.cfg['timeout']
    use_js = ctx.cfg['render_js']

    for attempt in range(retries + 1):
        if ctx.stop_event.is_set():
            return None, None
        try:
            if use_js:
                return _fetch_js(url, timeout, worker_id)
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            ct = (resp.headers.get('content-type') or '').lower()
            if not any(x in ct for x in ('html', 'xml', 'text/plain')):
                return None, None
            return resp.text, resp.url
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5 ** attempt)
                continue
            logger.debug(f"fetch failed {url}: {e}")
            return None, None
    return None, None


# ─── Per-task state ──────────────────────────────────────────────
class CrawlContext:
    def __init__(self, task_id, cfg):
        self.task_id = task_id
        self.cfg = cfg
        self.start_url = cfg['start_url']
        self.target_domain = _extract_domain(self.start_url)

        self.stop_event = threading.Event()
        self.frontier = queue.Queue()
        self.work_available = threading.Semaphore(0)

        self.seen = set()
        self.seen_lock = threading.Lock()

        self.domains = set()
        self.domains_lock = threading.Lock()

        self.ips = set()
        self.ips_lock = threading.Lock()

        self.pending = 0
        self.pending_lock = threading.Lock()

        self.stats = {
            'pages_visited': 0,
            'pages_failed': 0,
            'urls_found': 0,
            'workers_active': 0,
        }
        self.stats_lock = threading.Lock()

        self.host_last_hit = defaultdict(float)
        self.host_lock = threading.Lock()

        self.robots = None
        self.closed = False
        self.started_at = time.time()

        self.output_file = cfg['output_file']
        self.output_path = os.path.join(UPLOAD_FOLDER, self.output_file)
        self.file_lock = threading.Lock()

        # SSE incremental tracking
        self._last_pushed_domains = set()
        self._last_pushed_ips = set()


# ─── File helpers ───────────────────────────────────────────────
def _append_file(ctx, lines):
    if ctx.closed:
        return
    with ctx.file_lock:
        if ctx.closed:
            return
        try:
            with open(ctx.output_path, 'a', encoding='utf-8') as f:
                for line in lines:
                    f.write(line.rstrip() + '\n')
        except Exception as e:
            logger.warning(f"append failed: {e}")


# ─── SSE ────────────────────────────────────────────────────────
_crawler_queues = {}
_queue_lock = threading.Lock()


def get_queue(task_id):
    with _queue_lock:
        if task_id not in _crawler_queues:
            _crawler_queues[task_id] = queue.Queue(maxsize=200)
        return _crawler_queues[task_id]


def remove_queue(task_id):
    with _queue_lock:
        _crawler_queues.pop(task_id, None)


def push_update(task_id, payload):
    q = get_queue(task_id)
    try:
        q.put_nowait(payload)
    except queue.Full:
        try:
            q.get_nowait()
            q.put_nowait(payload)
        except Exception:
            pass


def _push_sse_state(ctx, final=False):
    with ctx.domains_lock:
        current_domains = set(ctx.domains)
    new_domains = current_domains - ctx._last_pushed_domains
    ctx._last_pushed_domains = current_domains

    with ctx.ips_lock:
        current_ips = set(ctx.ips)
    new_ips = current_ips - ctx._last_pushed_ips
    ctx._last_pushed_ips = current_ips

    with ctx.stats_lock:
        stats = dict(ctx.stats)
    with ctx.pending_lock:
        pending = ctx.pending

    progress = 0
    if ctx.cfg['max_pages']:
        progress = min(99, int(100 * stats['pages_visited'] / ctx.cfg['max_pages']))
    else:
        progress = min(50, stats['pages_visited'] // 10)  # heuristic

    if final:
        progress = 100

    payload = {
        'domains': sorted(new_domains)[:100],
        'ips': sorted(new_ips)[:100],
        'path': '',
        'progress': progress,
        'pages': stats['pages_visited'],
        'urls_count': stats['urls_found'],
        'failed': stats['pages_failed'],
        'workers': stats['workers_active'],
        'pending': pending,
        'elapsed': int(time.time() - ctx.started_at),
        'final': final,
    }
    push_update(ctx.task_id, payload)


def _update_task_state(ctx):
    t = load_task(ctx.task_id)
    if not t:
        return
    with ctx.stats_lock:
        stats = dict(ctx.stats)
    with ctx.domains_lock:
        domain_count = len(ctx.domains)
    with ctx.pending_lock:
        pending = ctx.pending

    progress = 0
    if ctx.cfg['max_pages']:
        progress = min(99, int(100 * stats['pages_visited'] / ctx.cfg['max_pages']))
    else:
        progress = min(99, stats['pages_visited'] // 10)

    t['progress'] = progress
    t['total_pages'] = stats['pages_visited']
    t['total_urls'] = stats['urls_found']
    t['failed_pages'] = stats['pages_failed']
    t['frontier'] = pending
    t['worker_count'] = stats['workers_active']
    t['domains_count'] = domain_count
    save_task(ctx.task_id, t)


# ─── Workers ────────────────────────────────────────────────────
def _process_url(ctx, worker_id, session, url, depth):
    # Per-host rate limit
    if ctx.cfg['delay_ms'] > 0:
        host = urlparse(url).netloc
        with ctx.host_lock:
            elapsed = time.time() - ctx.host_last_hit[host]
            wait = (ctx.cfg['delay_ms'] / 1000.0) - elapsed
            if wait > 0:
                time.sleep(wait)
            ctx.host_last_hit[host] = time.time()

    # Robots check
    if ctx.robots is not None and not ctx.robots.can_fetch('*', url):
        logger.debug(f"[w{worker_id}] robots disallow: {url}")
        return

    html, final_url = _fetch_with_retries(ctx, session, url, worker_id)
    if html is None:
        with ctx.stats_lock:
            ctx.stats['pages_failed'] += 1
        return

    # Extract IPs
    ips = _extract_ips(html)
    if ips:
        with ctx.ips_lock:
            new_ips = ips - ctx.ips
            ctx.ips |= ips
        if new_ips:
            _append_file(ctx, [f"IP: {ip}" for ip in new_ips])

    # Extract URLs
    urls = _extract_urls_from_html(html, final_url)
    new_domains = []
    new_urls_count = 0

    for u in urls:
        with ctx.seen_lock:
            if u in ctx.seen:
                continue
            if len(ctx.seen) >= ctx.cfg['max_seen']:
                logger.warning(f"seen set reached max_seen={ctx.cfg['max_seen']}")
                break
            ctx.seen.add(u)
        new_urls_count += 1

        dom = _extract_domain(u)
        if dom and dom != ctx.target_domain:
            with ctx.domains_lock:
                if dom not in ctx.domains:
                    ctx.domains.add(dom)
                    new_domains.append(dom)

        if (depth < ctx.cfg['max_depth']
                and _same_scope(u, ctx.target_domain, ctx.cfg['scope'])):
            with ctx.pending_lock:
                ctx.pending += 1
                ctx.frontier.put((u, depth + 1))
            ctx.work_available.release()

    if new_urls_count:
        with ctx.stats_lock:
            ctx.stats['urls_found'] += new_urls_count

    if new_domains:
        _append_file(ctx, new_domains)

    with ctx.stats_lock:
        ctx.stats['pages_visited'] += 1

    logger.debug(
        f"[w{worker_id}] {final_url} → {new_urls_count} links, "
        f"{len(new_domains)} new domains"
    )


def _worker(ctx, worker_id):
    session = _make_session(worker_id)
    with ctx.stats_lock:
        ctx.stats['workers_active'] += 1

    try:
        while not ctx.stop_event.is_set():
            if not ctx.work_available.acquire(timeout=2.0):
                continue
            try:
                url, depth = ctx.frontier.get_nowait()
            except queue.Empty:
                continue
            try:
                _process_url(ctx, worker_id, session, url, depth)
            except Exception as e:
                logger.debug(f"[w{worker_id}] error on {url}: {e}")
                with ctx.stats_lock:
                    ctx.stats['pages_failed'] += 1
            finally:
                with ctx.pending_lock:
                    ctx.pending -= 1
    finally:
        with ctx.stats_lock:
            ctx.stats['workers_active'] -= 1
        try:
            session.close()
        except Exception:
            pass


# ─── Monitor ────────────────────────────────────────────────────
def _monitor_until_done(ctx):
    last_pages = 0
    last_change = time.time()

    while not ctx.stop_event.is_set():
        try:
            t = load_task(ctx.task_id)
            if t and t.get('cancelled'):
                logger.info(f"Crawl {ctx.task_id} cancelled by user")
                ctx.stop_event.set()
                break

            with ctx.stats_lock:
                pages = ctx.stats['pages_visited']

            if ctx.cfg['max_pages'] and pages >= ctx.cfg['max_pages']:
                logger.info(f"Crawl {ctx.task_id} hit max_pages={ctx.cfg['max_pages']}")
                ctx.stop_event.set()
                break

            if pages != last_pages:
                last_pages = pages
                last_change = time.time()

            with ctx.pending_lock:
                pending = ctx.pending

            if pending == 0 and (time.time() - last_change) > IDLE_THRESHOLD_SEC:
                logger.info(f"Crawl {ctx.task_id} quiescent — finalizing")
                ctx.stop_event.set()
                break

            _update_task_state(ctx)
            _push_sse_state(ctx)
        except Exception as e:
            logger.exception(f"monitor iteration error: {e}")

        time.sleep(1.0)


# ─── Finalize ───────────────────────────────────────────────────
def _finalize(ctx):
    try:
        with ctx.domains_lock:
            domains = set(ctx.domains)
        with ctx.ips_lock:
            ips = set(ctx.ips)
        all_items = sorted(domains | ips)

        with ctx.file_lock:
            ctx.closed = True
            with open(ctx.output_path, 'w', encoding='utf-8') as f:
                for item in all_items:
                    f.write(item + '\n')

        t = load_task(ctx.task_id)
        if t:
            was_cancelled = t.get('cancelled')
            t['status'] = 'cancelled' if was_cancelled else 'done'
            t['progress'] = 100
            t['total_pages'] = ctx.stats.get('pages_visited', 0)
            t['total_urls'] = ctx.stats.get('urls_found', 0)
            t['failed_pages'] = ctx.stats.get('pages_failed', 0)
            t['domains'] = all_items[:10_000]
            t['current_url'] = None
            t['output_file'] = ctx.output_file
            t['elapsed'] = int(time.time() - ctx.started_at)
            save_task(ctx.task_id, t)

        _push_sse_state(ctx, final=True)
        logger.info(
            f"Crawl {ctx.task_id} done: {len(all_items)} items, "
            f"{ctx.stats.get('pages_visited', 0)} pages, "
            f"{int(time.time() - ctx.started_at)}s"
        )
    except Exception as e:
        logger.exception(f"finalize failed: {e}")
        t = load_task(ctx.task_id)
        if t:
            t['status'] = 'error'
            t['error_msg'] = str(e)
            save_task(ctx.task_id, t)
    finally:
        time.sleep(3)
        remove_queue(ctx.task_id)


# ─── Orchestrator ───────────────────────────────────────────────
def run_crawler(task_id, cfg):
    ctx = CrawlContext(task_id, cfg)

    if not ctx.target_domain:
        t = load_task(task_id)
        if t:
            t['status'] = 'error'
            t['error_msg'] = 'Invalid start URL'
            save_task(task_id, t)
        return

    logger.info(
        f"Crawl {task_id}: {cfg['start_url']} → {ctx.target_domain} "
        f"(workers={cfg['workers']}, max_pages={cfg['max_pages']}, "
        f"max_depth={cfg['max_depth']}, scope={cfg['scope']}, "
        f"js={cfg['render_js']}, robots={cfg['respect_robots']})"
    )

    # Bootstrap: robots + sitemap
    boot = _make_session()
    try:
        if cfg['respect_robots']:
            try:
                ctx.robots = _fetch_robots(boot, cfg['start_url'], cfg['timeout'])
                logger.info(f"Crawl {task_id}: robots.txt loaded")
            except Exception as e:
                logger.debug(f"robots error: {e}")

        initial_urls = {cfg['start_url']}
        if cfg['use_sitemap']:
            try:
                sm_urls = _fetch_sitemap_urls(boot, cfg['start_url'], cfg['timeout'])
                added = 0
                for u in sm_urls:
                    if _same_scope(u, ctx.target_domain, cfg['scope']):
                        initial_urls.add(u)
                        added += 1
                logger.info(f"Crawl {task_id}: sitemap → {added} URLs added")
            except Exception as e:
                logger.debug(f"sitemap error: {e}")
    finally:
        try:
            boot.close()
        except Exception:
            pass

    # Seed file + state
    try:
        with open(ctx.output_path, 'w', encoding='utf-8') as f:
            f.write(ctx.target_domain + '\n')
    except Exception as e:
        logger.error(f"cannot create output: {e}")
        t = load_task(task_id)
        if t:
            t['status'] = 'error'
            t['error_msg'] = str(e)
            save_task(task_id, t)
        return

    with ctx.domains_lock:
        ctx.domains.add(ctx.target_domain)
    ctx._last_pushed_domains = {ctx.target_domain}

    for u in initial_urls:
        with ctx.seen_lock:
            if u in ctx.seen:
                continue
            ctx.seen.add(u)
        with ctx.pending_lock:
            ctx.pending += 1
            ctx.frontier.put((u, 0))
        ctx.work_available.release()

    t = load_task(task_id)
    if t:
        t['status'] = 'crawling'
        t['worker_count'] = cfg['workers']
        t['frontier'] = ctx.pending
        t['output_file'] = cfg['output_file']
        save_task(task_id, t)

    _push_sse_state(ctx)

    # Launch workers
    workers = []
    for i in range(cfg['workers']):
        w = threading.Thread(
            target=_worker, args=(ctx, i), daemon=True,
            name=f"crawler-w{i}",
        )
        w.start()
        workers.append(w)

    # Monitor in this thread
    try:
        _monitor_until_done(ctx)
    except Exception as e:
        logger.exception(f"monitor crashed: {e}")
        ctx.stop_event.set()
    finally:
        ctx.stop_event.set()
        for w in workers:
            w.join(timeout=15)
        _finalize(ctx)


# ─── Routes ─────────────────────────────────────────────────────
def _fmt_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def register_routes(app):

    @app.route('/crawler/start', methods=['POST'])
    def crawler_start():
        start_url = (request.form.get('start_url') or '').strip()
        if not start_url:
            return jsonify({'error': 'start_url required'}), 400
        if not start_url.startswith(('http://', 'https://')):
            start_url = 'https://' + start_url

        def _int(name, default, lo, hi):
            try:
                v = int(request.form.get(name, default))
            except (ValueError, TypeError):
                v = default
            return max(lo, min(hi, v))

        def _bool(name, default=False):
            return request.form.get(name, str(default)).lower() in (
                '1', 'true', 'yes', 'on'
            )

        render_js = _bool('render_js', False)
        if render_js and not PLAYWRIGHT:
            return jsonify({
                'error': 'JS rendering requested but Playwright not installed. '
                         'Run: pip install playwright && playwright install chromium',
            }), 400

        workers_req = _int('workers', DEFAULT_WORKERS, 1, MAX_WORKERS)
        if render_js:
            workers_req = min(workers_req, JS_MAX_WORKERS)

        cfg = {
            'start_url': start_url,
            'max_pages': _int('max_pages', 0, 0, 500_000),
            'max_depth': _int('max_depth', 3, 1, 50),
            'workers': workers_req,
            'delay_ms': _int('delay_ms', DEFAULT_DELAY_MS, 0, 60_000),
            'timeout': _int('timeout', DEFAULT_TIMEOUT, 5, 120),
            'retries': _int('retries', 2, 0, 5),
            'max_seen': _int('max_seen', DEFAULT_MAX_SEEN, 1_000, 2_000_000),
            'respect_robots': _bool('respect_robots', True),
            'use_sitemap': _bool('use_sitemap', True),
            'render_js': render_js,
            'scope': request.form.get('scope', 'base'),
            'output_file': '',
        }
        if cfg['scope'] not in ('exact', 'base'):
            cfg['scope'] = 'base'

        task_id = str(uuid.uuid4())
        host = urlparse(start_url).netloc or 'site'
        safe_host = re.sub(r'[^a-zA-Z0-9]', '_', host)
        cfg['output_file'] = f"{safe_host}_{task_id[:8]}_domains.txt"

        save_task(task_id, {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
            'start_url': start_url,
            'output_file': cfg['output_file'],
            'max_pages': cfg['max_pages'],
            'max_depth': cfg['max_depth'],
            'workers': cfg['workers'],
            'scope': cfg['scope'],
            'total_pages': 0,
            'total_urls': 0,
            'domains': [],
        })

        threading.Thread(
            target=run_crawler, args=(task_id, cfg), daemon=True,
        ).start()

        return jsonify({
            'task_id': task_id,
            'output_file': cfg['output_file'],
            'workers': cfg['workers'],
            'render_js': cfg['render_js'],
        })

    @app.route('/crawler/cancel/<task_id>', methods=['POST'])
    def crawler_cancel(task_id):
        t = load_task(task_id)
        if not t:
            return jsonify({'error': 'Task not found'}), 404
        t['cancelled'] = True
        save_task(task_id, t)
        return jsonify({'status': 'cancellation requested'})

    @app.route('/crawler/stream/<task_id>')
    def crawler_stream(task_id):
        def event_gen():
            q = get_queue(task_id)
            try:
                t = load_task(task_id)
                if t:
                    initial = {
                        'domains': (t.get('domains') or [])[-50:],
                        'ips': [],
                        'path': '',
                        'progress': t.get('progress', 0),
                        'pages': t.get('total_pages', 0),
                        'urls_count': t.get('total_urls', 0),
                        'failed': t.get('failed_pages', 0),
                        'workers': t.get('worker_count', 0),
                        'pending': t.get('frontier', 0),
                        'elapsed': t.get('elapsed', 0),
                        'final': False,
                    }
                    yield f"event: update\ndata: {json.dumps(initial)}\n\n"
                    if t.get('status') in ('done', 'cancelled', 'error'):
                        yield f"event: {t.get('status')}\ndata: \n\n"
                        return

                last_keepalive = time.time()
                while True:
                    try:
                        data = q.get(timeout=5)
                        yield f"event: update\ndata: {json.dumps(data)}\n\n"
                        if data.get('final'):
                            t = load_task(task_id)
                            status = (t or {}).get('status', 'done')
                            yield f"event: {status}\ndata: \n\n"
                            return
                    except queue.Empty:
                        t = load_task(task_id)
                        if not t:
                            yield 'event: error\ndata: {"error":"task lost"}\n\n'
                            return
                        if t.get('status') in ('done', 'cancelled', 'error'):
                            yield f"event: {t.get('status')}\ndata: \n\n"
                            return
                        if time.time() - last_keepalive > 10:
                            yield ': keepalive\n\n'
                            last_keepalive = time.time()
            except GeneratorExit:
                t = load_task(task_id)
                if t and t.get('status') in ('done', 'cancelled', 'error'):
                    remove_queue(task_id)

        return Response(
            stream_with_context(event_gen()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache, no-transform',
                'X-Accel-Buffering': 'no',
            },
        )

    @app.route('/crawler/download_domains/<task_id>')
    def crawler_download_domains(task_id):
        t = load_task(task_id)
        if not t or not t.get('output_file'):
            return jsonify({'error': 'Task not found'}), 404
        fp = os.path.join(UPLOAD_FOLDER, t['output_file'])
        if not os.path.exists(fp):
            return jsonify({'error': 'File not found'}), 404
        return send_file(fp, as_attachment=True, download_name=t['output_file'])

    @app.route('/crawler/download_domains_by_filename')
    def crawler_download_by_filename():
        fn = request.args.get('filename', '')
        if not fn or not fn.endswith('_domains.txt') or '..' in fn:
            abort(400)
        fp = os.path.join(UPLOAD_FOLDER, fn)
        if not os.path.exists(fp):
            abort(404)
        return send_file(fp, as_attachment=True, download_name=fn)

    @app.route('/crawler/list_files')
    def crawler_list_files():
        files = []
        try:
            for f in os.listdir(UPLOAD_FOLDER):
                if f.endswith('_domains.txt'):
                    fp = os.path.join(UPLOAD_FOLDER, f)
                    st = os.stat(fp)
                    files.append({
                        'name': f,
                        'path': f,
                        'size': st.st_size,
                        'size_str': _fmt_size(st.st_size),
                        'mtime': st.st_mtime,
                    })
        except Exception as e:
            logger.warning(f"list files error: {e}")
        return jsonify(files)

    @app.route('/crawler/view/<filename>')
    def crawler_view(filename):
        if '..' in filename or not filename.endswith('_domains.txt'):
            abort(400)
        fp = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(fp):
            abort(404)
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                content = f.read()
            return Response(content, mimetype='text/plain')
        except Exception as e:
            return jsonify({'error': str(e)}), 500
