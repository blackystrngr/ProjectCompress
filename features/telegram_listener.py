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
  tg_listener_forward_prefix, tg_listener_detection_mode,
  tg_listener_min_score, tg_listener_cost_policy,
  tg_listener_llm_mode, tg_listener_llm_model, tg_listener_llm_base_url,
  tg_listener_llm_api_key, tg_listener_exchange_enabled,
  tg_listener_delivery (account|bot), tg_listener_bot_token, tg_listener_bot_chat

Hardening (v2)
  * Watchdog re-attaches the handler if the shared client is rebuilt.
  * Invalid regex falls back to the default pattern (never "forward all").
  * Numeric IDs work; target resolved once; target removed from sources.
  * Duplicate suppression; send retries; serialised attach/detach.

Added (v3)
  * Decisions come from features.opportunity_pipeline (rules → optional
    LLM second opinion). See crypto_opportunity_detector / llm_classifier.
  * /telegram/listener/recent shows every message and WHY it was forwarded
    or skipped — the answer to "why didn't event X show up?".
  * Starts features.exchange_events: polls Binance + Bybit announcement
    feeds and forwards new events here too.
  * Headline already forwarded from the website isn't forwarded twice.
  * Alerts are sent as Telegram-HTML (bold headline, reward, deadline,
    collapsible original text); everything is escaped.

Added (v4) — unread notifications
  * Alerts sent by your own account are *outgoing*, so Telegram marks them
    read and never notifies you. Delivery "bot" sends them through a Bot API
    bot instead (see features.bot_delivery) → unread badge + notification.
    If the bot fails the alert still goes out via the account (never lost).
