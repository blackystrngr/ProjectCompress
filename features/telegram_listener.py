# features/telegram_listener.py
"""
Telegram announcement listener.

Attaches an event handler to the SHARED persistent Telegram client
(see features.telegram._ensure_client_async). No separate session,
no separate loop, no separate SQLite handle — one connection, one
auth key, one rate-limit bucket.

Config keys (settings.json):
  tg_listener_enabled, tg_listener_sources, tg_listener_target,
  tg_listener_include_pattern, tg_listener_exclude_pattern,
  tg_listener_forward_prefix

Hardening over the first version
--------------------------------
  * Watchdog re-attaches the handler if the shared client is rebuilt
    (handlers live on the client object — a new client has none).
  * Invalid regex no longer means "forward everything": it falls back
    to the built-in default pattern and the problem is reported.
  * Numeric IDs (-100123…) work for sources and target.
  * Target is resolved once at attach time (bad target fails early),
    and is removed from the source list (no forward loops).
  * Duplicate announcements (cross-posts / re-posts) are suppressed.
  * Forwarding retries on dropped connections.
  * attach/detach are serialised (settings save + timer + reload route
    can no longer race each other).
"""

import re
import time
import asyncio
import hashlib
import threading
import logging
from collections import OrderedDict
from flask import request, jsonify
from telethon import events, utils

from . import telegram as tg_module
from .crypto_opportunity_detector import analyze_message, format_alert
from .telegram import (
    start_tg_loop_if_needed,
    run_in_tg_loop,
)

logger = logging.getLogger(__name__)

DEFAULT_INCLUDE = (
    r"\b(learn\s*&?\s*earn|read\s*to\s*earn|demo\s*trading|"
    r"demo\s*championship|demo\s*arena|demo\s*challenge|"
    r"no\s*deposit|zero\s*deposit|quiz|free\s*usdt|"
    r"red\s*packet|word\s*of\s*the\s*day|wotd)\b"
)
DEFAULT_EXCLUDE = (
    r"\b(first\s*deposit|net\s*deposit|deposit\s*at\s*least|"
    r"futures\s*margin\s*maintenance|scheduled\s*system\s*upgrade|"
    r"delisting\s*notice)\b"
)
DEFAULT_PREFIX = "🧭 Crypto Opportunity"

WATCHDOG_INTERVAL_SEC = 30
DEDUPE_TTL_SEC = 6 * 3600
DEDUPE_MAX_ENTRIES = 500
SEND_ATTEMPTS = 3

# Runtime state
_handler = None
_attached_client = None          # the client object the handler lives on
_handler_lock = threading.Lock()
_attach_lock = threading.RLock()  # serialises attach/detach
_watchdog_started = False
_seen = OrderedDict()             # text-hash -> timestamp (dedupe)
_stats = {
    'messages_seen': 0,
    'matches_forwarded': 0,
    'opportunities_detected': 0,
    'opportunities_rejected': 0,
    'duplicates_skipped': 0,
    'unresolved_sources': [],
    'invalid_patterns': [],
    'last_error': None,
    'started_at': None,
    'last_forward_at': None,
}


# ============================================================
# Config / helpers
# ============================================================
def _load_config():
    try:
        from .settings import get_effective_config
        cfg = get_effective_config()
        return {
            'enabled': bool(cfg.get('tg_listener_enabled', False)),
            'sources': [s.strip() for s in (cfg.get('tg_listener_sources') or '').split(',') if s.strip()],
            'target': (cfg.get('tg_listener_target') or '').strip(),
            'include': (cfg.get('tg_listener_include_pattern') or DEFAULT_INCLUDE).strip(),
            'exclude': (cfg.get('tg_listener_exclude_pattern') or DEFAULT_EXCLUDE).strip(),
            'prefix': (cfg.get('tg_listener_forward_prefix') or '🧭 Crypto Opportunity').strip(),
            'mode': cfg.get('tg_listener_detection_mode', 'intelligent'),
            'min_score': int(cfg.get('tg_listener_min_score', 45)),
            'cost_policy': cfg.get('tg_listener_cost_policy', 'free'),
        }
    except Exception as e:
        logger.warning(f"telegram_listener: could not read settings: {e}")
        return {
            'enabled': False, 'sources': [], 'target': '',
            'include': DEFAULT_INCLUDE, 'exclude': DEFAULT_EXCLUDE,
            'prefix': '🧭 Crypto Opportunity', 'mode': 'intelligent',
            'min_score': 45, 'cost_policy': 'free',
        }


