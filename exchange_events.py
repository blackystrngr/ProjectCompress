# features/exchange_events.py
"""
Exchange event poller — Binance + Bybit announcement feeds.

Why: official Telegram channels only post a subset of campaigns. The
exchanges publish every campaign on their own announcement pages, so
this polls those directly and forwards new, matching events to the
same target chat the Telegram listener uses (via telegram_listener).

Sources
  Bybit    GET https://api.bybit.com/v5/announcements/index?locale=en-US
           (official public endpoint, no key — documented by Bybit)
  Binance  GET https://www.binance.com/bapi/composite/v1/public/cms/article/list/query
           (the website's own CMS feed. NOT an official API: it can change
            or block datacenter IPs. Falls back to scraping the list page.)
           Catalogs: 93 Latest Activities · 128 Crypto Airdrop · 49 Latest News

Behaviour
  * First run per source only records what already exists (no flood).
  * Seen IDs persist across restarts (JSON in the secrets dir).
  * Every item goes through the same rules → optional-LLM pipeline as the
    Telegram listener (features.opportunity_pipeline); the exchange category
    ("Latest Activities" ...) is passed in as a weak "this is a campaign" hint.
    Binance bodies are fetched and judged again, because capital
    requirements ("deposit 100 USDT to qualify") live in the body.
  * Skips events already forwarded from a Telegram channel (title overlap).
  * Backs off per source on errors; never raises into the caller.

Probe from your server (prints exactly what each source returns):
  python3 features/exchange_events.py            # list items
  python3 features/exchange_events.py --detail   # + first item's details
"""

import os
import re
import sys
import json
import time
import random
import logging
import hashlib
import threading
from collections import OrderedDict, deque
from datetime import datetime, timezone

import requests

try:
    from config import PROXY_DICT
except Exception:
    PROXY_DICT = None
try:
    from config import SECRETS_DIR
except Exception:
    SECRETS_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)

# ============================================================
# Settings (constants — edit here)
# ============================================================
POLL_INTERVAL_SEC = 120
MAX_BACKOFF_SEC = 900
HTTP_TIMEOUT = 15
SEEN_MAX = 3000
BASELINE_ON_FIRST_RUN = True

BINANCE_CATALOGS = {93: 'Latest Activities', 128: 'Crypto Airdrop', 49: 'Latest Binance News'}
BINANCE_LIST_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/"
                    "article/list/query?type=1&catalogId={cid}&pageNo=1&pageSize=20")
BINANCE_DETAIL_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/"
                      "article/detail/query?articleCode={code}")
BINANCE_PAGE_URL = "https://www.binance.com/en/support/announcement/detail/{code}"
BINANCE_LIST_PAGE = "https://www.binance.com/en/support/announcement/list/{cid}"

BYBIT_HOSTS = ("https://api.bybit.com", "https://api.bytick.com")
BYBIT_PATH = "/v5/announcements/index?locale=en-US&limit=30"

ACTIVITY_HINTS = ('activit', 'airdrop', 'campaign', 'promotion', 'reward',
                  'competition', 'giveaway', 'launchpool', 'megadrop')

STATE_FILE = os.path.join(SECRETS_DIR, 'exchange_events_state.json')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ============================================================
# State
# ============================================================
_lock = threading.RLock()
_seen = OrderedDict()                 # item id -> ts
_baselined = {'binance': False, 'bybit': False}
_recent_titles = deque(maxlen=300)    # (ts, frozenset(tokens))
_recent_log = deque(maxlen=80)        # decisions, newest last
_health = {
    'binance': {'last_ok': None, 'last_error': None, 'fetched': 0, 'forwarded': 0, 'fails': 0},
    'bybit':   {'last_ok': None, 'last_error': None, 'fetched': 0, 'forwarded': 0, 'fails': 0},
}
_thread = None
_cfg_provider = None
_send = None
_poll_now = threading.Event()


# ============================================================
# Persistence
# ============================================================
def _load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        with _lock:
            _seen.clear()
            for k, v in (d.get('seen') or {}).items():
                _seen[k] = v
            for k in _baselined:
                _baselined[k] = bool((d.get('baselined') or {}).get(k))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"[exchange] state load failed: {e}")


def _save_state():
    try:
        with _lock:
            while len(_seen) > SEEN_MAX:
                _seen.popitem(last=False)
            payload = {'seen': dict(_seen), 'baselined': dict(_baselined)}
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.warning(f"[exchange] state save failed: {e}")


# ============================================================
# Title-overlap dedupe (shared with the Telegram listener)
# ============================================================
_WORD = re.compile(r"[a-z0-9$%.+]+")


