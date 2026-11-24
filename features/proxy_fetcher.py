"""
features/proxy_fetcher.py

Persistent, automated proxy fetcher & tester.

• Single on-disk store (SECRETS_DIR/proxy_store.json) survives restarts
• Optional background daemon re-fetches + re-tests on a schedule
• Tested results merged across cycles — latency, success/fail counts kept
• REST API for listing, filtering, paging, random, plain-text export
• UI-safe: 100-default page size, capped at 500, to prevent client overload
"""

import os
import re
import json
import time
import uuid
import random
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import request, jsonify, Response
from tasks import save_task, load_task
from config import SECRETS_DIR

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────
STORE_FILE = os.path.join(SECRETS_DIR, 'proxy_store.json')
TEST_URL = "https://httpbin.org/ip"
TEST_TIMEOUT = 5

# GeoIP (country lookup) — ip-api.com's free batch endpoint, no key needed.
# Free tier: ~45 req/min from the same source IP, up to 100 IPs per batch
# call, so this stays cheap even on a full fetch cycle.
GEOIP_BATCH_URL = "http://ip-api.com/batch"
GEOIP_BATCH_SIZE = 100
GEOIP_TIMEOUT = 10
GEOIP_BATCH_DELAY = 1.5  # seconds between batches, stays well under the rate limit
DEFAULT_INTERVAL = 900
MIN_INTERVAL = 60
MAX_INTERVAL = 86_400
DEFAULT_MAX_STORED = 10_000

MAX_NEW_PER_CYCLE = 500
MAX_RETEST_PER_CYCLE = 200
RETEST_AFTER_SEC = 1800          # re-test existing after 30 min
STALE_PRUNE_DAYS = 7             # drop never-succeeding entries after 7 days
HARD_PRUNE_DAYS = 30             # drop anything not tested in 30 days
TEST_CONCURRENCY = 30
API_DEFAULT_LIMIT = 100
API_MAX_LIMIT = 500

SOURCES = [
    ("HTTP (TheSpeedX)",
     "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
     "http"),
    ("SOCKS4 (TheSpeedX)",
     "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
     "socks4"),
    ("SOCKS5 (TheSpeedX)",
     "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
     "socks5"),
    ("HTTP (ProxyScrape)",
     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http"
     "&timeout=5000&country=all&ssl=all&anonymity=all", "http"),
    ("SOCKS4 (ProxyScrape)",
     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4"
     "&timeout=5000&country=all", "socks4"),
    ("SOCKS5 (ProxyScrape)",
     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5"
     "&timeout=5000&country=all", "socks5"),
]

PROXY_LINE_RE = re.compile(
    r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$'
)


# ── Global state ───────────────────────────────────────────────
_store_lock = threading.Lock()
_store_cache = None
_store_cache_mtime = 0

_cycle_lock = threading.Lock()

_daemon_lock = threading.Lock()
_daemon_thread = None
_daemon_stop = threading.Event()

_status_lock = threading.Lock()
_status = {
    'running': False,
    'cycle_type': None,          # 'daemon' | 'manual' | None
    'cycle_count': 0,
    'current_test': 0,
    'current_total': 0,
    'last_cycle_start': 0,
    'last_cycle_end': 0,
    'last_cycle_tested': 0,
    'last_cycle_found': 0,
    'next_run_at': 0,
    'last_error': None,
}


# ══════════════════════════════════════════════════════════════
# Persistent store
# ══════════════════════════════════════════════════════════════
def _empty_store():
    return {
        'version': 2,
        'proxies': {},
        'last_updated': 0,
        'pruned_at': 0,
    }