def _parse_peer(value):
    """'-1001234567890' → int (Telethon treats str as username/phone)."""
    v = str(value).strip()
    if re.fullmatch(r'-?\d+', v):
        return int(v)
    return v


def _compile(pattern, fallback=None):
    """
    Compile a regex. On error: log, and (if given) compile `fallback`
    instead — never return None, so a typo can't turn the filter off.
    Returns (compiled, error_or_None).
    """
    try:
        return re.compile(pattern, re.IGNORECASE), None
    except re.error as e:
        logger.error(f"[listener] Invalid regex {pattern!r}: {e}")
        if fallback is not None:
            return re.compile(fallback, re.IGNORECASE), str(e)
        return None, str(e)


def _should_forward(text, include_re, exclude_re):
    if not text:
        return False
    if include_re and not include_re.search(text):
        return False
    if exclude_re and exclude_re.search(text):
        return False
    return True


def _text_key(text):
    norm = re.sub(r'\s+', ' ', text).strip().lower()
    return hashlib.sha1(norm.encode('utf-8', errors='ignore')).hexdigest()


def _is_duplicate(text):
    """True if the same announcement was forwarded recently."""
    now = time.time()
    while _seen:
        k, ts = next(iter(_seen.items()))
        if now - ts > DEDUPE_TTL_SEC or len(_seen) > DEDUPE_MAX_ENTRIES:
            _seen.popitem(last=False)
        else:
            break
    key = _text_key(text)
    if key in _seen:
        return True
    _seen[key] = now
    return False


def _channel_display(chat):
    title = getattr(chat, 'title', None)
    username = getattr(chat, 'username', None)
    if title and username:
        return f"{title} (@{username})"
    if title:
        return title
    if username:
        return f"@{username}"
    return "Unknown channel"