def _tokens(text):
    return frozenset(w for w in _WORD.findall((text or '').lower()) if len(w) > 2)


def note_forwarded(text):
    """Record an announcement that was already forwarded (any source)."""
    first = next((ln for ln in (text or '').splitlines() if ln.strip()), '')[:200]
    t = _tokens(first)
    if len(t) >= 4:
        with _lock:
            _recent_titles.append((time.time(), t))


def covered(text):
    """True if (nearly) this headline was already forwarded in the last 12h."""
    first = next((ln for ln in (text or '').splitlines() if ln.strip()), '')[:200]
    t = _tokens(first)
    if len(t) < 4:
        return False
    cutoff = time.time() - 12 * 3600
    with _lock:
        for ts, s in _recent_titles:
            if ts < cutoff:
                continue
            small, big = (t, s) if len(t) <= len(s) else (s, t)
            if len(small) >= 4 and len(small & big) / len(small) >= 0.8:
                return True
    return False


# ============================================================
# HTTP
# ============================================================
def _session():
    s = requests.Session()
    if PROXY_DICT:
        s.proxies = PROXY_DICT
    s.headers.update({
        'User-Agent': UA,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    return s


def _get(session, url, **kw):
    r = session.get(url, timeout=HTTP_TIMEOUT, **kw)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from {url.split('?')[0]}")
    return r


def _ts_to_str(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return ''


# ============================================================
# Bybit
# ============================================================
def fetch_bybit(session=None):
    session = session or _session()
    last = None
    for host in BYBIT_HOSTS:
        try:
            data = _get(session, host + BYBIT_PATH).json()
            if data.get('retCode') != 0:
                raise RuntimeError(f"retCode={data.get('retCode')} {data.get('retMsg')}")
            items = []
            for a in (data.get('result') or {}).get('list') or []:
                url = a.get('url') or ''
                if not url:
                    continue
                t = a.get('type') or {}
                cat = ' · '.join(x for x in [t.get('title')] + list(a.get('tags') or []) if x)
                items.append({
                    'id': 'bybit:' + hashlib.sha1(url.encode()).hexdigest()[:16],
                    'source': 'Bybit',
                    'title': (a.get('title') or '').strip(),
                    'summary': (a.get('description') or '').strip(),
                    'category': cat,
                    'type_key': t.get('key') or '',
                    'url': url,
                    'published': a.get('publishTime') or a.get('dateTimestamp') or 0,
                })
            return items
        except Exception as e:
            last = e
            logger.debug(f"[exchange] bybit via {host} failed: {e}")
    raise RuntimeError(f"Bybit unreachable: {str(last)[:160]}")


# ============================================================
# Binance
# ============================================================
def _binance_articles_from_json(data):
    """Defensive: the CMS payload nests articles under catalogs."""
    d = (data or {}).get('data') or {}
    out = []
    for cat in d.get('catalogs') or []:
        out.extend(cat.get('articles') or [])
    out.extend(d.get('articles') or [])
    return out


def _binance_from_list_page(session, cid):
    """Fallback: pull (code, title) pairs out of the list page's embedded JSON."""
    html = _get(session, BINANCE_LIST_PAGE.format(cid=cid),
                headers={'Accept': 'text/html'}).text
    pairs = re.findall(r'"code"\s*:\s*"([0-9a-f]{32})"\s*,\s*"title"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    arts, seen = [], set()
    for code, title in pairs:
        if code in seen:
            continue
        seen.add(code)
        try:
            title = json.loads(f'"{title}"')
        except Exception:
            pass
        arts.append({'code': code, 'title': title, 'releaseDate': 0})
    return arts


def fetch_binance(session=None):
    session = session or _session()
    items, errors = [], []
    for cid, cname in BINANCE_CATALOGS.items():
        arts = []
        try:
            arts = _binance_articles_from_json(
                _get(session, BINANCE_LIST_URL.format(cid=cid)).json())
        except Exception as e:
            errors.append(f"{cname}: {e}")
        if not arts:
            try:
                arts = _binance_from_list_page(session, cid)
            except Exception as e:
                errors.append(f"{cname} (page): {e}")
        for a in arts:
            code = a.get('code')
            if not code:
                continue
            items.append({
                'id': 'binance:' + code,
                'source': 'Binance',
                'title': (a.get('title') or '').strip(),
                'summary': '',
                'category': cname,
                'type_key': str(cid),
                'url': BINANCE_PAGE_URL.format(code=code),
                'published': a.get('releaseDate') or 0,
                'code': code,
            })
        time.sleep(0.4)
    if not items:
        raise RuntimeError("Binance returned nothing — " + ('; '.join(e[:110] for e in errors[:3]) or 'empty'))
    seen, uniq = set(), []
    for it in items:
        if it['id'] not in seen:
            seen.add(it['id'])
            uniq.append(it)
    return uniq


def _collect_text(node, out):
    if isinstance(node, dict):
        if isinstance(node.get('text'), str):
            out.append(node['text'])
        for v in node.values():
            if isinstance(v, (dict, list)):
                _collect_text(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_text(v, out)


def binance_detail(session, code):
    """Best-effort plain-text body of a Binance announcement."""
    try:
        d = _get(session, BINANCE_DETAIL_URL.format(code=code)).json().get('data') or {}
        body = d.get('body')
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                return re.sub(r'<[^>]+>', ' ', body)
        parts = []
        _collect_text(body, parts)
        text = ' '.join(p.strip() for p in parts if p.strip())
        if text:
            return text
    except Exception as e:
        logger.debug(f"[exchange] binance detail api failed: {e}")
    try:
        html = _get(session, BINANCE_PAGE_URL.format(code=code),
                    headers={'Accept': 'text/html'}).text
        if BeautifulSoup:
            soup = BeautifulSoup(html, 'html.parser')
            m = (soup.find('meta', attrs={'name': 'description'})
                 or soup.find('meta', attrs={'property': 'og:description'}))
            if m and m.get('content'):
                return m['content']
    except Exception as e:
        logger.debug(f"[exchange] binance detail page failed: {e}")
    return ''


# ============================================================
# Decision + message
# ============================================================
def _compile(p):
    if not p:
        return None
    try:
        return re.compile(p, re.IGNORECASE)
    except re.error:
        return None


def _clip(s, n):
    s = re.sub(r'\s+', ' ', s or '').strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def _plain_alert(prefix, item, summary):
    lines = [f"{prefix} · {item['source']}", '', _clip(item['title'], 300)]
    meta = ' · '.join(x for x in (item.get('category'), _ts_to_str(item.get('published'))) if x)
    if meta:
        lines.append(f"🏷 {_clip(meta, 200)}")
    summary = _clip(summary, 800)
    if summary and summary.lower() not in item['title'].lower():
        lines += ['', summary]
    lines += ['', f"🔗 {item['url']}"]
    return '\n'.join(lines)[:3800]


def _log_decision(item, decision, reason, analysis=None):
    row = {
        'time': time.time(), 'source': item['source'], 'title': item['title'][:160],
        'category': item.get('category', ''), 'decision': decision, 'reason': reason,
        'url': item['url'],
    }
    if analysis:
        row.update(score=analysis.get('score'), cost=analysis.get('cost_tier'))
    _recent_log.append(row)


def _evaluate(item, cfg, include_re, exclude_re, body='', allow_llm=True):
    """Run the shared rules→LLM pipeline on one exchange item."""
    from . import opportunity_pipeline as pipeline
    text = f"{item['title']}\n{body or item.get('summary', '')}".strip()
    return pipeline.evaluate(text, cfg, include_re, exclude_re,
                             context_hint=item.get('category', ''), allow_llm=allow_llm)


# ============================================================
# Poll cycle
# ============================================================
def poll_once(dry_run=False):
    """One pass over both sources. Returns a list of decision dicts."""
    from .crypto_opportunity_detector import format_alert_html
    cfg = dict(_cfg_provider() if _cfg_provider else {})
    include_re = _compile(cfg.get('include'))
    exclude_re = _compile(cfg.get('exclude'))
    prefix = cfg.get('prefix') or '🧭 Crypto Opportunity'
    session = _session()
    results = []

    for name, fetch in (('bybit', fetch_bybit), ('binance', fetch_binance)):
        h = _health[name]
        try:
            items = fetch(session)
        except Exception as e:
            h['last_error'] = f"{type(e).__name__}: {e}"
            h['fails'] += 1
            logger.warning(f"[exchange] {name} fetch failed ({h['fails']}x): {e}")
            continue
        h['fails'] = 0
        h['last_ok'] = time.time()
        h['last_error'] = None
        h['fetched'] = len(items)

        if BASELINE_ON_FIRST_RUN and not _baselined[name] and not dry_run:
            with _lock:
                for it in items:
                    _seen[it['id']] = time.time()
                _baselined[name] = True
            _save_state()
            logger.info(f"[exchange] {name}: baseline recorded ({len(items)} existing items, none forwarded)")
            continue

        new = [it for it in items if it['id'] not in _seen]
        new.sort(key=lambda i: i.get('published') or 0)
        for it in new:
            summary = it.get('summary', '')
            d = _evaluate(it, cfg, include_re, exclude_re, allow_llm=not dry_run)
            # Binance list gives titles only: fetch the body, then judge again
            # (the body is where "deposit 100 USDT to qualify" lives)
            if (it['source'] == 'Binance' and it.get('code') and not dry_run
                    and (d['forward'] or (d.get('analysis') or {}).get('needs_review'))):
                summary = binance_detail(session, it['code']) or summary
                d = _evaluate(it, cfg, include_re, exclude_re, body=summary)
            decision = 'forward' if d['forward'] else 'skip'
            reason = d['reason']
            analysis = d.get('analysis')

            if decision == 'forward' and covered(it['title']):
                decision, reason = 'skip', 'already forwarded from a Telegram channel'

            if decision == 'forward' and not dry_run:
                if not (_send and cfg.get('enabled')):
                    results.append({**it, 'decision': 'pending', 'reason': 'listener-not-ready'})
                    break                       # keep unseen, retry next cycle
                if analysis is not None:
                    a2 = dict(analysis)
                    if not a2.get('reward') and (a2.get('llm') or {}).get('reward'):
                        a2['reward'] = a2['llm']['reward']
                    body = it['title'] + ('\n\n' + _clip(summary, 1500) if summary else '')
                    text = format_alert_html(body, a2,
                                             f"{it['source']} · {it.get('category') or 'announcement'}",
                                             it['url'], prefix=prefix)
                    pm = 'html'
                else:
                    text, pm = _plain_alert(prefix, it, summary), None
                ok = False
                try:
                    ok = bool(_send(text, pm))
                except Exception as e:
                    logger.warning(f"[exchange] send failed: {e}")
                if not ok:
                    results.append({**it, 'decision': 'pending', 'reason': 'send-failed'})
                    break
                note_forwarded(it['title'])
                h['forwarded'] += 1
                logger.info(f"[exchange] forwarded {it['source']}: {it['title'][:80]}")
            if not dry_run:
                with _lock:
                    _seen[it['id']] = time.time()
                _log_decision(it, decision, reason, analysis)
            results.append({**it, 'decision': decision, 'reason': reason})
        if not dry_run:
            _save_state()
    return results


def _loop():
    backoff = 0
    while True:
        try:
            cfg = _cfg_provider() if _cfg_provider else {}
            if cfg.get('enabled') and cfg.get('exchange', True):
                poll_once()
                worst = max(h['fails'] for h in _health.values())
                backoff = min(MAX_BACKOFF_SEC, POLL_INTERVAL_SEC * (2 ** min(worst, 4))) if worst else 0
        except Exception:
            logger.exception("[exchange] poll loop error")
        wait = (backoff or POLL_INTERVAL_SEC) + random.uniform(0, 10)
        _poll_now.wait(timeout=wait)
        _poll_now.clear()


def start(cfg_provider, send):
    """Idempotent. cfg_provider() → listener cfg dict; send(text) → bool."""
    global _thread, _cfg_provider, _send
    _cfg_provider, _send = cfg_provider, send
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _load_state()
        _thread = threading.Thread(target=_loop, daemon=True, name='exchange-events')
        _thread.start()
        logger.info(f"[exchange] poller started (every {POLL_INTERVAL_SEC}s: Binance + Bybit)")


def poll_now():
    _poll_now.set()


def get_status():
    with _lock:
        return {
            'running': _thread is not None and _thread.is_alive(),
            'interval_sec': POLL_INTERVAL_SEC,
            'baselined': dict(_baselined),
            'seen_count': len(_seen),
            'sources': {k: dict(v) for k, v in _health.items()},
            'recent': list(_recent_log)[-20:],
        }


# ============================================================
# CLI probe
# ============================================================
def _probe():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    want_detail = '--detail' in sys.argv
    s = _session()
    for name, fn in (('Bybit', fetch_bybit), ('Binance', fetch_binance)):
        print(f"\n=== {name} ===")
        try:
            items = fn(s)
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")
            continue
        print(f"{len(items)} item(s)")
        for it in items[:8]:
            print(f" - [{it['category'] or '?'}] {it['title'][:90]}\n   {it['url']}")
        if want_detail and items:
            it = items[0]
            if name == 'Binance' and it.get('code'):
                print("\n   DETAIL:", _clip(binance_detail(s, it['code']), 500) or '(empty)')
            else:
                print("\n   DETAIL:", _clip(it.get('summary'), 500) or '(empty)')


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _probe()