def _ensure_cache():
    """Populate _store_cache from disk if stale (called while holding lock)."""
    global _store_cache, _store_cache_mtime
    if not os.path.exists(STORE_FILE):
        if _store_cache is None:
            _store_cache = _empty_store()
        return
    try:
        mtime = os.path.getmtime(STORE_FILE)
        if _store_cache is not None and mtime == _store_cache_mtime:
            return
        with open(STORE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('version') == 2 \
                and isinstance(data.get('proxies'), dict):
            _store_cache = data
            _store_cache_mtime = mtime
            return
        logger.warning(f"Unrecognized store format — resetting")
    except Exception as e:
        logger.warning(f"store load error: {e}")
    if _store_cache is None:
        _store_cache = _empty_store()


def load_store_readonly():
    """Return a snapshot for API use — never mutate the returned dict."""
    with _store_lock:
        _ensure_cache()
        return {
            'version': _store_cache['version'],
            'proxies': dict(_store_cache['proxies']),
            'last_updated': _store_cache.get('last_updated', 0),
            'pruned_at': _store_cache.get('pruned_at', 0),
        }


def _get_writable_store():
    """Only the cycle may mutate. Returns the live cached object."""
    with _store_lock:
        _ensure_cache()
        return _store_cache


def _save_store_locked():
    global _store_cache_mtime
    with _store_lock:
        if _store_cache is None:
            return
        os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)
        tmp = STORE_FILE + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(_store_cache, f, separators=(',', ':'))
            os.replace(tmp, STORE_FILE)
            try:
                os.chmod(STORE_FILE, 0o600)
            except Exception:
                pass
            _store_cache_mtime = os.path.getmtime(STORE_FILE)
        except Exception as e:
            logger.error(f"store save error: {e}")


def _proxy_key(ip, port, ptype):
    return f"{ptype}://{ip}:{port}"


# ══════════════════════════════════════════════════════════════
# Fetch from sources
# ══════════════════════════════════════════════════════════════
def _parse_proxies(text, ptype):
    out = []
    for line in text.splitlines():
        line = line.split('#')[0].strip()
        if not line:
            continue
        m = PROXY_LINE_RE.match(line)
        if m:
            out.append((m.group(1), int(m.group(2)), ptype))
            continue
        parts = line.split()
        if len(parts) >= 2 and re.match(r'^\d{1,3}(?:\.\d{1,3}){3}$', parts[0]):
            try:
                out.append((parts[0], int(parts[1]), ptype))
            except ValueError:
                pass
    return out


def _fetch_sources():
    all_proxies = []
    for name, url, ptype in SOURCES:
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                parsed = _parse_proxies(resp.text, ptype)
                all_proxies.extend(parsed)
                logger.info(f"Fetched {len(parsed)} from {name}")
        except Exception as e:
            logger.warning(f"Error fetching {name}: {e}")
    seen = set()
    unique = []
    for ip, port, ptype in all_proxies:
        key = (ip, port, ptype)
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


# ══════════════════════════════════════════════════════════════
# Testing
# ══════════════════════════════════════════════════════════════
def _test_proxy(ip, port, ptype, timeout=TEST_TIMEOUT):
    url = f"{ptype}://{ip}:{port}"
    proxies = {'http': url, 'https': url}
    try:
        start = time.time()
        resp = requests.get(TEST_URL, proxies=proxies, timeout=timeout)
        latency = int((time.time() - start) * 1000)
        if resp.status_code == 200:
            return {
                'ip': ip, 'port': int(port), 'type': ptype,
                'latency_ms': latency, 'url': url,
            }
    except Exception:
        pass
    return None


def _test_batch(proxies_list, on_progress=None, should_cancel=None):
    """Returns (successes, failures). `on_progress(done, total)` fires per completion."""
    successes = []
    failures = []
    if not proxies_list:
        return successes, failures

    cancelled = False
    with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as ex:
        futures = {ex.submit(_test_proxy, ip, port, pt): (ip, port, pt)
                   for ip, port, pt in proxies_list}
        try:
            for i, fut in enumerate(as_completed(futures), 1):
                if cancelled:
                    continue
                ip, port, pt = futures[fut]
                try:
                    r = fut.result()
                    if r:
                        successes.append(r)
                    else:
                        failures.append((ip, port, pt))
                except Exception:
                    failures.append((ip, port, pt))

                if on_progress:
                    try:
                        on_progress(i, len(proxies_list))
                    except Exception:
                        pass

                if should_cancel and should_cancel():
                    cancelled = True
        finally:
            for f in futures:
                f.cancel()
    return successes, failures


