# features/telegram_logger.py
"""
Send logs + task lifecycle events to a Telegram channel.

Config is read from settings.json on startup and can be changed live
via features.settings → reconfigure().
"""

import os
import re
import json
import time
import html
import queue
import asyncio
import logging
import threading
import atexit
from datetime import datetime
from collections import OrderedDict

from flask import jsonify

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from config import (
    TELEGRAM_CREDS_FILE,
    TELEGRAM_SESSION_FILE       as TELEGRAM_SESSION,
    TELEGRAM_LOGGER_SESSION     as LOGGER_SESSION,
    TELEGRAM_LOGGER_BOT_SESSION as LOGGER_BOT_SESSION,
)

MAX_MSG_LEN       = 3800
MAX_QUEUE_SIZE    = 500
BATCH_WINDOW_SEC  = 3.0
MIN_SEND_INTERVAL = 3.0
RETRY_DELAYS      = [5, 15, 60]

# ---------- Mutable runtime config ----------
TG_LOG_ENABLED   = False
TG_LOG_CHANNEL   = ''          # str (@username) OR int (channel ID)
TG_LOG_BOT_TOKEN = ''
TG_LOG_LEVEL     = 'WARNING'

# ---------- Internal state ----------
_worker  = None
_handler = None
_task_state = OrderedDict()
_MAX_TRACKED_TASKS = 500


# ============================================================
# Channel normalization
# ============================================================
def _normalize_channel(ch) -> str | int:
    """
    Return either:
      int   — for numeric channel IDs (supergroups/channels)
      str   — '@username' for public channels
      ''    — invalid/empty
    """
    if ch is None:
        return ''

    # Already an int (from settings.json stored as int)
    if isinstance(ch, int):
        return ch

    ch = str(ch).strip().rstrip('/')
    if not ch:
        return ''

    for prefix in ('https://t.me/', 'http://t.me/', 't.me/'):
        if ch.lower().startswith(prefix):
            ch = ch[len(prefix):]
            break

    if not ch:
        return ''

    # Private/numeric channel ID → MUST be int for bots
    if ch.startswith('-'):
        try:
            return int(ch)
        except ValueError:
            logger.warning(f"Channel '{ch}' looks numeric but isn't a valid int")
            return ch

    # Invite link — cannot use directly
    if ch.startswith('+'):
        return ch

    if not ch.startswith('@'):
        ch = '@' + ch
    return ch


# ============================================================
# Config loading
# ============================================================
def _load_config() -> dict:
    try:
        from .settings import get_effective_config
        cfg = get_effective_config()
        return {
            'enabled':  bool(cfg.get('tg_log_enabled', False)),
            'channel':  _normalize_channel(cfg.get('tg_log_channel', '')),
            'token':    str(cfg.get('tg_log_bot_token', '') or '').strip(),
            'level':    (str(cfg.get('tg_log_level', 'WARNING') or 'WARNING')).upper(),
        }
    except Exception as e:
        logger.warning(f"telegram_logger: could not read settings: {e}")
        return {
            'enabled':  os.environ.get('TG_LOG_ENABLED', 'false').lower()
                        in ('1', 'true', 'yes', 'on'),
            'channel':  _normalize_channel(os.environ.get('TG_LOG_CHANNEL', '')),
            'token':    os.environ.get('TG_LOG_BOT_TOKEN', '').strip(),
            'level':    os.environ.get('TG_LOG_LEVEL', 'WARNING').upper(),
        }


def is_enabled() -> bool:
    if not TG_LOG_ENABLED or not TG_LOG_CHANNEL:
        return False
    if isinstance(TG_LOG_CHANNEL, str) and TG_LOG_CHANNEL.startswith('+'):
        return False
    return os.path.exists(TELEGRAM_CREDS_FILE)


# ============================================================
# Helpers
# ============================================================
def _escape(s) -> str:
    return (str(s) if s is not None else '').replace(
        '&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', '', s))


