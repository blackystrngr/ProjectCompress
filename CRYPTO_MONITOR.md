# Intelligent Telegram Crypto Opportunity Monitor

## What changed

The existing shared-session Telethon listener now supports two modes:

- `intelligent` (default): a dependency-free hybrid detector that combines opportunity-category patterns with contextual signals, cost checks, and wallet-phishing rejection.
- `legacy`: retains the previous include/exclude regex behavior.

The selected `free` cost policy forwards only messages with affirmative zero-cost signals (for example, testnet, faucet, free, or no-deposit language) and rejects messages that mention deposits, staking, liquidity provision, bridging, swaps, paid minting, trading-volume requirements, fees, or other capital requirements. Dangerous requests for seed phrases, recovery phrases, private keys, or payment-to-claim are rejected regardless of score.

This detector is intentionally local and makes no LLM/API calls. It adds no Python package dependencies. It is a heuristic signal detector, not a verification service: alerts are labelled UNVERIFIED and users must independently validate project identity, URLs, eligibility, and wallet interactions.

## Installation

1. Back up your current `settings.json` and project directory.
2. Copy the updated `features/` directory and `templates/base.html` into your installation (or deploy the supplied complete project archive).
3. No new pip dependencies are required. Existing Flask and Telethon dependencies remain required.
4. Restart ProjectCompress.
5. Open Settings → Announcement Listener.
6. Keep Detection engine set to **Intelligent hybrid**, Minimum confidence at **45**, and Opportunity cost policy at **Free-only**.
7. Configure source channels and a destination chat, enable the listener, and save.
8. Use **Test filter** with sample announcements before relying on live alerts.

## Settings keys

Existing keys remain supported:

- `tg_listener_enabled`
- `tg_listener_sources`
- `tg_listener_target`
- `tg_listener_include_pattern`
- `tg_listener_exclude_pattern`
- `tg_listener_forward_prefix`

New optional keys (automatically defaulted when absent):

- `tg_listener_detection_mode`: `intelligent` or `legacy` (default `intelligent`)
- `tg_listener_min_score`: integer 0–100 (default `45`)
- `tg_listener_cost_policy`: `free` or `any` (default `free`)

The existing include regex is used in legacy mode; the exclude regex remains an additional veto in both modes. Switching to legacy restores the previous include-regex gate.

## Validation

Run from the project root:

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

The status endpoint `/telegram/listener/status` reports the active mode, score threshold, cost policy, messages seen, opportunities detected/rejected, and forwarding counts. `/telegram/listener/test` returns the intelligent analysis alongside the legacy regex diagnostics.

## Limitations and safety

- No AI model or external API is used; the hybrid classifier is a transparent rule-and-context heuristic, not a general-purpose language model.
- Scores are prioritization signals, not probabilities of receiving tokens or financial returns.
- “Free” means the message did not match known paid/funded-action patterns and included a free/testnet/faucet signal. Hidden costs, gas requirements, eligibility restrictions, and malicious links may still exist.
- The detector does not open links, connect wallets, sign transactions, or claim rewards automatically.
- Keep the listener's source list limited to chats you are authorized to monitor.