"""

import re
import time
import asyncio
import hashlib
import functools
import threading
import logging
from collections import OrderedDict, deque
from flask import request, jsonify
from telethon import events, utils

from . import telegram as tg_module
from . import opportunity_pipeline as pipeline
from . import llm_classifier
from . import bot_delivery
from .crypto_opportunity_detector import format_alert_html, explain
from .telegram import (
    start_tg_loop_if_needed,
    run_in_tg_loop,
)
try:
    from . import exchange_events
except Exception as _e:               # optional — never break the listener
    exchange_events = None
    logging.getLogger(__name__).warning(f"exchange_events unavailable: {_e}")

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
_target_entity = None            # resolved forward target (shared with the poller)
_handler_lock = threading.Lock()
_attach_lock = threading.RLock()  # serialises attach/detach
_watchdog_started = False
_seen = OrderedDict()             # text-hash -> timestamp (dedupe)
_recent = deque(maxlen=200)       # last decisions: why a message was / wasn't forwarded
_stats = {
    'messages_seen': 0,
    'no_text': 0,
    'matches_forwarded': 0,
    'opportunities_detected': 0,
    'opportunities_rejected': 0,
    'duplicates_skipped': 0,
    'bot_sent': 0,
    'bot_failed': 0,
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
            'prefix': (cfg.get('tg_listener_forward_prefix') or DEFAULT_PREFIX).strip(),
            'mode': cfg.get('tg_listener_detection_mode', 'intelligent'),
            'min_score': int(cfg.get('tg_listener_min_score', 45)),
            'cost_policy': cfg.get('tg_listener_cost_policy', 'free'),
            'llm_mode': cfg.get('tg_listener_llm_mode', 'off'),
            'llm_model': (cfg.get('tg_listener_llm_model') or '').strip(),
            'llm_base_url': (cfg.get('tg_listener_llm_base_url') or '').strip(),
            'llm_api_key': (cfg.get('tg_listener_llm_api_key') or '').strip(),
            'exchange': bool(cfg.get('tg_listener_exchange_enabled', True)),
            'delivery': cfg.get('tg_listener_delivery', 'account'),
            # dedicated alert bot, else reuse the Telegram-logger bot token
            'bot_token': ((cfg.get('tg_listener_bot_token') or cfg.get('tg_log_bot_token') or '')).strip(),
            'bot_chat': (cfg.get('tg_listener_bot_chat') or '').strip(),
        }
    except Exception as e:
        logger.warning(f"telegram_listener: could not read settings: {e}")
        return {
            'enabled': False, 'sources': [], 'target': '',
            'include': DEFAULT_INCLUDE, 'exclude': DEFAULT_EXCLUDE,
            'prefix': DEFAULT_PREFIX, 'mode': 'intelligent',
            'min_score': 45, 'cost_policy': 'free',
            'llm_mode': 'off', 'llm_model': '', 'llm_base_url': '', 'llm_api_key': '',
            'exchange': True, 'delivery': 'account', 'bot_token': '', 'bot_chat': '',
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


def _record(source, msg_id, text, decision, reason='', analysis=None, engine=''):
    row = {
        'time': time.time(), 'source': source, 'msg_id': msg_id,
        'text': (text or '')[:200].replace('\n', ' '),
        'decision': decision, 'reason': reason, 'engine': engine,
    }
    if analysis:
        row.update(score=analysis.get('score'), category=analysis.get('category'),
                   cost=analysis.get('cost_tier'), reward=analysis.get('reward') or None)
    _recent.append(row)


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


async def _send_with_retry(client, target, text, parse_mode=None):
    """parse_mode=None → plain text (Telethon's default Markdown would eat _ and *)."""
    delay = 2.0
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            return await client.send_message(target, text, link_preview=False,
                                             parse_mode=parse_mode)
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            if attempt == SEND_ATTEMPTS:
                raise
            logger.warning(
                f"[listener] send attempt {attempt}/{SEND_ATTEMPTS} failed: "
                f"{type(e).__name__}: {e} — retrying in {delay:.0f}s"
            )
            await asyncio.sleep(delay)
            delay *= 2


async def _deliver(client, target_entity, text, parse_mode, cfg):
    """Bot first (real notification); fall back to the account so nothing is lost."""
    if bot_delivery.is_configured(cfg):
        loop = asyncio.get_running_loop()
        ok, err = await loop.run_in_executor(
            None, functools.partial(bot_delivery.send, text, parse_mode, cfg))
        if ok:
            _stats['bot_sent'] += 1
            return 'bot'
        _stats['bot_failed'] += 1
        _stats['last_error'] = f"bot delivery failed: {err}"
        logger.warning(f"[listener] bot delivery failed ({err}) — falling back to account")
    if client is None or target_entity is None:
        raise RuntimeError("no delivery path available (bot failed and account target unresolved)")
    await _send_with_retry(client, target_entity, text, parse_mode=parse_mode)
    return 'account'


# ============================================================
# Attach / detach
# ============================================================
async def _attach_async(cfg):
    global _handler, _attached_client, _target_entity

    client = await tg_module._ensure_client_async()

    # Detach any previously attached handler (from the old client if it changed)
    with _handler_lock:
        old, old_client = _handler, _attached_client
        _handler = None
        _attached_client = None
        _target_entity = None
    if old is not None:
        try:
            (old_client or client).remove_event_handler(old)
            logger.info("[listener] Detached previous handler")
        except Exception as e:
            logger.warning(f"[listener] Failed to remove old handler: {e}")

    if not cfg['sources']:
        logger.info("[listener] No sources configured — nothing to attach")
        return
    if not cfg['target'] and not (cfg.get('bot_chat') and bot_delivery.is_configured(cfg)):
        logger.info("[listener] No target configured — nothing to attach")
        return

    # Resolve target once — fail early on a bad target. In bot mode the bot
    # delivers, so an account-side miss is only a warning (no fallback then).
    bot_mode = bot_delivery.is_configured(cfg)
    try:
        if not cfg['target']:
            raise ValueError("no account-side target set")
        target_entity = await client.get_entity(_parse_peer(cfg['target']))
        target_id = utils.get_peer_id(target_entity)
    except Exception as e:
        if not bot_mode:
            raise Exception(f"Cannot resolve target {cfg['target']!r}: {e}")
        logger.warning(f"[listener] account cannot resolve target {cfg['target']!r} ({e}) "
                       f"— bot delivery will be used without account fallback")
        target_entity, target_id = None, None

    resolved, unresolved = [], []
    for src in cfg['sources']:
        try:
            entity = await client.get_entity(_parse_peer(src))
        except Exception as e:
            logger.warning(f"[listener] Cannot resolve {src}: {e}")
            unresolved.append(src)
            continue
        if target_id is not None and utils.get_peer_id(entity) == target_id:
            logger.warning(f"[listener] Source {src} is the target chat — "
                           f"skipped (would forward in a loop)")
            continue
        resolved.append(entity)
        logger.info(f"[listener] Resolved: {src} → {_channel_display(entity)}")
    _stats['unresolved_sources'] = unresolved
    names = {utils.get_peer_id(e): _channel_display(e) for e in resolved}

    if not resolved:
        raise Exception("None of the source channels could be resolved")

    include_re, inc_err = _compile(cfg['include'], DEFAULT_INCLUDE)
    exclude_re, exc_err = _compile(cfg['exclude'], DEFAULT_EXCLUDE)
    _stats['invalid_patterns'] = (
        ([f"include: {inc_err}"] if inc_err else [])
        + ([f"exclude: {exc_err}"] if exc_err else [])
    )
    prefix = cfg['prefix']
    loop = asyncio.get_running_loop()
    snapshot = dict(cfg)               # settings change → reconfigure → re-attach

    @client.on(events.NewMessage(chats=resolved))
    async def _listener(event):
        source = names.get(getattr(event, 'chat_id', None), 'unknown')
        text = event.raw_text or ''
        try:
            if not text:
                _stats['no_text'] += 1
                _record(source, event.id, '(media only / no text)', 'no_text',
                        'message has no text — images/stickers cannot be matched')
                return
            _stats['messages_seen'] += 1

            # rules → optional LLM (blocking HTTP → run off the event loop)
            d = await loop.run_in_executor(
                None, functools.partial(pipeline.evaluate, text, snapshot,
                                        include_re, exclude_re))
            analysis = d.get('analysis')
            if not d['forward']:
                _stats['opportunities_rejected'] += 1
                _record(source, event.id, text, 'skipped', d['reason'], analysis, d['engine'])
                return
            _stats['opportunities_detected'] += 1

            if _is_duplicate(text):
                _stats['duplicates_skipped'] += 1
                _record(source, event.id, text, 'duplicate', 'same text forwarded recently', analysis, d['engine'])
                logger.info(f"[listener] Duplicate skipped: msg_id={event.id}")
                return
            if exchange_events is not None and exchange_events.covered(text):
                _record(source, event.id, text, 'covered_by_website',
                        'same headline was already forwarded (another channel or the exchange feed)', analysis, d['engine'])
                logger.info(f"[listener] Already forwarded from exchange feed: msg_id={event.id}")
                return

            chat = await event.get_chat()
            source = _channel_display(chat)
            link = _message_link(chat, event.id)
            if analysis is not None:
                alert = format_alert_html(text, analysis, source, link, prefix=prefix)
                pm = 'html'
            else:
                body = text[:3500]
                head = f"{prefix} ({source})"
                alert = f"{head}\n\n{body}\n\n🔗 {link}" if link else f"{head}\n\n{body}"
                pm = None
            via = await _deliver(client, target_entity, alert, pm, snapshot)
            _stats['matches_forwarded'] += 1
            _stats['last_forward_at'] = time.time()
            _record(source, event.id, text, 'forwarded', f"{d['reason']} [via {via}]", analysis, d['engine'])
            if exchange_events is not None:
                exchange_events.note_forwarded(text)
            logger.info(f"[listener] Forwarded match: {source} msg_id={event.id} "
                        f"({d['engine']}: {d['reason'][:60]})")
        except Exception as e:
            _stats['last_error'] = f"{type(e).__name__}: {e}"
            _record(source, event.id, text, 'send_error', _stats['last_error'])
            logger.exception("[listener] handler error")

    with _handler_lock:
        _handler = _listener
        _attached_client = client
        _target_entity = target_entity
    _stats['started_at'] = time.time()
    _stats['last_error'] = None
    logger.info(
        f"[listener] Attached to shared client — "
        f"{len(resolved)} source(s) → {cfg['target']}"
    )


async def _detach_async():
    global _handler, _attached_client, _target_entity
    with _handler_lock:
        old, old_client = _handler, _attached_client
        _handler = None
        _attached_client = None
        _target_entity = None
    if old is None:
        return
    client = old_client or tg_module.get_persistent_client()
    if client is not None:
        try:
            client.remove_event_handler(old)
            logger.info("[listener] Detached")
        except Exception as e:
            logger.warning(f"[listener] Detach failed: {e}")


async def _forward_text_async(text, parse_mode=None):
    cfg = _load_config()
    client, target = _attached_client, _target_entity
    if not bot_delivery.is_configured(cfg):
        if client is None or target is None or not client.is_connected():
            return False
    await _deliver(client, target, text, parse_mode, cfg)
    _stats['matches_forwarded'] += 1
    _stats['last_forward_at'] = time.time()
    return True


def _send_text_sync(text, parse_mode=None):
    """Used by features.exchange_events to post to the same target chat."""
    return bool(run_in_tg_loop(_forward_text_async(text, parse_mode)))


def _start_exchange_poller():
    if exchange_events is not None:
        try:
            exchange_events.start(_load_config, _send_text_sync)
        except Exception:
            logger.exception("[listener] could not start exchange poller")


def attach(cfg=None):
    start_tg_loop_if_needed()
    if cfg is None:
        cfg = _load_config()
    with _attach_lock:
        try:
            run_in_tg_loop(_attach_async(cfg))
            _start_watchdog()
            _start_exchange_poller()
            return True
        except Exception as e:
            _stats['last_error'] = f"{type(e).__name__}: {e}"
            logger.exception("[listener] Attach failed")
            _start_watchdog()   # keep retrying in the background
            _start_exchange_poller()
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
    if not cfg['enabled'] or not cfg['sources'] or not (cfg['target'] or cfg['bot_chat']):
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
        'no_text_messages': _stats['no_text'],
        'matches_forwarded': _stats['matches_forwarded'],
        'opportunities_detected': _stats['opportunities_detected'],
        'opportunities_rejected': _stats['opportunities_rejected'],
        'duplicates_skipped': _stats['duplicates_skipped'],
        'last_forward_at': _stats['last_forward_at'],
        'last_error': _stats['last_error'],
        'delivery': bot_delivery.get_status(cfg),
        'llm': llm_classifier.get_status(cfg),
        'exchange': exchange_events.get_status() if exchange_events else None,
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
        d = pipeline.evaluate(text, cfg, include_re, exclude_re,
                              allow_llm=bool(data.get('use_llm')))
        analysis = d.get('analysis')
        return jsonify({
            'matched': d['forward'],
            'reason': d['reason'],
            'engine': d['engine'],
            'summary': explain(analysis) if analysis else d['reason'],
            'intelligent': analysis,
            'llm': d.get('llm'),
            'included': included,
            'excluded': excluded,
            'include_pattern': cfg['include'],
            'exclude_pattern': cfg['exclude'],
            'include_valid': inc_err is None,
            'exclude_valid': exc_err is None,
            'include_error': inc_err,
            'exclude_error': exc_err,
        })

    @app.route('/telegram/listener/recent', methods=['GET'])
    def listener_recent():
        """Last messages seen and why each was forwarded or skipped.
        ?n=50  ?decision=skipped|forwarded|duplicate|no_text|covered_by_website"""
        try:
            n = max(1, min(200, int(request.args.get('n', 50))))
        except ValueError:
            n = 50
        only = (request.args.get('decision') or '').strip()
        rows = [r for r in _recent if not only or r['decision'] == only]
        summary = {}
        for r in _recent:
            summary[r['decision']] = summary.get(r['decision'], 0) + 1
        return jsonify({'summary': summary, 'messages': list(reversed(rows))[:n]})

    @app.route('/telegram/listener/bot/<action>', methods=['POST'])
    def listener_bot(action):
        """verify | detect | test — uses the token/chat in the request body when
        given (so you can try values before saving), else the saved settings."""
        data = request.get_json(silent=True) or {}
        cfg = _load_config()
        token = (data.get('token') or '').strip() or cfg['bot_token']
        chat = str(data.get('chat') or '').strip() or cfg['bot_chat'] or cfg['target']
        if action == 'verify':
            return jsonify(bot_delivery.verify(token))
        if action == 'detect':
            return jsonify(bot_delivery.detect_chats(token))
        if action == 'test':
            test_cfg = dict(cfg, delivery='bot', bot_token=token, bot_chat=chat)
            ok, err = bot_delivery.send(
                "<b>✅ Alert bot connected</b>\nYou'll get opportunity alerts here "
                "as normal unread messages.", 'html', test_cfg)
            return jsonify({'ok': ok, 'error': err})
        return jsonify({'error': 'unknown action'}), 404

    @app.route('/telegram/listener/exchange', methods=['GET', 'POST'])
    def listener_exchange():
        """GET: poller status. POST: poll Binance/Bybit right now."""
        if exchange_events is None:
            return jsonify({'error': 'exchange_events module not available'}), 500
        if request.method == 'POST':
            exchange_events.poll_now()
            time.sleep(1.5)
        return jsonify(exchange_events.get_status())

    @app.route('/telegram/listener/reload', methods=['POST'])
    def listener_reload():
        try:
            reconfigure()
            time.sleep(0.5)
            return jsonify(get_status())
        except Exception as e:
            return jsonify({'error': str(e)}), 500