def _message_link(chat, msg_id):
    username = getattr(chat, 'username', None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    chat_id = getattr(chat, 'id', None)
    if chat_id:
        cid = str(chat_id)
        if cid.startswith('-100'):
            cid = cid[4:]
        return f"https://t.me/c/{cid}/{msg_id}"
    return ''


async def _send_with_retry(client, target, text):
    delay = 2.0
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            return await client.send_message(target, text, link_preview=False)
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            if attempt == SEND_ATTEMPTS:
                raise
            logger.warning(
                f"[listener] send attempt {attempt}/{SEND_ATTEMPTS} failed: "
                f"{type(e).__name__}: {e} — retrying in {delay:.0f}s"
            )
            await asyncio.sleep(delay)
            delay *= 2


# ============================================================
# Attach / detach
# ============================================================
async def _attach_async(cfg):
    global _handler, _attached_client

    client = await tg_module._ensure_client_async()

    # Detach any previously attached handler (from the old client if it changed)
    with _handler_lock:
        old, old_client = _handler, _attached_client
        _handler = None
        _attached_client = None
    if old is not None:
        try:
            (old_client or client).remove_event_handler(old)
            logger.info("[listener] Detached previous handler")
        except Exception as e:
            logger.warning(f"[listener] Failed to remove old handler: {e}")

    if not cfg['sources']:
        logger.info("[listener] No sources configured — nothing to attach")
        return
    if not cfg['target']:
        logger.info("[listener] No target configured — nothing to attach")
        return

    # Resolve target once — fail early on a bad target
    try:
        target_entity = await client.get_entity(_parse_peer(cfg['target']))
        target_id = utils.get_peer_id(target_entity)
    except Exception as e:
        raise Exception(f"Cannot resolve target {cfg['target']!r}: {e}")

    resolved, unresolved = [], []
    for src in cfg['sources']:
        try:
            entity = await client.get_entity(_parse_peer(src))
        except Exception as e:
            logger.warning(f"[listener] Cannot resolve {src}: {e}")
            unresolved.append(src)
            continue
        if utils.get_peer_id(entity) == target_id:
            logger.warning(f"[listener] Source {src} is the target chat — "
                           f"skipped (would forward in a loop)")
            continue
        resolved.append(entity)
        logger.info(f"[listener] Resolved: {src} → {_channel_display(entity)}")
    _stats['unresolved_sources'] = unresolved

    if not resolved:
        raise Exception("None of the source channels could be resolved")

    include_re, inc_err = _compile(cfg['include'], DEFAULT_INCLUDE)
    exclude_re, exc_err = _compile(cfg['exclude'], DEFAULT_EXCLUDE)
    _stats['invalid_patterns'] = (
        ([f"include: {inc_err}"] if inc_err else [])
        + ([f"exclude: {exc_err}"] if exc_err else [])
    )
    prefix = cfg['prefix']

    @client.on(events.NewMessage(chats=resolved))
    async def _listener(event):
        try:
            text = event.raw_text or ''
            if not text:
                return
            _stats['messages_seen'] += 1
            if exclude_re and exclude_re.search(text):
                return
            analysis = None
            if cfg.get('mode', 'intelligent') == 'intelligent':
                analysis = analyze_message(
                    text,
                    min_score=max(0, min(100, int(cfg.get('min_score', 45)))),
                    policy=cfg.get('cost_policy', 'free'),
                )
                if not analysis.get('accepted'):
                    _stats['opportunities_rejected'] += 1
                    return
                _stats['opportunities_detected'] += 1
            elif not _should_forward(text, include_re, exclude_re):
                return
            if _is_duplicate(text):
                _stats['duplicates_skipped'] += 1
                logger.info(f"[listener] Duplicate skipped: msg_id={event.id}")
                return
            chat = await event.get_chat()
            source = _channel_display(chat)
            link = _message_link(chat, event.id)
            if analysis is not None:
                alert = format_alert(text, analysis, source, link)
                alert = f"{prefix}\n\n{alert}"
            else:
                body = text[:3500]
                head = f"{prefix} ({source})"
                alert = f"{head}\n\n{body}\n\n🔗 {link}" if link else f"{head}\n\n{body}"
            await _send_with_retry(client, target_entity, alert)
            _stats['matches_forwarded'] += 1
            _stats['last_forward_at'] = time.time()
            logger.info(f"[listener] Forwarded match: {source} msg_id={event.id}")
        except Exception as e:
            _stats['last_error'] = f"{type(e).__name__}: {e}"
            logger.exception("[listener] handler error")

    with _handler_lock:
        _handler = _listener
        _attached_client = client
    _stats['started_at'] = time.time()
    _stats['last_error'] = None
    logger.info(
        f"[listener] Attached to shared client — "
        f"{len(resolved)} source(s) → {cfg['target']}"
    )


async def _detach_async():
    global _handler, _attached_client
    with _handler_lock:
        old, old_client = _handler, _attached_client
        _handler = None
        _attached_client = None
    if old is None:
        return
    client = old_client or tg_module.get_persistent_client()
    if client is not None:
        try:
            client.remove_event_handler(old)
            logger.info("[listener] Detached")
        except Exception as e:
            logger.warning(f"[listener] Detach failed: {e}")


def attach(cfg=None):
    start_tg_loop_if_needed()
    if cfg is None:
        cfg = _load_config()
    with _attach_lock:
        try:
            run_in_tg_loop(_attach_async(cfg))
            _start_watchdog()
            return True
        except Exception as e:
            _stats['last_error'] = f"{type(e).__name__}: {e}"
            logger.exception("[listener] Attach failed")
            _start_watchdog()   # keep retrying in the background
            return False


def detach():
    if _handler is None:
        return False
    with _attach_lock:
        try:
            run_in_tg_loop(_detach_async())
            return True
        except Exception as e:
            logger.warning(f"[listener] Detach failed: {e}")
            return False


def reconfigure():
    """Called by settings_post after any tg_listener_* change. Non-blocking."""
    cfg = _load_config()

    def _work():
        try:
            if cfg['enabled']:
                attach(cfg)
            else:
                detach()
        except Exception:
            logger.exception("[listener] reconfigure failed")

    threading.Thread(target=_work, daemon=True, name='tg-listener-reconf').start()


def start_listener_if_enabled():
    cfg = _load_config()
    if cfg['enabled']:
        logger.info("[listener] Auto-attaching (enabled in settings)")
        # 6s delay so the persistent client has time to warm up
        threading.Timer(6.0, lambda: attach(cfg)).start()


# ============================================================
# Watchdog — keeps the handler alive across client rebuilds
# ============================================================
def _needs_reattach():
    """Enabled in settings, but no live handler on the CURRENT client."""
    cfg = _load_config()
    if not cfg['enabled'] or not cfg['sources'] or not cfg['target']:
        return None
    client = tg_module.get_persistent_client()
    if client is None:
        return None                     # client not up yet
    if _handler is None or client is not _attached_client:
        return cfg
    return None


def _watchdog_tick():
    cfg = _needs_reattach()
    if cfg is not None:
        logger.warning("[listener] Handler missing on current client — re-attaching")
        attach(cfg)


def _watchdog_loop():
    while True:
        time.sleep(WATCHDOG_INTERVAL_SEC)
        try:
            _watchdog_tick()
        except Exception:
            logger.exception("[listener] watchdog error")


def _start_watchdog():
    global _watchdog_started
    with _handler_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    threading.Thread(target=_watchdog_loop, daemon=True,
                     name='tg-listener-watchdog').start()


# ============================================================
# Status / routes
# ============================================================
def get_status():
    cfg = _load_config()
    client = tg_module.get_persistent_client()
    running = (_handler is not None and client is not None
               and client is _attached_client and client.is_connected())
    return {
        'enabled': cfg['enabled'],
        'running': running,
        'sources': cfg['sources'],
        'unresolved_sources': _stats['unresolved_sources'],
        'target': cfg['target'],
        'include': cfg['include'],
        'exclude': cfg['exclude'],
        'prefix': cfg['prefix'],
        'detection_mode': cfg['mode'],
        'min_score': cfg['min_score'],
        'cost_policy': cfg['cost_policy'],
        'invalid_patterns': _stats['invalid_patterns'],
        'started_at': _stats['started_at'],
        'uptime_sec': int(time.time() - _stats['started_at']) if _stats['started_at'] else 0,
        'messages_seen': _stats['messages_seen'],
        'matches_forwarded': _stats['matches_forwarded'],
        'opportunities_detected': _stats['opportunities_detected'],
        'opportunities_rejected': _stats['opportunities_rejected'],
        'duplicates_skipped': _stats['duplicates_skipped'],
        'last_forward_at': _stats['last_forward_at'],
        'last_error': _stats['last_error'],
    }


def register_routes(app):

    @app.route('/telegram/listener/status', methods=['GET'])
    def listener_status():
        return jsonify(get_status())

    @app.route('/telegram/listener/test', methods=['POST'])
    def listener_test():
        data = request.get_json(silent=True) or {}
        text = (data.get('text') or '').strip()
        if not text:
            return jsonify({'error': 'text required'}), 400
        cfg = _load_config()
        include_re, inc_err = _compile(cfg['include'], DEFAULT_INCLUDE)
        exclude_re, exc_err = _compile(cfg['exclude'], DEFAULT_EXCLUDE)
        included = bool(include_re.search(text))
        excluded = bool(exclude_re.search(text))
        intelligent = analyze_message(text, min_score=max(0, min(100, int(cfg.get('min_score', 45)))), policy=cfg.get('cost_policy', 'free'))
        return jsonify({
            'matched': (intelligent.get('accepted', False) if cfg.get('mode') == 'intelligent' else included) and not excluded,
            'intelligent': intelligent,
            'included': included,
            'excluded': excluded,
            'include_pattern': cfg['include'],
            'exclude_pattern': cfg['exclude'],
            'include_valid': inc_err is None,
            'exclude_valid': exc_err is None,
            'include_error': inc_err,
            'exclude_error': exc_err,
        })

    @app.route('/telegram/listener/reload', methods=['POST'])
    def listener_reload():
        try:
            reconfigure()
            time.sleep(0.5)
            return jsonify(get_status())
        except Exception as e:
            return jsonify({'error': str(e)}), 500
