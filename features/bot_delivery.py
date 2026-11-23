"""Deliver alerts through a Telegram BOT (Bot API) so they arrive as real
incoming messages — unread badge + notification.

Why: the listener runs on your own user account. Anything that account sends is
an *outgoing* message, which Telegram marks as read and never notifies you
about. A bot is a different sender, so its messages are incoming.

Setup (one time)
  1. @BotFather → /newbot → copy the token.
  2. Open your new bot and press START (a bot cannot message you before that).
  3. Settings → Announcement Listener → paste the token → "Detect my chat" →
     Save → "Send test via bot".
  Channel / group instead of a private chat: add the bot as an admin that can
  post, and use @channelusername or the -100… id as the chat.

Plain `requests` against api.telegram.org — no Telethon, no extra session.
The token is never logged (errors are redacted).
"""

import re
import html
import time
import logging
import threading

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
MAX_TEXT = 4096
HTTP_TIMEOUT = 20
MAX_RETRY_AFTER = 30

_lock = threading.Lock()
_stats = {"sent": 0, "failed": 0, "last_error": None, "last_ok": None, "bot_username": None}


# ============================================================
# Helpers
# ============================================================
def resolve_token(cfg):
    return (cfg.get("bot_token") or "").strip()


def resolve_chat(cfg):
    return str(cfg.get("bot_chat") or "").strip()


def valid_token(token):
    return bool(TOKEN_RE.match(token or ""))


def is_configured(cfg):
    return (cfg.get("delivery") == "bot" and valid_token(resolve_token(cfg))
            and bool(resolve_chat(cfg)))


def _redact(text, token):
    text = str(text)
    return text.replace(token, "<token>") if token else text


def _strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def _call(token, method, payload=None, timeout=HTTP_TIMEOUT):
    """→ (http_status, json_dict). Network errors return (0, {'description': ...})."""
    url = API_URL.format(token=token, method=method)
    try:
        r = requests.post(url, json=payload or {}, timeout=timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"description": (r.text or "")[:120]}
    except Exception as e:                                     # network / DNS / timeout
        return 0, {"description": _redact(f"{type(e).__name__}: {e}", token)}


def _explain(status, data):
    """Turn Bot API errors into an instruction the user can act on."""
    desc = (data or {}).get("description", "") or f"HTTP {status}"
    low = desc.lower()
    if status == 401 or "unauthorized" in low:
        return "Bot token rejected (wrong or revoked) — re-copy it from @BotFather"
    if "chat not found" in low:
        return ("Chat not found — open your bot and press START, then use 'Detect my chat'; "
                "for a channel add the bot as admin and use @channelname or the -100… id")
    if "bot was blocked" in low or "can't initiate conversation" in low or "user is deactivated" in low:
        return "The bot can't message that chat — open the bot and press START (or unblock it)"
    if "not enough rights" in low or "have no rights" in low or "need administrator" in low:
        return "Bot needs 'Post messages' admin rights in that channel/group"
    if "kicked" in low or "not a member" in low:
        return "Bot was removed from that chat — add it back as admin"
    return desc[:200]


# ============================================================
# Send
# ============================================================
def send(text, parse_mode, cfg):
    """Send one alert. → (ok: bool, error: str). Never raises."""
    token, chat = resolve_token(cfg), resolve_chat(cfg)
    if not valid_token(token):
        return False, "bot token missing or malformed"
    if not chat:
        return False, "bot chat not set — press START in the bot and use 'Detect my chat'"

    body = (text or "")[:MAX_TEXT]
    pm = {"html": "HTML", "HTML": "HTML", "md": "Markdown", "markdown": "Markdown"}.get(parse_mode or "", None)

    def payload(plain=False):
        p = {"chat_id": chat, "text": _strip_html(body) if plain and pm == "HTML" else body,
             "link_preview_options": {"is_disabled": True}}
        if pm and not plain:
            p["parse_mode"] = pm
        return p

    plain = False
    for attempt in range(1, 4):
        status, data = _call(token, "sendMessage", payload(plain))
        if status == 200 and data.get("ok"):
            with _lock:
                _stats["sent"] += 1
                _stats["last_ok"] = time.time()
                _stats["last_error"] = None
            return True, ""
        desc = (data or {}).get("description", "").lower()
        if status == 429 and attempt < 3:
            wait = min(MAX_RETRY_AFTER, int((data.get("parameters") or {}).get("retry_after", 3)))
            logger.warning(f"[bot] rate limited — waiting {wait}s")
            time.sleep(wait)
            continue
        if status == 400 and "parse entities" in desc and not plain:
            plain = True                      # malformed markup → resend as plain text
            continue
        if status == 0 and attempt < 3:       # transient network error
            time.sleep(2 * attempt)
            continue
        break

    err = _explain(status, data)
    with _lock:
        _stats["failed"] += 1
        _stats["last_error"] = err
    logger.warning(f"[bot] send failed: {err}")
    return False, err


# ============================================================
# Setup helpers (used by the Settings UI)
# ============================================================
def verify(token):
    """→ dict(ok, username, name, error)."""
    token = (token or "").strip()
    if not valid_token(token):
        return {"ok": False, "error": "token looks malformed (expected 123456789:AA…)"}
    status, data = _call(token, "getMe")
    if status == 200 and data.get("ok"):
        r = data["result"]
        with _lock:
            _stats["bot_username"] = r.get("username")
        return {"ok": True, "username": r.get("username"), "name": r.get("first_name"), "id": r.get("id")}
    return {"ok": False, "error": _explain(status, data)}


def detect_chats(token):
    """Find chats that have talked to the bot (the user pressed START).
    → dict(ok, chats=[{id,type,label}], error)."""
    token = (token or "").strip()
    if not valid_token(token):
        return {"ok": False, "error": "token looks malformed", "chats": []}
    status, data = _call(token, "getUpdates", {
        "limit": 50, "timeout": 0,
        "allowed_updates": ["message", "my_chat_member", "channel_post"]})
    if status != 200 or not data.get("ok"):
        desc = (data or {}).get("description", "")
        if "webhook" in desc.lower():
            return {"ok": False, "chats": [], "error": "this bot has a webhook set; delete it or type the chat id manually"}
        return {"ok": False, "chats": [], "error": _explain(status, data)}

    seen, chats = set(), []
    for upd in reversed(data.get("result") or []):             # newest first
        for key in ("message", "channel_post", "my_chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if not chat or chat["id"] in seen:
                continue
            seen.add(chat["id"])
            label = (chat.get("title") or chat.get("username") and "@" + chat["username"]
                     or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or str(chat["id"]))
            chats.append({"id": chat["id"], "type": chat.get("type"), "label": label})
    chats.sort(key=lambda c: c["type"] != "private")           # private chats first
    if not chats:
        return {"ok": True, "chats": [], "error": "no chats yet — open your bot in Telegram and press START, then try again"}
    return {"ok": True, "chats": chats, "error": ""}


def get_status(cfg):
    with _lock:
        return {
            "delivery": cfg.get("delivery", "account"),
            "configured": is_configured(cfg),
            "token_set": valid_token(resolve_token(cfg)),
            "chat": resolve_chat(cfg),
            "bot_username": _stats["bot_username"],
            "sent": _stats["sent"], "failed": _stats["failed"],
            "last_error": _stats["last_error"], "last_ok": _stats["last_ok"],
        }