def notify(text: str, level: str = 'info'):
    if _worker is None or not is_enabled():
        return
    emoji = {'info': 'ℹ️', 'success': '✅',
             'warning': '⚠️', 'error': '❌'}.get(level.lower(), '•')
    ts = datetime.now().strftime('%H:%M:%S')
    _worker.enqueue(f"{emoji} <b>[{level.upper()}]</b> ({ts})\n{text}")


# ============================================================
# Task lifecycle hook
# ============================================================
def _guess_task_type(t: dict) -> str:
    if t.get('type'):        return str(t['type'])
    if 'url' in t:           return 'download'
    if 'video_file' in t:    return 'video'
    if 'chat_link' in t:     return 'telegram'
    if 'page_url' in t:      return 'extract'
    if 'query' in t:         return 'search'
    return 'task'


def notify_task_event(task_id: str, task_data: dict):
    if _worker is None or not is_enabled():
        return
    status = task_data.get('status')
    if not status:
        return

    if len(_task_state) > _MAX_TRACKED_TASKS:
        _task_state.popitem(last=False)

    prev = _task_state.get(task_id)
    _task_state[task_id] = (status, time.time())
    _task_state.move_to_end(task_id)

    if prev is None:
        typ = _guess_task_type(task_data)
        detail = f"<code>{task_id[:8]}</code> — {typ}"
        for key in ('url', 'video_file', 'chat_link', 'page_url', 'query'):
            v = task_data.get(key)
            if v:
                detail += f"\n<i>{_escape(str(v)[:120])}</i>"
                break
        notify(f"▶️ <b>New task</b>\n{detail}", level='info')
        return

    if status in ('done', 'error', 'cancelled') and prev[0] != status:
        emoji = {'done': '✅', 'error': '❌', 'cancelled': '⛔'}[status]
        detail = f"<code>{task_id[:8]}</code>"
        out = task_data.get('output_file')
        err = task_data.get('error_msg')
        if out:
            detail += f"\n📄 <code>{_escape(str(out))}</code>"
        if err:
            detail += f"\n<i>{_escape(str(err)[:200])}</i>"
        lvl = 'success' if status == 'done' else (
              'warning' if status == 'cancelled' else 'error')
        notify(f"{emoji} <b>Task {status}</b>\n{detail}", level=lvl)


# ============================================================
# Session bootstrap
# ============================================================
def _bootstrap_session():
    if TG_LOG_BOT_TOKEN:
        return
    src = TELEGRAM_SESSION + '.session'
    dst = LOGGER_SESSION + '.session'
    if not os.path.exists(dst) and os.path.exists(src):
        try:
            import shutil
            shutil.copy2(src, dst)
            logger.info("Cloned Telegram session for logger")
        except Exception as e:
            logger.warning(f"Session clone failed: {e}")


