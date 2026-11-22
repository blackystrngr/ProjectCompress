"""Free, dependency-free hybrid detector for Telegram crypto opportunities.

Hybrid here means deterministic phrase/rule signals combined with contextual
semantic token scoring. It deliberately makes no network/LLM calls and never
claims that an opportunity or project is legitimate.
"""

import re
from typing import Dict, List

CATEGORIES = {
    "airdrop": [r"\bairdrop\b", r"token distribution", r"claim (?:your )?tokens?", r"eligible wallets?", r"\bgiveaway\b"],
    "testnet": [r"\btestnet\b", r"test network", r"testnet faucet"],
    "points": [r"\bpoints? (?:program|campaign|season|farming)\b", r"earn points", r"points are live"],
    "retroactive": [r"retroactive", r"early (?:users?|participants?)", r"snapshot", r"past activity"],
    "faucet": [r"\bfaucet\b", r"free test(?:net)? (?:eth|token|gas)"],
    "quests": [r"\bquests?\b", r"campaign tasks", r"complete (?:the )?tasks", r"\blearn[- ]to[- ]earn\b"],
    "referral": [r"referral (?:reward|bonus|program)", r"invite friends? and earn", r"invite code"],
    "rebate": [r"fee rebate", r"trading fee cashback", r"fee refund"],
    "nft": [r"free mint", r"\bwhitelist\b", r"allowlist", r"free nft"],
    "governance": [r"governance participation reward", r"vote and earn", r"delegate rewards"],
}

POSITIVE = {
    "reward": (r"reward", r"earn", r"claim", r"distribution", r"incentive", r"allocation", r"bonus"),
    "participation": (r"participate", r"interact", r"complete", r"early user", r"activity", r"quest", r"task"),
    "free": (r"free", r"no deposit", r"without deposit", r"zero cost", r"no purchase", r"faucet"),
    "timing": (r"snapshot", r"deadline", r"before the", r"season", r"campaign", r"now live"),
}

# Conservative policy: if a message indicates that funds or a paid action are
# needed, do not forward it under the free-only policy.
COST_RISKS = (
    r"(?<!no )(?<!without )\bdeposit\b", r"\bstake\b", r"staking", r"provide liquidity", r"liquidity pool",
    r"\bbridge\b", r"swap tokens?", r"buy (?:the )?token", r"(?<!no )(?<!or )purchase", r"trading volume",
    r"trade (?:at least|\$|\d)", r"pay (?:a )?gas", r"gas fee", r"transaction fee",
    r"minimum investment", r"lock(?:up)? (?:your )?funds", r"hold at least",
    r"mint price", r"entry fee", r"subscription",
)

DANGEROUS = (
    r"seed phrase", r"recovery phrase", r"private key", r"enter your mnemonic",
    r"connect wallet to (?:verify|unlock) (?:your )?(?:account|wallet)",
    r"send (?:crypto|eth|btc|usdt) to (?:claim|receive|unlock)",
)


def _hits(text: str, patterns) -> List[str]:
    found = []
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(pattern)
    return found


def analyze_message(message: str, min_score: int = 45, policy: str = "free") -> Dict:
    """Return a JSON-safe classification. `accepted` means worth alerting, not verified."""
    text = (message or "").strip()
    lower = text.lower()
    if not text:
        return {"accepted": False, "score": 0, "category": "unknown", "reason": "empty message", "risk_flags": []}

    risk_flags = []
    dangerous = _hits(lower, DANGEROUS)
    if dangerous:
        return {"accepted": False, "score": 0, "category": "suspicious", "reason": "dangerous wallet credential/payment request", "risk_flags": ["credential_or_payment_phishing"]}

    cost_hits = _hits(lower, COST_RISKS)
    if policy == "free" and cost_hits:
        return {"accepted": False, "score": 0, "category": "paid_or_capital_required", "reason": "free-only policy rejected a message mentioning a funded or paid action", "risk_flags": ["financial_requirement"]}

    category_hits = {name: _hits(lower, patterns) for name, patterns in CATEGORIES.items()}
    category_hits = {name: hits for name, hits in category_hits.items() if hits}
    if not category_hits:
        return {"accepted": False, "score": 0, "category": "unknown", "reason": "no opportunity concept detected", "risk_flags": []}

    # Phrase matches identify a mechanism; contextual concepts distinguish a
    # real opportunity from generic discussion about crypto.
    score = min(35, 12 + 8 * len(category_hits))
    evidence = [f"category:{name}" for name in category_hits]
    for group, patterns in POSITIVE.items():
        hits = _hits(lower, patterns)
        if hits:
            score += {"reward": 18, "participation": 14, "free": 20, "timing": 5}[group]
            evidence.append(group)
    if re.search(r"https?://|t\.me/", lower):
        score += 3
        evidence.append("link_present")
    if re.search(r"\b(testnet|faucet|free mint|no deposit|zero cost)\b", lower):
        score += 8
        evidence.append("explicit_low_cost_signal")
    score = min(100, score)

    # Free-only means affirmative evidence of a no-purchase path. Testnet and
    # faucet actions count as non-monetary by default; otherwise require an
    # explicit free/no-deposit/no-purchase statement.
    free_evidence = bool(_hits(lower, (r"\bfree\b", r"no deposit", r"without deposit", r"zero cost", r"no purchase", r"testnet", r"\bfaucet\b", r"free mint")))
    if policy == "free" and not free_evidence:
        return {"accepted": False, "score": score, "category": sorted(category_hits)[0], "reason": "no clear zero-cost participation evidence", "evidence": evidence, "risk_flags": ["cost_not_established"]}

    category = max(category_hits, key=lambda name: len(category_hits[name]))
    accepted = score >= int(min_score)
    return {
        "accepted": accepted,
        "score": score,
        "category": category,
        "categories": sorted(category_hits),
        "reason": "potential free opportunity" if accepted else "below confidence threshold",
        "evidence": evidence,
        "risk_flags": risk_flags,
        "cost_policy": policy,
        "cost_established": free_evidence,
        "verification": "unverified; independently verify the project and URL",
    }


def format_alert(message: str, analysis: Dict, source: str, link: str, max_chars: int = 3500) -> str:
    policy_label = "free-only" if analysis.get("cost_policy", "free") == "free" else "all cost levels"
    lines = ["🧭 CRYPTO OPPORTUNITY SIGNAL", f"Category: {analysis.get('category', 'unknown').replace('_', ' ').title()}",
             f"Confidence score: {analysis.get('score', 0)}/100", f"Source: {source}",
             f"Cost filter: {policy_label}", "Status: UNVERIFIED — do not trust links or connect a wallet without checking"]
    if analysis.get("categories"):
        lines.append("Signals: " + ", ".join(analysis["categories"]))
    if link:
        lines.append("Source message: " + link)
    lines.extend(["", "Original message:", (message or "")[:max_chars]])
    return "\n".join(lines)