# ══════════════════════════════════════════════════════════════
# GeoIP (country lookup)
# ══════════════════════════════════════════════════════════════
def _lookup_countries(ips):
    """
    Resolve country for a list of IPs via ip-api.com's free /batch
    endpoint. Never raises — a network hiccup or an unresolvable IP
    (private/reserved ranges, rate-limited) just means that IP is
    missing from the returned dict, and the caller falls back to
    whatever it already had on file (or leaves it unresolved to retry
    on a later cycle).
    """
    out = {}
    unique_ips = list(dict.fromkeys(ips))  # de-dupe, keep order
    for i in range(0, len(unique_ips), GEOIP_BATCH_SIZE):
        batch = unique_ips[i:i + GEOIP_BATCH_SIZE]
        payload = [{'query': ip, 'fields': 'status,country,countryCode,query'}
                   for ip in batch]
        try:
            resp = requests.post(GEOIP_BATCH_URL, json=payload,
                                  timeout=GEOIP_TIMEOUT)
            if resp.status_code == 200:
                for item in resp.json():
                    if item.get('status') == 'success' and item.get('query'):
                        out[item['query']] = {
                            'country': item.get('country'),
                            'country_code': item.get('countryCode'),
                        }
            else:
                logger.warning(f"GeoIP batch lookup HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"GeoIP batch lookup error: {e}")

        if i + GEOIP_BATCH_SIZE < len(unique_ips):
            time.sleep(GEOIP_BATCH_DELAY)
    return out


# ══════════════════════════════════════════════════════════════
# Store operations
# ══════════════════════════════════════════════════════════════
def _pick_to_test(store, raw_list):
    now = int(time.time())
    to_test = []
    seen = set()

    proxies = store['proxies']
    for ip, port, ptype in raw_list:
        key = _proxy_key(ip, port, ptype)
        if key in proxies or key in seen:
            continue
        seen.add(key)
        to_test.append((ip, port, ptype))
        if len(to_test) >= MAX_NEW_PER_CYCLE:
            break

    if MAX_RETEST_PER_CYCLE > 0:
        stale = [
            (k, v) for k, v in proxies.items()
            if now - v.get('tested_at', 0) > RETEST_AFTER_SEC
        ]
        stale.sort(key=lambda x: x[1].get('tested_at', 0))
        for _key, entry in stale[:MAX_RETEST_PER_CYCLE]:
            to_test.append((entry['ip'], entry['port'], entry['type']))

    return to_test


def _merge_results(store, successes, failures):
    now = int(time.time())
    proxies = store['proxies']

    # Only geolocate IPs we don't already have a country for — most
    # successes on any given cycle are re-tests of proxies we've already
    # resolved, so this keeps GeoIP calls limited to genuinely new entries
    # (and any older entries saved before this field existed, which
    # backfill the first time they're re-tested).
    need_geo_ips = []
    for r in successes:
        key = _proxy_key(r['ip'], r['port'], r['type'])
        if not proxies.get(key, {}).get('country'):
            need_geo_ips.append(r['ip'])
    geo = _lookup_countries(need_geo_ips) if need_geo_ips else {}

    for r in successes:
        key = _proxy_key(r['ip'], r['port'], r['type'])
        prev = proxies.get(key, {})
        geo_info = geo.get(r['ip'], {})
        proxies[key] = {
            'ip': r['ip'],
            'port': r['port'],
            'type': r['type'],
            'latency_ms': r['latency_ms'],
            'tested_at': now,
            'success_count': prev.get('success_count', 0) + 1,
            'fail_count': prev.get('fail_count', 0),
            'first_seen': prev.get('first_seen', now),
            # None (not 'Unknown') when unresolved, so a transient GeoIP
            # failure gets retried on the next cycle instead of sticking.
            'country': geo_info.get('country') or prev.get('country'),
            'country_code': geo_info.get('country_code') or prev.get('country_code'),
        }

    for ip, port, ptype in failures:
        key = _proxy_key(ip, port, ptype)
        entry = proxies.get(key)
        if not entry:
            continue
        entry['fail_count'] = entry.get('fail_count', 0) + 1
        entry['tested_at'] = now
        entry['last_failed_at'] = now
        # Drop entries that are failing much more than succeeding
        succ = entry.get('success_count', 0)
        fails = entry.get('fail_count', 0)
        if fails >= 3 and succ == 0:
            proxies.pop(key, None)
        elif fails > succ * 2 + 5:
            proxies.pop(key, None)


def _prune_store(store):
    now = int(time.time())
    soft = now - STALE_PRUNE_DAYS * 86400
    hard = now - HARD_PRUNE_DAYS * 86400

    before = len(store['proxies'])
    keep = {}
    for key, v in store['proxies'].items():
        tested = v.get('tested_at', 0)
        if tested < hard:
            continue
        if tested < soft and v.get('success_count', 0) == 0:
            continue
        keep[key] = v

    # Hard cap
    max_stored = _get_max_stored()
    if len(keep) > max_stored:
        items = sorted(keep.items(),
                       key=lambda x: x[1].get('latency_ms', 999999))
        keep = dict(items[:max_stored])

    store['proxies'] = keep
    store['pruned_at'] = now
    removed = before - len(keep)
    if removed > 0:
        logger.info(f"Pruned {removed} stale proxies")


def _get_max_stored():
    try:
        from .settings import get_effective_config
        return int(get_effective_config().get('proxy_max_results', DEFAULT_MAX_STORED))
    except Exception:
        return DEFAULT_MAX_STORED


# ══════════════════════════════════════════════════════════════
# Cycle
# ══════════════════════════════════════════════════════════════
def _set_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


def _run_cycle(manual_task_id=None, cycle_type='manual'):
    with _cycle_lock:
        started = int(time.time())
        _set_status(
            running=True, cycle_type=cycle_type,
            last_cycle_start=started, last_error=None,
            current_test=0, current_total=0,
        )
        with _status_lock:
            _status['cycle_count'] += 1

        def _update_task(**kw):
            if not manual_task_id:
                return
            t = load_task(manual_task_id)
            if not t:
                return
            for k, v in kw.items():
                t[k] = v
            save_task(manual_task_id, t)

        def _should_cancel():
            if manual_task_id:
                t = load_task(manual_task_id)
                if t and t.get('cancelled'):
                    return True
            if cycle_type == 'daemon' and _daemon_stop.is_set():
                return True
            return False

        try:
            _update_task(status='fetching', progress=0,
                         phase='Fetching source lists…')
            raw = _fetch_sources()
            logger.info(f"Cycle: fetched {len(raw)} raw proxies")

            if not raw:
                _set_status(running=False, last_cycle_end=int(time.time()),
                            last_error='No proxies fetched from sources')
                _update_task(status='error',
                             error_msg='No proxies fetched from sources')
                return

            _update_task(phase=f'Fetched {len(raw)} raw · selecting test set…',
                         progress=3)

            store = _get_writable_store()
            to_test = _pick_to_test(store, raw)
            logger.info(f"Cycle: testing {len(to_test)} proxies")

            if not to_test:
                store['last_updated'] = int(time.time())
                _save_store_locked()
                _set_status(
                    running=False,
                    last_cycle_end=int(time.time()),
                    last_cycle_tested=0, last_cycle_found=0,
                )
                _update_task(
                    status='done', progress=100,
                    phase='Nothing to test — store is fresh',
                    tested=0, total_proxies=0, working_proxies=0,
                )
                return

            _set_status(current_test=0, current_total=len(to_test))
            _update_task(
                status='testing',
                phase=f'Testing {len(to_test)} proxies…',
                total_proxies=len(to_test),
                tested=0, progress=5,
            )

            def on_progress(done, total):
                _set_status(current_test=done)
                pct = 5 + int(90 * done / total) if total else 5
                if manual_task_id and (done % 5 == 0 or done == total):
                    t = load_task(manual_task_id)
                    if t:
                        t['tested'] = done
                        t['progress'] = pct
                        save_task(manual_task_id, t)

            successes, failures = _test_batch(
                to_test, on_progress=on_progress, should_cancel=_should_cancel
            )
            logger.info(
                f"Cycle: {len(successes)} working / {len(failures)} failed"
            )

            _update_task(phase='Saving results…', progress=95)

            _merge_results(store, successes, failures)
            _prune_store(store)
            store['last_updated'] = int(time.time())
            _save_store_locked()

            _set_status(
                running=False,
                last_cycle_end=int(time.time()),
                last_cycle_tested=len(to_test),
                last_cycle_found=len(successes),
                current_test=0, current_total=0,
            )

            cancelled = _should_cancel()
            _update_task(
                status='cancelled' if cancelled else 'done',
                progress=100,
                working_proxies=len(successes),
                tested=len(to_test),
                phase=f'{len(successes)} working / {len(to_test)} tested',
            )

        except Exception as e:
            logger.exception("Proxy cycle error")
            _set_status(running=False, last_cycle_end=int(time.time()),
                        last_error=str(e))
            _update_task(status='error', error_msg=str(e))


# ══════════════════════════════════════════════════════════════
# Continuous daemon
# ══════════════════════════════════════════════════════════════
def _get_continuous_config():
    try:
        from .settings import get_effective_config
        cfg = get_effective_config()
        interval = int(cfg.get('proxy_continuous_interval', DEFAULT_INTERVAL))
        interval = max(MIN_INTERVAL, min(MAX_INTERVAL, interval))
        enabled = bool(cfg.get('proxy_continuous_enabled', False))
        return enabled, interval
    except Exception:
        return False, DEFAULT_INTERVAL


def _continuous_loop():
    logger.info("Proxy daemon started")
    while not _daemon_stop.is_set():
        _enabled, interval = _get_continuous_config()
        if not _enabled:
            logger.info("Proxy daemon: disabled in settings — stopping")
            break

        _set_status(next_run_at=int(time.time() + interval))
        try:
            _run_cycle(manual_task_id=None, cycle_type='daemon')
        except Exception:
            logger.exception("Daemon cycle error")

        # Sleep in 1-second ticks so stop is responsive
        remaining = interval
        while remaining > 0 and not _daemon_stop.is_set():
            step = min(1, remaining)
            if _daemon_stop.wait(timeout=step):
                break
            remaining -= step
        # Refresh interval from settings on every tick
        _enabled, interval = _get_continuous_config()
        if not _enabled:
            break

    _set_status(running=False, cycle_type=None, next_run_at=0)
    logger.info("Proxy daemon stopped")


def start_daemon():
    global _daemon_thread
    with _daemon_lock:
        if _daemon_thread and _daemon_thread.is_alive():
            if not _daemon_stop.is_set():
                return False
            _daemon_thread.join(timeout=15)
        _daemon_stop.clear()
        _daemon_thread = threading.Thread(
            target=_continuous_loop, daemon=True, name="proxy-daemon",
        )
        _daemon_thread.start()
        logger.info("Proxy daemon thread started")
        return True


def stop_daemon():
    _daemon_stop.set()
    logger.info("Proxy daemon stop requested")
    return True


def reconfigure():
    """Called by settings_post when proxy_* keys change."""
    enabled, _interval = _get_continuous_config()
    if enabled:
        start_daemon()
    else:
        stop_daemon()


def start_if_enabled():
    enabled, _interval = _get_continuous_config()
    if enabled:
        logger.info("Proxy daemon auto-start (enabled in settings)")
        threading.Timer(5.0, start_daemon).start()


# ══════════════════════════════════════════════════════════════
# Status / routes
# ══════════════════════════════════════════════════════════════
def _status_snapshot():
    with _status_lock:
        s = dict(_status)
    enabled, interval = _get_continuous_config()
    store = load_store_readonly()
    s['daemon_alive'] = bool(_daemon_thread and _daemon_thread.is_alive())
    s['continuous_enabled'] = enabled
    s['continuous_interval'] = interval
    s['total_stored'] = len(store['proxies'])
    s['store_last_updated'] = store.get('last_updated', 0)
    return s


def _filter_sort_page(store, args):
    """Shared filter/sort/page logic for API + listing."""
    proxies = list(store['proxies'].values())

    ptype = (args.get('type') or '').lower()
    if ptype in ('http', 'socks4', 'socks5'):
        proxies = [p for p in proxies if p['type'] == ptype]

    min_lat = args.get('min_latency', type=int)
    if min_lat is not None:
        proxies = [p for p in proxies if p.get('latency_ms', 0) >= min_lat]

    max_lat = args.get('max_latency', type=int)
    if max_lat is not None:
        proxies = [p for p in proxies
                   if p.get('latency_ms', 999_999) <= max_lat]

    q = (args.get('q') or '').strip().lower()
    if q:
        proxies = [p for p in proxies if q in p['ip'].lower()]

    country = (args.get('country') or '').strip().lower()
    if country:
        proxies = [
            p for p in proxies
            if country in (p.get('country') or '').lower()
            or country == (p.get('country_code') or '').lower()
        ]

    sort = (args.get('sort') or 'latency').lower()
    if sort == 'latency':
        proxies.sort(key=lambda p: p.get('latency_ms', 999_999))
    elif sort == 'recent':
        proxies.sort(key=lambda p: p.get('tested_at', 0), reverse=True)
    elif sort == 'ip':
        proxies.sort(key=lambda p: (p['ip'], p['port']))
    elif sort == 'type':
        proxies.sort(key=lambda p: (p['type'], p.get('latency_ms', 999_999)))
    elif sort == 'country':
        # Unresolved countries ('' sorts after real names) fall last.
        proxies.sort(key=lambda p: (p.get('country') or '￿',
                                     p.get('latency_ms', 999_999)))
    else:
        proxies.sort(key=lambda p: p.get('latency_ms', 999_999))

    total = len(proxies)
    try:
        limit = int(args.get('limit', API_DEFAULT_LIMIT))
    except (ValueError, TypeError):
        limit = API_DEFAULT_LIMIT
    try:
        offset = int(args.get('offset', 0))
    except (ValueError, TypeError):
        offset = 0
    limit = max(1, min(API_MAX_LIMIT, limit))
    offset = max(0, offset)

    page = proxies[offset:offset + limit]
    now = int(time.time())
    for p in page:
        p['age_sec'] = now - p.get('tested_at', 0)
        p['url'] = f"{p['type']}://{p['ip']}:{p['port']}"

    return {
        'total': total,
        'limit': limit,
        'offset': offset,
        'proxies': page,
    }


def register_routes(app):

    # ── Manual "Fetch now" (creates a task, streams via /proxy/stream) ──
    @app.route('/proxy/fetch', methods=['POST'])
    def proxy_fetch():
        task_id = str(uuid.uuid4())
        save_task(task_id, {
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'phase': 'Queued',
            'tested': 0, 'total_proxies': 0, 'working_proxies': 0,
            'type': 'proxy_cycle',
        })

        def _run():
            try:
                _run_cycle(manual_task_id=task_id, cycle_type='manual')
            except Exception as e:
                logger.exception(f"manual proxy cycle failed: {e}")
                t = load_task(task_id)
                if t:
                    t['status'] = 'error'
                    t['error_msg'] = str(e)
                    save_task(task_id, t)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'task_id': task_id})

    # ── Status (used by panel + proxy tab) ──
    @app.route('/proxy/status')
    def proxy_status():
        return jsonify(_status_snapshot())

    # ══════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════
    @app.route('/api/proxies')
    def api_proxies():
        store = load_store_readonly()
        return jsonify(_filter_sort_page(store, request.args))

    @app.route('/api/proxies/count')
    def api_proxies_count():
        store = load_store_readonly()
        by_type = {}
        for p in store['proxies'].values():
            by_type[p['type']] = by_type.get(p['type'], 0) + 1
        return jsonify({
            'total': len(store['proxies']),
            'by_type': by_type,
            'last_updated': store.get('last_updated', 0),
        })

    @app.route('/api/proxies/stats')
    def api_proxies_stats():
        store = load_store_readonly()
        proxies = list(store['proxies'].values())
        by_type = {}
        for p in proxies:
            by_type[p['type']] = by_type.get(p['type'], 0) + 1
        by_country = {}
        for p in proxies:
            c = p.get('country') or 'Unknown'
            by_country[c] = by_country.get(c, 0) + 1
        lats = sorted(p.get('latency_ms', 0) for p in proxies
                      if p.get('latency_ms'))
        def pct(q):
            if not lats:
                return 0
            i = int(len(lats) * q)
            return lats[min(i, len(lats) - 1)]
        return jsonify({
            'total': len(proxies),
            'by_type': by_type,
            'by_country': by_country,
            'latency': {
                'min': lats[0] if lats else 0,
                'p50': pct(0.5),
                'p95': pct(0.95),
                'max': lats[-1] if lats else 0,
            },
            'last_updated': store.get('last_updated', 0),
        })

    @app.route('/api/proxies/random')
    def api_proxies_random():
        store = load_store_readonly()
        proxies = list(store['proxies'].values())
        if not proxies:
            return jsonify({'error': 'No proxies available'}), 404
        p = dict(random.choice(proxies))
        p['url'] = f"{p['type']}://{p['ip']}:{p['port']}"
        return jsonify(p)

    @app.route('/api/proxies.txt')
    def api_proxies_txt():
        store = load_store_readonly()
        lines = [f"{p['type']}://{p['ip']}:{p['port']}"
                 for p in store['proxies'].values()]
        return Response('\n'.join(lines), mimetype='text/plain')

    # ── Convenience: probe a single proxy ──
    @app.route('/proxy/test_single', methods=['POST'])
    def proxy_test_single():
        data = request.get_json(silent=True) or {}
        ip = data.get('ip')
        port = data.get('port')
        ptype = data.get('type')
        if not (ip and port and ptype):
            return jsonify({'error': 'Missing ip/port/type'}), 400
        r = _test_proxy(ip, int(port), ptype)
        if r:
            return jsonify({'success': True, 'latency_ms': r['latency_ms']})
        return jsonify({'success': False})