# ============================================================
# Worker
# ============================================================
class _Worker:
    def __init__(self):
        self.q = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.dropped = 0
        self.loop = None
        self.client = None
        self.last_send = 0.0
        self.thread = threading.Thread(
            target=self._run, daemon=True, name='tg-log')
        self.thread.start()

    def enqueue(self, text: str):
        try:
            self.q.put_nowait(text)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        logger.info("Telegram logger worker started")
        while True:
            batch = self._collect_batch()
            if batch:
                self._flush(batch)

    def _collect_batch(self):
        out = []
        try:
            out.append(self.q.get(timeout=1.0))
        except queue.Empty:
            return out
        deadline = time.time() + BATCH_WINDOW_SEC
        while time.time() < deadline and len(out) < 15:
            remaining = max(0.05, deadline - time.time())
            try:
                out.append(self.q.get(timeout=remaining))
            except queue.Empty:
                break
        return out

    def _flush(self, batch):
        text = '\n\n'.join(batch)
        if len(text) > MAX_MSG_LEN:
            text = text[:MAX_MSG_LEN - 40] + '\n\n<i>… (truncated)</i>'

        elapsed = time.time() - self.last_send
        if elapsed < MIN_SEND_INTERVAL:
            time.sleep(MIN_SEND_INTERVAL - elapsed)

        for delay in [0] + RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                self.loop.run_until_complete(self._send(text))
                self.last_send = time.time()
                logger.debug(f"Telegram log sent ({len(batch)} msg, "
                             f"{len(text)} chars) → {TG_LOG_CHANNEL}")
                return
            except Exception as e:
                logger.warning(f"Telegram log send failed: "
                               f"{type(e).__name__}: {e}")
                # FIX: close the client before dropping the reference
                try:
                    if self.client is not None:
                        self.loop.run_until_complete(self._disconnect_quietly())
                except Exception:
                    pass
                self.client = None

    async def _disconnect_quietly(self):
        try:
            if self.client is not None and self.client.is_connected():
                await self.client.disconnect()
        except Exception:
            pass

    async def _send(self, text: str):
        client = await self._ensure_client()
        try:
            await client.send_message(
                TG_LOG_CHANNEL, text,
                parse_mode='html', link_preview=False)
        except Exception as e:
            if 'parse' in str(e).lower() or 'entit' in str(e).lower():
                await client.send_message(
                    TG_LOG_CHANNEL, _strip_html(text),
                    link_preview=False)
            else:
                raise

    async def _ensure_client(self):
        # Reuse a healthy connection
        if self.client is not None and self.client.is_connected():
            return self.client

        # FIX: Properly disconnect any stale client before opening a new one
        if self.client is not None:
            try:
                if self.client.is_connected():
                    await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        from telethon import TelegramClient

        if not os.path.exists(TELEGRAM_CREDS_FILE):
            raise FileNotFoundError(
                f"{TELEGRAM_CREDS_FILE} not found. "
                "Create it with {\"api_id\": ..., \"api_hash\": ...}")

        with open(TELEGRAM_CREDS_FILE) as f:
            creds = json.load(f)
        api_id   = int(creds['api_id'])
        api_hash = creds['api_hash']

        if TG_LOG_BOT_TOKEN:
            # FIX: Bot auth is stateless — use in-memory session to
            # completely eliminate "database is locked" errors.
            logger.info("Telegram logger connecting (bot mode, in-memory session)…")
            client = TelegramClient(':memory:', api_id, api_hash)
            await client.start(bot_token=TG_LOG_BOT_TOKEN)
        else:
            # User mode needs persistence (login session)
            logger.info("Telegram logger connecting (user mode)…")
            client = TelegramClient(LOGGER_SESSION, api_id, api_hash)
            await client.start()

        me = await client.get_me()
        who = getattr(me, 'username', None) or getattr(me, 'first_name', '?')
        logger.info(f"Telegram logger connected as @{who} → {TG_LOG_CHANNEL}")

        self.client = client
        return client


# ============================================================
# Handler
# ============================================================
class _Handler(logging.Handler):
    def __init__(self, worker: _Worker, level: int):
        super().__init__(level=level)
        self.worker = worker

    def emit(self, record):
        try:
            if not is_enabled():
                return
            ts = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
            emoji = {'DEBUG': '🐛', 'INFO': 'ℹ️', 'WARNING': '⚠️',
                     'ERROR': '❌', 'CRITICAL': '🔥'}.get(record.levelname, '•')
            msg = self.format(record)
            text = (f"{emoji} <b>[{record.levelname}]</b> "
                    f"<code>{_escape(record.name)}</code> ({ts})\n"
                    f"{_escape(msg)}")
            self.worker.enqueue(text)
        except Exception:
            self.handleError(record)


# ============================================================
# Registration
# ============================================================
def _shutdown():
    if _worker is None:
        return
    try:
        _worker.enqueue("🛑 <b>ProjectCompress shutting down</b>")
        time.sleep(2.5)
    except Exception:
        pass


def register_logging(app=None):
    global _worker, _handler
    if _worker is not None or _handler is not None:
        return
    _apply_config(initial=True)


def reconfigure():
    _apply_config(initial=False)


