# features/exchange_events.py
"""
Exchange event poller — Binance + Bybit announcement feeds.

Why: official Telegram channels only post a subset of campaigns. The
exchanges publish every campaign on their own announcement pages, so
this polls those directly and forwards new, matching events to the
same target chat the Telegram listener uses (via telegram_listener).

Sources
  Binance Square  public web pages (Binance has NO official API for *reading* Square;
           its official Square API only publishes). Pages read, all anonymous HTML:
             · verified official accounts  /square/profile/binance_announcement,
               binance_square_official, binance_security (+ accounts you add)
             · hashtag pages               /square/hashtag/<tag>   (Square's topic search)
             · the Discover feed           /square
           Parsed from the page markup — Binance can change it or serve a bot
           challenge to datacenter IPs. Falls back to a headless browser when
           Playwright is installed. Community posts get stricter filtering.
  Bybit ByX  public feed  https://www.bybit.com/en/social/  (Bybit's community platform).
           No official read API either; posts have no permalinks in the markup, so they
           are identified by author + content and the alert links to the author's profile.
           Official accounts (e.g. BybitX, "official" avatar) are trusted; the rest is
           community content with the same stricter filtering as Binance Square.
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
  python3 features/exchange_events.py            # Bybit + Binance + Square
  python3 features/exchange_events.py --square   # Square only (≈40 s: polite delays)
  (the same check is the "Check feeds" button / GET /telegram/listener/exchange?probe=1)
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
from urllib.parse import quote, unquote
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
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger(__name__)

# ============================================================
# Settings (constants — edit here)
# ============================================================
POLL_INTERVAL_SEC = 120
MAX_BACKOFF_SEC = 900
HTTP_TIMEOUT = 15
SEEN_MAX = 20000
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

# ---- Binance Square ----
SQUARE_BASE = "https://www.binance.com/en/square"
SQUARE_POLL_INTERVAL_SEC = 300                 # Square is polled less often than the feeds
SQUARE_OFFICIAL_HANDLES = ("binance_announcement", "binance_square_official", "binance_security")
SQUARE_DEFAULT_TAGS = ("airdrop", "giveaway", "LearnAndEarn", "RedPacket", "WordOfTheDay", "CryptoBox",
                       "FreeUSDT", "Testnet", "CreatorPad", "BinanceAlpha", "BinanceWallet", "Booster",
                       "Megadrop", "TGE")
SQUARE_COMMUNITY_SCORE_BONUS = 10              # community posts need min_score + this
SQUARE_MAX_FORWARD_PER_CYCLE = 5               # flood guard
SQUARE_AUTHOR_MAX_PER_HOUR = 2                 # one author can't spam the chat
SQUARE_LLM_MAX_PER_CYCLE = 5                   # cap LLM spend on borderline community posts
SQUARE_PAGE_DELAY = (0.8, 2.0)                 # polite random pause between page requests
SQUARE_BROWSER_FALLBACK = True                 # use Playwright when plain HTTP is challenged
SQUARE_BROWSER_MAX_PAGES = 4                   # per cycle

# ---- Bybit ByX (community feed) ----
BYX_BASE = "https://www.bybit.com/en/social"
BYX_OFFICIAL_IDS = ("kxobMj",)                 # BybitX (official account)
UGC_TYPES = ("square_official", "square_community", "byx_official", "byx_community")

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
_baselined = {'binance': False, 'bybit': False, 'square': False, 'byx': False}
_square_last = 0.0                    # last Square poll
_byx_last = 0.0                       # last ByX poll
_author_hits = {}                     # handle -> deque of forward timestamps
_recent_titles = deque(maxlen=300)    # (ts, frozenset(tokens))
_recent_log = deque(maxlen=80)        # decisions, newest last
_health = {
    'binance': {'last_ok': None, 'last_error': None, 'fetched': 0, 'forwarded': 0, 'fails': 0},
    'bybit':   {'last_ok': None, 'last_error': None, 'fetched': 0, 'forwarded': 0, 'fails': 0},
    'square':  {'last_ok': None, 'last_error': None, 'fetched': 0, 'forwarded': 0, 'fails': 0,
                'pages_ok': 0, 'pages_failed': 0, 'page_errors': [], 'via_browser': 0},
    'byx':     {'last_ok': None, 'last_error': None, 'fetched': 0, 'forwarded': 0, 'fails': 0,
                'pages_ok': 0, 'pages_failed': 0, 'page_errors': [], 'via_browser': 0},
}
_thread = None
_cfg_provider = None
_send = None
_poll_now = threading.Event()
_force_square = False


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
# Binance Square  (public pages — there is no official read API)
# ============================================================
_POST_HREF = re.compile(r"/square/post/(\d{6,})")
_PROFILE_HREF = re.compile(r"/square/profile/([^/?#\"']+)")
_SQUARE_NOISE = re.compile(r"\{(?:future|spot|alpha|margin)\}\([A-Z0-9_]+\)")


def _square_clean(text):
    text = _SQUARE_NOISE.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _square_title(text, limit=110):
    first = re.split(r"(?<=[.!?])\s|\n", text, 1)[0]
    if len(first) <= limit:
        return first
    cut = first[:limit].rsplit(" ", 1)[0]
    return (cut or first[:limit]).rstrip(",;:-") + "…"


def parse_square_html(html, label="", default_handle=None, official=()):
    """Extract posts from a Square hashtag / profile / discover page.

    Layout-agnostic on purpose: walks every <a> in document order, remembers the
    last profile link as the author, and treats each /square/post/<id> link with
    real text as one post. → list of item dicts (see fetch_square)."""
    if not BeautifulSoup:
        raise RuntimeError("beautifulsoup4 is required for Binance Square (pip install beautifulsoup4)")
    soup = BeautifulSoup(html or "", "html.parser")
    official = {h.lower() for h in official}
    author = (default_handle, "") if default_handle else (None, "")
    posts, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        pm = _PROFILE_HREF.search(href)
        if pm:
            name = a.get_text(" ", strip=True)
            author = (unquote(pm.group(1)), name or (author[1] if author[0] == unquote(pm.group(1)) else ""))
            continue
        qm = _POST_HREF.search(href)
        if not qm or qm.group(1) in seen:
            continue
        text = _square_clean(a.get_text(" ", strip=True))
        if len(text) < 20:
            continue
        pid = qm.group(1)
        seen.add(pid)
        handle = (author[0] or "unknown")
        is_official = handle.lower() in official
        posts.append({
            "id": f"square:{pid}", "source": "Binance Square",
            "title": _square_title(text), "summary": text[:1800],
            "category": f"{'Official' if is_official else 'Community'} · {label}".strip(" ·"),
            "type_key": "square_official" if is_official else "square_community",
            "url": f"https://www.binance.com/en/square/post/{pid}",
            "published": 0, "handle": handle, "author": author[1] or handle,
            "official": is_official, "square_page": label,
        })
    return posts


class _SquareBrowser:
    """Lazy headless-Chromium fetcher, used only when plain HTTP is challenged."""

    def __init__(self):
        self.pw = self.browser = self.ctx = None
        self.pages = 0

    def available(self):
        return SQUARE_BROWSER_FALLBACK and PLAYWRIGHT_AVAILABLE and self.pages < SQUARE_BROWSER_MAX_PAGES

    def get(self, url):
        if self.browser is None:
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            self.ctx = self.browser.new_context(user_agent=UA, locale="en-US",
                                                viewport={"width": 1280, "height": 900})
        self.pages += 1
        page = self.ctx.new_page()
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            return page.content()
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self):
        for obj, meth in ((self.ctx, "close"), (self.browser, "close"), (self.pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass


_SQUARE_MARKERS = ("/square/post/", "/square/profile/")
_BYX_MARKERS = ("/social/user/",)


def _blocked(resp_text, status, markers=_SQUARE_MARKERS):
    full = (resp_text or "").lower()
    low = full[:6000]
    return (status in (202, 403, 405, 429, 451)
            or "awswaf" in low or "aws-waf" in low or "captcha" in low[:3000]
            or not any(m in full for m in markers))


def _square_html(session, url, browser, health, markers=_SQUARE_MARKERS):
    """Plain HTTP first; headless browser when challenged / empty."""
    err = ""
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, headers={"Accept": "text/html,application/xhtml+xml"})
        if r.status_code == 200 and not _blocked(r.text, r.status_code, markers):
            return r.text
        err = f"HTTP {r.status_code}{' (bot challenge)' if _blocked(r.text, r.status_code, markers) else ''}"
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:80]}"
    if browser is not None and browser.available():
        html = browser.get(url)
        if not _blocked(html, 200, markers):
            health["via_browser"] += 1
            return html
        err += " + browser also challenged"
    raise RuntimeError(f"{err} for {re.sub(r'^https://www[.](?:binance|bybit)[.]com', '', url)}")


def square_pages(cfg):
    """→ list of (label, url, default_handle) for this config."""
    official = [h for h in SQUARE_OFFICIAL_HANDLES]
    for h in cfg.get("square_accounts") or []:
        if h and h.lower() not in {x.lower() for x in official}:
            official.append(h)
    pages = [(f"@{h}", f"{SQUARE_BASE}/profile/{quote(h)}", h) for h in official]
    if cfg.get("square_community", True):
        pages.append(("discover", SQUARE_BASE, None))
        for tag in (cfg.get("square_tags") or SQUARE_DEFAULT_TAGS):
            tag = str(tag).strip().lstrip("#").replace(" ", "")
            if tag:
                pages.append((f"#{tag}", f"{SQUARE_BASE}/hashtag/{quote(tag)}", None))
    return pages, official


def fetch_square(session=None, cfg=None):
    """Read Square pages and return unique post items (newest id first).
    Individual page failures are tolerated; raises only if *everything* failed."""
    cfg = cfg or {}
    session = session or _session()
    pages, official = square_pages(cfg)
    health = _health["square"]
    health.update(pages_ok=0, pages_failed=0, page_errors=[], via_browser=0)
    items, browser = {}, _SquareBrowser()
    try:
        for i, (label, url, handle) in enumerate(pages):
            try:
                html = _square_html(session, url, browser, health)
                for p in parse_square_html(html, label, handle, official):
                    cur = items.get(p["id"])
                    if cur is None or (p["official"] and not cur["official"]):
                        items[p["id"]] = p
                health["pages_ok"] += 1
            except Exception as e:
                health["pages_failed"] += 1
                if len(health["page_errors"]) < 4:
                    health["page_errors"].append(str(e)[:140])
            if i < len(pages) - 1:
                time.sleep(random.uniform(*SQUARE_PAGE_DELAY))
    finally:
        browser.close()
    if not items:
        raise RuntimeError("Binance Square returned nothing — " + ("; ".join(health["page_errors"][:2]) or "no posts parsed"))
    return sorted(items.values(), key=lambda it: int(it["id"].split(":")[1]), reverse=True)


# ============================================================
# Bybit ByX  (public community feed — no official read API)
# ============================================================
_BYX_USER = re.compile(r"/social/user/([A-Za-z0-9_-]+)")
_BYX_TIME = re.compile(r"^\d{1,2}:\d{2}\b")
_BYX_MOOD = re.compile(r"(?:bullish|bearish)\s*$", re.IGNORECASE)
_BYX_UI = {"like", "comment", "repost", "share", "follow", "following"}
_BYX_CHIP = re.compile(r"^[A-Z0-9]{2,15}(?:USDT|USDC|USD|PERP)$")


def parse_byx_html(html, label="feed", official_ids=()):
    """Extract posts from a ByX feed / profile page.

    Layout-agnostic: linearises the page into (profile-link | text) tokens; every
    /social/user/<id> link opens a post block whose text runs to the next link.
    First line = author, first time-like line = timestamp, the rest = body (UI
    counters, trade-pair chips and repeated hashtag chips are dropped)."""
    if not BeautifulSoup:
        raise RuntimeError("beautifulsoup4 is required for Bybit ByX (pip install beautifulsoup4)")
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    official_ids = {i for i in official_ids}
    blocks, cur = [], None
    for node in soup.descendants:
        name = getattr(node, "name", None)
        if name == "a" and node.get("href"):
            m = _BYX_USER.search(node["href"])
            if m:
                img = node.find("img")
                avatar = (img.get("src") or "") if img else ""
                if cur is not None and not cur["lines"]:
                    cur["uid"] = m.group(1)                      # avatar link then name link: same block
                    cur["avatar"] = cur["avatar"] or avatar
                else:
                    cur = {"uid": m.group(1), "avatar": avatar, "lines": []}
                    blocks.append(cur)
            continue
        if name is None and cur is not None:
            txt = re.sub(r"\s+", " ", str(node)).strip()
            if txt:
                cur["lines"].append(txt)
    posts, seen = [], set()
    for b in blocks:
        lines = b["lines"]
        name_ = lines[0] if lines and not _BYX_TIME.match(lines[0]) else ""
        t_idx = next((i for i, l in enumerate(lines) if _BYX_TIME.match(l)), None)
        posted = _BYX_MOOD.sub("", lines[t_idx]).strip() if t_idx is not None else ""
        body_lines, seen_lines = [], set()
        for l in lines[(t_idx + 1 if t_idx is not None else (1 if name_ else 0)):]:
            if l.lower() in _BYX_UI or re.fullmatch(r"[\d.,kKmM]+", l) or _BYX_CHIP.match(l):
                continue
            if l.lower() in ("bullish", "bearish") or l in seen_lines:
                continue
            seen_lines.add(l)
            body_lines.append(l)
        body = _square_clean(" ".join(body_lines))
        if len(body) < 20:
            continue
        uid = b["uid"]
        pid = hashlib.sha1(f"{uid}|{re.sub(r'[^a-z0-9]+', ' ', body.lower())[:400]}".encode()).hexdigest()[:16]
        if pid in seen:
            continue
        seen.add(pid)
        is_official = uid in official_ids or "official" in (b["avatar"] or "").lower()
        posts.append({
            "id": f"byx:{pid}", "source": "Bybit ByX",
            "title": _square_title(body), "summary": body[:1800],
            "category": f"{'Official' if is_official else 'Community'} · {label}".strip(" ·"),
            "type_key": "byx_official" if is_official else "byx_community",
            "url": f"{BYX_BASE}/user/{uid}", "published": 0, "posted": posted,
            "handle": uid, "author": name_ or uid, "official": is_official, "square_page": label,
        })
    return posts


def byx_pages(cfg):
    ids = list(BYX_OFFICIAL_IDS)
    for u in cfg.get("byx_accounts") or []:
        if u and u not in ids:
            ids.append(u)
    pages = [(f"@{u}", f"{BYX_BASE}/user/{quote(u)}", u) for u in ids]
    if cfg.get("square_community", True):
        pages.insert(0, ("feed", BYX_BASE + "/", None))
    return pages, ids


def fetch_byx(session=None, cfg=None):
    """Read the ByX feed + official/followed profiles → unique post items. Tolerates single-page failures."""
    cfg = cfg or {}
    session = session or _session()
    pages, official = byx_pages(cfg)
    health = _health["byx"]
    health.update(pages_ok=0, pages_failed=0, page_errors=[], via_browser=0)
    items, browser = {}, _SquareBrowser()
    try:
        for i, (label, url, _h) in enumerate(pages):
            try:
                html = _square_html(session, url, browser, health, _BYX_MARKERS)
                for p in parse_byx_html(html, label, official):
                    cur = items.get(p["id"])
                    if cur is None or (p["official"] and not cur["official"]):
                        items[p["id"]] = p
                health["pages_ok"] += 1
            except Exception as e:
                health["pages_failed"] += 1
                if len(health["page_errors"]) < 4:
                    health["page_errors"].append(str(e)[:140])
            if i < len(pages) - 1:
                time.sleep(random.uniform(*SQUARE_PAGE_DELAY))
    finally:
        browser.close()
    if not items:
        raise RuntimeError("Bybit ByX returned nothing — " + ("; ".join(health["page_errors"][:2]) or "no posts parsed"))
    return list(items.values())


def _author_allowed(handle):
    """Hourly per-author cap for community posts."""
    now = time.time()
    dq = _author_hits.setdefault(handle.lower(), deque())
    while dq and now - dq[0] > 3600:
        dq.popleft()
    return len(dq) < SQUARE_AUTHOR_MAX_PER_HOUR


def _author_key(it):
    h = str(it.get("handle", "unknown")).lower()
    return ("byx:" + h) if str(it.get("type_key", "")).startswith("byx") else h


def _square_gate(it, d, cfg, min_score):
    """Extra scrutiny for user-generated posts (Square / ByX). → (ok, reason). Official accounts pass."""
    if it.get("official"):
        return True, ""
    if not cfg.get("square_community", True):
        return False, "community posts are turned off"
    a = d.get("analysis") or {}
    flags = set(a.get("risk_flags") or []) | set(a.get("link_flags") or [])
    if "referral_link" in flags:
        return False, "community post with a referral / affiliate link"
    llm = d.get("llm") or {}
    llm_sure = llm.get("is_opportunity") and llm.get("cost") == "free" and llm.get("confidence", 0) >= 80
    need = int(min_score) + SQUARE_COMMUNITY_SCORE_BONUS + (10 if "engagement_bait" in flags else 0)
    if (a.get("score") or 0) < need and not llm_sure:
        return False, f"community post needs score ≥ {need} (got {a.get('score', 0)})"
    if not _author_allowed(_author_key(it)):
        return False, f"@{it.get('handle')} already forwarded {SQUARE_AUTHOR_MAX_PER_HOUR} this hour"
    return True, ""


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
    # A Square post is not "a campaign" just because it sits on #airdrop's page → no hint
    hint = '' if item.get('type_key') in UGC_TYPES else item.get('category', '')
    return pipeline.evaluate(text, cfg, include_re, exclude_re, context_hint=hint, allow_llm=allow_llm)


# ============================================================
# Poll cycle
# ============================================================
def _source_label(it):
    if it['source'] in ('Binance Square', 'Bybit ByX'):
        tag = 'Official ✔' if it.get('official') else 'Community (unverified)'
        who = it.get('author') if it['source'] == 'Bybit ByX' else '@' + str(it.get('handle', 'unknown'))
        return f"{it['source']} · {tag} · {who}"
    return f"{it['source']} · {it.get('category') or 'announcement'}"


def poll_once(dry_run=False, force_square=False):
    """One pass over the feeds (+ Square when due). Returns a list of decision dicts."""
    global _square_last, _byx_last
    from .crypto_opportunity_detector import format_alert_html
    cfg = dict(_cfg_provider() if _cfg_provider else {})
    include_re = _compile(cfg.get('include'))
    exclude_re = _compile(cfg.get('exclude'))
    prefix = cfg.get('prefix') or '🧭 Crypto Opportunity'
    min_score = int(cfg.get('min_score', 45))
    session = _session()
    results = []

    sources = [('bybit', fetch_bybit), ('binance', fetch_binance)]
    now = time.time()
    if cfg.get('square', True) and (dry_run or force_square or now - _square_last >= SQUARE_POLL_INTERVAL_SEC):
        sources.append(('square', lambda s: fetch_square(s, cfg)))
        if not dry_run:
            _square_last = now
    if cfg.get('byx', True) and (dry_run or force_square or now - _byx_last >= SQUARE_POLL_INTERVAL_SEC):
        sources.append(('byx', lambda s: fetch_byx(s, cfg)))
        if not dry_run:
            _byx_last = now

    for name, fetch in sources:
        h = _health[name]
        is_square = name in ('square', 'byx')          # user-generated community sources
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
        if name == 'square':
            new.sort(key=lambda i: int(i['id'].split(':')[1]))      # oldest post id first
        elif name == 'byx':
            pass                                                    # feed order (newest first) is kept
        else:
            new.sort(key=lambda i: i.get('published') or 0)
        sq_forwarded = sq_llm = 0
        for it in new:
            if is_square and sq_forwarded >= SQUARE_MAX_FORWARD_PER_CYCLE:
                break                                               # rest stays unseen → next cycle
            summary = it.get('summary', '')
            llm_ok = (not dry_run) and (not is_square or sq_llm < SQUARE_LLM_MAX_PER_CYCLE)
            d = _evaluate(it, cfg, include_re, exclude_re, allow_llm=llm_ok)
            if is_square and d.get('llm'):
                sq_llm += 1
            # Binance list gives titles only: fetch the body, then judge again
            # (the body is where "deposit 100 USDT to qualify" lives)
            if (it['source'] == 'Binance' and it.get('code') and not dry_run
                    and (d['forward'] or (d.get('analysis') or {}).get('needs_review'))):
                summary = binance_detail(session, it['code']) or summary
                d = _evaluate(it, cfg, include_re, exclude_re, body=summary)
            decision = 'forward' if d['forward'] else 'skip'
            reason = d['reason']
            analysis = d.get('analysis')

            if decision == 'forward' and is_square:
                ok_gate, why = _square_gate(it, d, cfg, min_score)
                if not ok_gate:
                    decision, reason = 'skip', why
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
                    if is_square:
                        body = _clip(summary, 1800)
                    else:
                        body = it['title'] + ('\n\n' + _clip(summary, 1500) if summary else '')
                    text = format_alert_html(body, a2, _source_label(it), it['url'], prefix=prefix)
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
                if is_square:
                    sq_forwarded += 1
                    if not it.get('official'):
                        _author_hits.setdefault(_author_key(it), deque()).append(time.time())
                logger.info(f"[exchange] forwarded {_source_label(it)}: {it['title'][:80]}")
            if not dry_run:
                with _lock:
                    _seen[it['id']] = time.time()
                # Square is mostly unrelated chatter — keep the decision log readable
                if not (is_square and decision == 'skip' and 'no opportunity concept' in reason):
                    _log_decision(it, decision, reason, analysis)
            results.append({**it, 'decision': decision, 'reason': reason})
        if not dry_run:
            _save_state()
    return results


def probe(cfg=None, only=None):
    """Fetch every feed once, without forwarding, and report what came back.
    Use it to see whether Binance/Square are reachable from THIS server."""
    cfg = dict(cfg or (_cfg_provider() if _cfg_provider else {}) or {})
    s = _session()
    jobs = {'bybit': lambda: fetch_bybit(s), 'binance': lambda: fetch_binance(s),
            'square': lambda: fetch_square(s, cfg), 'byx': lambda: fetch_byx(s, cfg)}
    out = {}
    for name, fn in jobs.items():
        if only and name != only:
            continue
        try:
            items = fn()
            out[name] = {'ok': True, 'count': len(items),
                         'sample': [f"[{i.get('category') or '?'}] {i['title'][:80]}" for i in items[:6]]}
        except Exception as e:
            out[name] = {'ok': False, 'error': str(e)[:300]}
    for key in ('square', 'byx'):
        if not only or only == key:
            h = _health[key]
            out[f'{key}_pages'] = {k: h[k] for k in ('pages_ok', 'pages_failed', 'via_browser', 'page_errors')}
    out['playwright_available'] = PLAYWRIGHT_AVAILABLE
    return out


def _take_force_square():
    global _force_square
    flag, _force_square = _force_square, False
    return flag


def _loop():
    backoff = 0
    while True:
        try:
            cfg = _cfg_provider() if _cfg_provider else {}
            if cfg.get('enabled') and cfg.get('exchange', True):
                poll_once(force_square=_take_force_square())
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


def poll_now(force_square=False):
    global _force_square
    _force_square = _force_square or bool(force_square)
    _poll_now.set()


def get_status():
    with _lock:
        return {
            'running': _thread is not None and _thread.is_alive(),
            'interval_sec': POLL_INTERVAL_SEC,
            'square_interval_sec': SQUARE_POLL_INTERVAL_SEC,
            'square_last_poll': _square_last or None,
            'playwright_available': PLAYWRIGHT_AVAILABLE,
            'baselined': dict(_baselined),
            'seen_count': len(_seen),
            'sources': {k: dict(v) for k, v in _health.items()},
            'recent': list(_recent_log)[-20:],
        }


# ============================================================
# CLI probe
# ============================================================
def _probe():
    """python3 features/exchange_events.py [--square] — what does each feed return from THIS server?"""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    res = probe(cfg={'square': True, 'square_community': True},
                only='square' if '--square' in sys.argv else None)
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _probe()
