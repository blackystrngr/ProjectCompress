# Crypto Opportunity Monitor (v4)

Forwards **free** crypto opportunities from Telegram channels **and** the Binance / Bybit announcement
feeds to your alert chat, with a log that tells you exactly why anything was skipped.

## Unread notifications (bot delivery)

Alerts sent by your own account are *outgoing* messages: Telegram marks them read and never notifies you.
Delivery **Bot** sends them through a Bot API bot instead, so they arrive as normal unread messages.

1. Telegram → **@BotFather** → `/newbot` → copy the token.
2. Open your new bot and press **START** (a bot cannot message you before that).
3. Settings → Announcement Listener → *Deliver alerts via* **Bot**, paste the token, click **Detect my chat**
   (fills your chat id), **Save**, then **Send test via bot**.

* Channel/group instead: add the bot as an admin that can post and use `@channelname` or the `-100…` id.
* If no dedicated token is set, the Telegram-logger bot token is reused.
* If the bot fails, the alert still goes out through your account (never lost) and the status line shows the reason.
* Make sure the chat/bot isn't muted. The token is stored in `settings.json` (secrets dir), never returned by the API.

## Why v1 missed events (fixed)

| Problem in v1 | Fix in v3 |
|---|---|
| Dropped your original keywords: `learn & earn`, `quiz`, `red packet`, `word of the day`, `demo trading`, `crypto box` … | All covered (test `test_original_include_vocabulary_is_covered`) |
| Any cost word anywhere (`deposit`, `stake`, `swap`…) rejected the whole post — even "no staking needed" | Per-sentence cost analysis: *required* vs *mentioned* vs *negated* |
| Needed a literal "free / no deposit" | Quizzes, learn-&-earn, testnets, faucets, social quests, demo trading, red packets count as zero-cost by nature |
| Channels only post a subset of campaigns | Polls Binance + Bybit announcement feeds directly |
| No way to see why a post was skipped | `/telegram/listener/recent` + "Why were messages skipped?" button |
| Pure rules can't read nuance | Optional LLM second opinion (off by default) |

## How a message is judged

```
text ──► exclude regex veto ──► rules engine ──► (optional) LLM ──► forward / skip
                                  │  scam & look-alike-domain gate (never overridden)
                                  │  mechanism score  (airdrop, testnet, learn&earn, quests …)
                                  │  cost tier: free | unstated | mentioned | required
                                  └─ reward / deadline / platform extraction
```

**Cost policy** (Settings → Announcement Listener)

* `free` (default) – reject only when a capital action is *required* (deposit/stake/lock/trade volume/buy/bridge/hold…).
* `strict` – old v1 behaviour: also needs an explicit "free / no deposit" statement.
* `any` – ignore cost (scams are still rejected).

Rejected outright, never sent to an LLM: seed-phrase / private-key requests, "connect wallet to verify",
pay-to-claim, guaranteed-profit bait, look-alike domains (`binance-airdrop.xyz`).

## Optional LLM second opinion

Off by default. Any OpenAI-compatible API (DeepSeek, OpenAI, OpenRouter, Groq, Ollama/vLLM).

* **Assist** – only borderline posts (below threshold, or cost unclear) are sent to the model. Cheap.
* **Full** – every candidate post is checked; a confident "no" (≥80%) vetoes a rule match.
* Settings: base URL (default `https://api.deepseek.com`), model (default `deepseek-chat`), API key
  (stored in `settings.json` in the secrets dir, never returned by the API; or env `LLM_API_KEY` /
  `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`).
* Guards: result cache, 20 calls/min, 400 calls/day, circuit breaker after 5 failures, message wrapped as
  untrusted data (prompt-injection defence), strict JSON validation. Any failure falls back to the rules.
* **Privacy:** the text of the posts it reviews is sent to your provider.

## Binance + Bybit feeds

`features/exchange_events.py` polls every 2 minutes (backs off on errors).

* **Bybit** – official public endpoint `GET /v5/announcements/index` (documented).
* **Binance** – the website's own CMS feed (`/bapi/composite/v1/public/cms/article/list/query`, catalogs
  93 Latest Activities · 128 Crypto Airdrop · 49 News) with an HTML-page fallback. This is **not an official
  API**; Binance may change or block it, especially from datacenter IPs. Article bodies are fetched and judged
  again because capital requirements live in the body.
* First run only records what already exists (no flood). Seen IDs persist in `exchange_events_state.json`.
* Headlines already forwarded from a Telegram channel (or vice-versa) are not sent twice.

**Check it works from your server first:**

```bash
python3 features/exchange_events.py            # list what each feed returns
python3 features/exchange_events.py --detail   # + first item's body
```

If Binance prints `HTTP 403/451`, run the app through `PROXY_URL` or turn the feeds off
(Settings → "Binance + Bybit website feeds").

## Diagnosing a missed event

1. Open Settings → **Why were messages skipped?** (or `GET /telegram/listener/recent?decision=skipped`).
   Each row shows the decision, reason, score, category and cost tier.
2. Paste the missed post into **Test filter** — it prints the score, cost words (required / mentioned / negated)
   and, optionally, the LLM's opinion.
3. Typical causes: `no_text` (image-only post), `capital action required`, `below confidence threshold`
   (lower *Minimum confidence*, or use LLM *Assist*), `excluded` (your exclude regex), `unresolved_sources`
   in `/telegram/listener/status` (the account isn't in that channel).

## Endpoints

| Route | Purpose |
|---|---|
| `GET /telegram/listener/status` | state, counters, LLM + feed health |
| `GET /telegram/listener/recent?n=50&decision=skipped` | why each message was forwarded/skipped |
| `POST /telegram/listener/test` `{text, use_llm}` | full analysis of a sample |
| `GET/POST /telegram/listener/exchange` | feed status / poll now |
| `POST /telegram/listener/bot/verify\|detect\|test` | check token / find your chat id / send a test alert |
| `POST /telegram/listener/reload` | re-apply settings |

## Settings keys

Existing: `tg_listener_enabled|sources|target|include_pattern|exclude_pattern|forward_prefix|detection_mode|min_score|cost_policy`
(`cost_policy` now also accepts `strict`). New: `tg_listener_llm_mode` (`off|assist|full`),
`tg_listener_llm_model`, `tg_listener_llm_base_url`, `tg_listener_llm_api_key`, `tg_listener_exchange_enabled`,
`tg_listener_delivery` (`account|bot`), `tg_listener_bot_token`, `tg_listener_bot_chat`.

## Tests

```bash
python -m unittest discover -s tests -v      # 53 tests, needs only `requests`
python -m compileall -q .
```

## Limits

Scores are prioritisation signals, not probabilities. "Free" means no *required* capital action was found in the
text; hidden costs, KYC/region limits and malicious links may still exist. Alerts are labelled UNVERIFIED. The tool
never opens links, connects wallets or claims anything.