def _apply_config(initial: bool):
    global TG_LOG_ENABLED, TG_LOG_CHANNEL, TG_LOG_BOT_TOKEN, TG_LOG_LEVEL
    global _worker, _handler

    cfg = _load_config()
    token_changed   = (cfg['token']   != TG_LOG_BOT_TOKEN)
    channel_changed = (cfg['channel'] != TG_LOG_CHANNEL)

    TG_LOG_ENABLED   = cfg['enabled']
    TG_LOG_CHANNEL   = cfg['channel']
    TG_LOG_BOT_TOKEN = cfg['token']
    TG_LOG_LEVEL     = cfg['level'] or 'WARNING'

    # Force client rebuild if token or channel changed
    if _worker is not None and (token_changed or channel_changed):
        _worker.client = None

    if not is_enabled():
        if _handler is not None:
            try:
                logging.getLogger().removeHandler(_handler)
            except Exception:
                pass
            _handler = None
        logger.info("Telegram logging disabled")
        return

    _bootstrap_session()

    if _worker is None:
        _worker = _Worker()

    level = getattr(logging, TG_LOG_LEVEL, logging.WARNING)
    if _handler is None:
        _handler = _Handler(_worker, level=level)
        _handler.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(_handler)
        atexit.register(_shutdown)
    else:
        _handler.setLevel(level)

    logger.info(f"Telegram logging → {TG_LOG_CHANNEL} @ {TG_LOG_LEVEL}"
                + (" (initial)" if initial else " (reconfigured)"))

    if initial:
        notify("🚀 <b>ProjectCompress started</b>", level='success')


# ============================================================
# Routes
# ============================================================
def register_routes(app):

    @app.route('/telegram/log/test', methods=['GET'])
    def telegram_log_test():
        if not is_enabled():
            reasons = []
            if not TG_LOG_ENABLED:
                reasons.append('logging disabled (enable in Settings)')
            if not TG_LOG_CHANNEL:
                reasons.append('channel not set')
            elif isinstance(TG_LOG_CHANNEL, str) and TG_LOG_CHANNEL.startswith('+'):
                reasons.append('invite links (t.me/+…) cannot be used — '
                               'use @channel or the numeric -100… ID')
            if not os.path.exists(TELEGRAM_CREDS_FILE):
                reasons.append('telegram_creds.json missing')
            return jsonify({
                'enabled': False,
                'reason': 'Telegram logging not ready: ' + '; '.join(reasons),
                'debug': {
                    'enabled':  TG_LOG_ENABLED,
                    'channel':  TG_LOG_CHANNEL or None,
                    'channel_type': type(TG_LOG_CHANNEL).__name__,
                    'has_token': bool(TG_LOG_BOT_TOKEN),
                    'has_creds': os.path.exists(TELEGRAM_CREDS_FILE),
                },
            }), 400

        notify("🧪 <b>Test message</b> — Telegram logging works!", level='success')

        start = time.time()
        delivered = False
        while time.time() - start < 15:
            if _worker is not None and _worker.last_send > start:
                delivered = True
                break
            time.sleep(0.3)

        if delivered:
            return jsonify({
                'enabled': True, 'status': 'sent',
                'channel': str(TG_LOG_CHANNEL),
                'mode': 'bot' if TG_LOG_BOT_TOKEN else 'user',
            })
        return jsonify({
            'enabled': True,
            'status': 'queued',
            'note': 'Queued but delivery not confirmed within 15s. '
                    'Check server logs — likely bot is not admin of the channel.',
            'channel': str(TG_LOG_CHANNEL),
            'mode': 'bot' if TG_LOG_BOT_TOKEN else 'user',
            'queue_size': _worker.q.qsize() if _worker else 0,
        }), 202

    @app.route('/telegram/log/status', methods=['GET'])
    def telegram_log_status():
        return jsonify({
            'enabled':  is_enabled(),
            'channel':  str(TG_LOG_CHANNEL) if TG_LOG_CHANNEL else None,
            'channel_type': type(TG_LOG_CHANNEL).__name__,
            'mode':     'bot' if TG_LOG_BOT_TOKEN else ('user' if is_enabled() else 'off'),
            'level':    TG_LOG_LEVEL,
            'queue':    _worker.q.qsize() if _worker else 0,
            'dropped':  _worker.dropped if _worker else 0,
        })
