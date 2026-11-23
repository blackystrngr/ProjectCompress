"""Free-opportunity detector for Telegram / exchange announcements (v2).

Deterministic, dependency-free, no network calls. An optional LLM second
opinion lives in ``llm_classifier`` / ``opportunity_pipeline`` and is OFF by
default — this module never contacts anything.

What changed vs v1 (why v1 under-detected)
------------------------------------------
* v1 did not know the exchange-promo vocabulary the original include regex
  covered (learn & earn, quiz, red packet, word of the day, demo trading,
  crypto box ...), so those were silently dropped.
* v1 rejected a message if ANY cost word appeared anywhere ("no staking
  needed", "deposits are open", a footer ...). v2 judges cost per sentence,
  understands negation ("no deposit", "deposit-free", "not required") and
  separates *requirements* from mere *mentions*.
* v1 required a literal "free"/"no deposit". v2 also treats mechanisms that
  cost nothing by nature (quiz, learn & earn, testnet, faucet, social
  quests, demo trading, red packets) as zero-cost evidence.
* v2 extracts reward amount / deadline / platform, flags phishing and
  look-alike domains, and marks borderline messages ``needs_review`` so an
  optional LLM can look at just those.

Public API (unchanged): ``analyze_message``, ``format_alert``.
New: ``format_alert_html``, ``explain``.
"""

import html
import re
import unicodedata
from typing import Dict, List, Tuple

# ============================================================
# Mechanisms (what kind of opportunity is this?)
#   weight: how strongly the mechanism suggests a real opportunity (1-3)
#   free:   costs nothing by its nature (counts as zero-cost evidence)
# ============================================================
CATEGORIES: Dict[str, Dict] = {
    "learn_earn": {"weight": 3, "free": True, "patterns": [
        r"\blearn\s*(?:&|and|n|\+|to)?\s*earn\b", r"\bread\s*(?:&|and|to)\s*earn\b",
        r"\bwatch\s*(?:&|and|to)\s*earn\b", r"\bquiz\b", r"\bwotd\b",
        r"\bword of the day\b", r"\bcomplete (?:the |a )?(?:course|lesson|quiz)\b",
    ]},
    "red_packet": {"weight": 3, "free": True, "patterns": [
        r"\bred\s*packets?\b", r"\bcrypto\s*box(?:es)?\b", r"\bgift\s*code\b",
        r"\bcoupon\s*code\b", r"\bpromo\s*code\b",
    ]},
    "demo_trading": {"weight": 3, "free": True, "patterns": [
        r"\bdemo\s*(?:trading|championship|arena|challenge|contest|competition|tournament)\b",
        r"\bpaper\s*trading\s*(?:contest|competition)\b",
    ]},
    "airdrop": {"weight": 3, "free": False, "patterns": [
        r"\bair\s*-?drops?\b", r"\bretro-?drop\b", r"token distribution",
        r"claim (?:your |the |free )?(?:tokens?|airdrop|rewards?)", r"eligible wallets?",
        r"\bgiveaways?\b", r"\btge\b", r"token generation event",
        r"airdrop (?:checker|eligibility)", r"claim (?:page )?(?:is )?(?:now )?live",
    ]},
    "testnet": {"weight": 3, "free": True, "patterns": [
        r"\btest\s*-?net\b", r"\bdevnet\b", r"test network", r"incentivi[sz]ed test",
        r"\bpublic testnet\b",
    ]},
    "faucet": {"weight": 3, "free": True, "patterns": [
        r"\bfaucets?\b", r"free test(?:net)? (?:eth|tokens?|gas|funds)",
    ]},
    "quests": {"weight": 2, "free": True, "patterns": [
        r"\bquests?\b", r"campaign tasks?", r"complete (?:the |these |simple |all )?tasks?",
        r"\bsocial tasks?\b", r"\bgalxe\b", r"\bzealy\b", r"\blayer3\b", r"\btaskon\b",
        r"\bquest3\b", r"\bgleam\b", r"\bintract\b", r"\bquestn\b", r"\bcrew3\b",
        r"follow (?:us )?on (?:x|twitter)",
    ]},
    "points": {"weight": 2, "free": False, "patterns": [
        r"\bpoints? (?:program|campaign|season|farming|system)\b", r"earn(?:ing)? points",
        r"points (?:are|is) (?:now )?live", r"\bseason\s*\d+\b", r"points? multiplier",
        r"farm(?:ing)? points", r"\bxp\b (?:program|campaign|rewards?)",
    ]},
    "retroactive": {"weight": 2, "free": False, "patterns": [
        r"retroactive", r"early (?:users?|participants?|supporters?|adopters?)",
        r"\bsnapshot\b", r"past activity",
    ]},
    "referral": {"weight": 1, "free": False, "patterns": [
        r"referral (?:reward|bonus|program|campaign)", r"invite (?:your )?friends? (?:and|to) earn",
        r"\binvite code\b", r"refer (?:and|&) earn", r"\breferral\b",
    ]},
    "rebate": {"weight": 1, "free": False, "patterns": [
        r"fee rebate", r"trading fee cashback", r"fee refund", r"\bcashback\b",
    ]},
    "nft": {"weight": 2, "free": False, "patterns": [
        r"free mint", r"\bwhitelist\b", r"\ballow-?list\b", r"free nft", r"\bwl spots?\b", r"\bgtd\b",
    ]},
    "governance": {"weight": 1, "free": False, "patterns": [
        r"governance participation reward", r"vote (?:and|&|to) earn", r"delegate rewards?",
    ]},
    "exchange_promo": {"weight": 2, "free": False, "patterns": [
        r"\b(?:free|bonus)\s*(?:usdt|usdc|bnb|btc|eth|crypto|vouchers?|coupons?|tokens?)\b",
        r"\bno[- ]deposit\b", r"\bzero[- ]deposit\b", r"\bvouchers?\b", r"\bnew (?:user|member)s? (?:bonus|reward|gift|task|exclusive)",
        r"\b(?:complete|finish|pass) (?:kyc|identity verification|verification)\b", r"\bkyc (?:bonus|reward|campaign|task)\b",
        r"\bmystery\s*box(?:es)?\b", r"\btrading (?:voucher|bonus)\b", r"\bprize pool\b",
        r"\bshare (?:a |an |the )?(?:\$|[\d,.]+\s*(?:k|m)?\s*)?[\d,.]*\s*(?:usdt|usdc|bnb|usd)\b",
        r"\bspin\s*(?:&|and|to)\s*win\b", r"\blucky draw\b", r"\blottery\b",
    ]},
    "bounty": {"weight": 1, "free": False, "patterns": [
        r"\bbount(?:y|ies)\b", r"\bambassador\b", r"content (?:creator|contest)",
        r"writing contest", r"\bmeme contest\b",
    ]},
    "beta_access": {"weight": 1, "free": False, "patterns": [
        r"\bwaitlist\b", r"\bearly access\b", r"\bbeta test(?:ers?|ing)?\b", r"\bclosed beta\b",
    ]},
    # Added automatically when the caller knows the source is an exchange
    # "activities" page (see ``context_hint``).
    "exchange_activity": {"weight": 2, "free": False, "patterns": []},
}

CATEGORY_LABELS = {
    "learn_earn": "Learn & Earn", "red_packet": "Red Packet / Crypto Box", "demo_trading": "Demo Trading",
    "airdrop": "Airdrop", "testnet": "Testnet", "faucet": "Faucet", "quests": "Quests",
    "points": "Points Program", "retroactive": "Retroactive", "referral": "Referral", "rebate": "Fee Rebate",
    "nft": "NFT / Whitelist", "governance": "Governance", "exchange_promo": "Exchange Promo",
    "bounty": "Bounty", "beta_access": "Beta / Waitlist", "exchange_activity": "Exchange Campaign",
}


def _label(category: str) -> str:
    return CATEGORY_LABELS.get(category, (category or "unknown").replace("_", " ").title())


PLATFORMS = ("galxe", "zealy", "layer3", "taskon", "quest3", "gleam", "intract", "questn",
             "crew3", "binance", "bybit", "okx", "bitget", "kucoin", "gate.io", "mexc",
             "coinbase", "kraken", "htx", "bingx")

# ============================================================
# Positive context
# ============================================================
POSITIVE = {
    "reward": (r"\brewards?\b", r"\bearn\b", r"\bclaim\b", r"\bshare\b", r"\bprize", r"\bwin\b",
               r"\bbonus\b", r"\bvouchers?\b", r"\ballocation\b", r"distribution", r"\bincentive"),
    "participation": (r"\bparticipate\b", r"\bcomplete\b", r"\bjoin\b", r"\bregister\b",
                      r"\bsign\s*up\b", r"\bfollow\b", r"\bsubmit\b", r"\banswer\b",
                      r"\btake part\b", r"\binteract\b", r"\bconnect\b"),
    "timing": (r"\bsnapshot\b", r"\bdeadline\b", r"\bends?\b", r"\bseason\b", r"\bnow live\b",
               r"\blimited\b", r"\bfirst come\b", r"\bwhile (?:supplies|spots) last\b"),
}

# Literal zero-cost statements ("explicit"): add confidence AND satisfy 'strict'.
FREE_EXPLICIT = (
    r"\bfree\b", r"\bno (?:deposit|purchase|cost|fees?|payment|investment|stake|staking|capital|trading)\b",
    r"\bzero[- ](?:deposit|cost|fees?)\b", r"\bwithout (?:any )?(?:a )?(?:deposit|purchase|cost|payment|investing|staking|trading)\b",
    r"\bat no (?:cost|charge)\b", r"\b(?:deposit|cost|fee)[- ]free\b", r"\b100% free\b",
    r"\bno\b[^.\n]{0,25}\brequired\b", r"\bnothing to (?:pay|deposit|buy)\b",
)

# ============================================================
# Cost / capital requirements  (judged per sentence)
# ============================================================
COST_TYPES: Dict[str, Tuple[str, ...]] = {
    "deposit": (r"\bdeposit(?:s|ing|ed)?\b",),
    "stake": (r"\bstak(?:e|es|ing|ed)\b", r"\block(?:ing|ed|up)?\b[^.\n]{0,25}\b(?:bnb|tokens?|funds?|usdt|eth)\b"),
    "liquidity": (r"provide liquidity", r"add liquidity", r"liquidity pool", r"\blp tokens?\b"),
    "bridge": (r"\bbridg(?:e|es|ing|ed)\b",),
    "trade_volume": (r"trading volume", r"\btrade (?:at least|a minimum|\$|\d)", r"\bspot trad(?:e|ing)\b[^.\n]{0,20}\bvolume\b",
                     r"\bfutures trad(?:e|ing)\b", r"\bleverage\b", r"\bperpetual\b"),
    "swap": (r"\bswap(?:s|ping|ped)?\b",),
    "purchase": (r"\bbuy(?:ing)?\b", r"\bpurchas(?:e|es|ing|ed)\b"),
    "hold": (r"\bhold(?:ing)? (?:at least|a minimum|\d|\$)", r"minimum balance", r"\bholders? of\b",
             r"\bhodler\b", r"\bsimple earn\b", r"\bsubscri(?:be|bed|bing|ption) (?:bnb|usdt|usdc|to (?:simple|earn|flexible|locked))"),
    "locked_product": (r"\bhodler\b", r"\bsimple earn\b", r"\blaunchpool\b", r"\bmegadrop\b", r"\blaunchpad\b"),
    "fees": (r"\bgas fees?\b", r"\btransaction fees?\b", r"\bnetwork fees?\b", r"\bpay (?:a )?(?:small )?fee\b"),
    "paid_entry": (r"\bmint (?:price|fee|cost)\b", r"\bentry fee\b", r"\bsubscription\b",
                   r"\bregistration fee\b", r"\bminimum investment\b", r"\binvest(?:ing)? (?:at least|\$|\d)"),
}

NEGATION_BEFORE = re.compile(
    r"(?:\bno\b|\bnot\b|\bnever\b|\bwithout\b|\bzero\b|\bnothing\b|\bfree of\b|\bneed not\b|"
    r"\bno need to\b|\bdon'?t\b|\bdoesn'?t\b|\bwon'?t\b|\bisn'?t\b|\baren'?t\b|\bwithout any\b|"
    r"\binstead of\b|\bavoid\b|\bneither\b|\bnor\b)(?:\W+(?!but\b|however\b|except\b|unless\b|only\b)\w+){0,5}\W*$"
)
NEGATION_AFTER = re.compile(
    r"^\W*(?:is |are |will be |be )?(?:not (?:required|needed|necessary|mandatory)|"
    r"isn'?t (?:required|needed)|aren'?t (?:required|needed)|not a requirement|free\b|-free\b|"
    r"is optional|optional\b)"
)
# "- Deposit 50 USDT" style line (imperative, with a number) = a requirement
IMPERATIVE_COST = re.compile(
    r"^\W*(?:deposit|stake|trade|buy|hold|swap|bridge|lock|invest|purchase)\b[^.\n]{0,40}(?:\d|\$)")
REQUIREMENT_CUES = re.compile(
    r"\b(?:must|needs? to|needed|required|requires?|at least|minimum|min\.|to qualify|to be eligible|"
    r"eligible (?:if|when|users?)|in order to|have to|only users who|users who|you (?:will )?need|"
    r"mandatory|prerequisite)\b"
)
# "deposit 100 USDT to get / and receive ..." → the capital action IS the price of entry
ACTION_TO_REWARD = re.compile(
    r"\b(?:deposit|stake|trade|buy|hold|swap|bridge|lock|invest|purchase|subscribe)\w*\b[^.!?\n]{0,70}"
    r"\b(?:to (?:get|receive|earn|share|unlock|win|qualify|participate|be eligible|claim)|"
    r"and (?:get|receive|earn|share|win|unlock))\b"
)

DANGEROUS = (
    r"seed\s*phrase", r"recovery\s*phrase", r"private\s*key", r"\bmnemonic\b", r"secret\s*phrase",
    r"connect (?:your )?wallet to (?:verify|unlock|validate|sync|rectify|restore)",
    r"(?:verify|validate|sync|rectify) your wallet", r"wallet (?:is )?(?:at risk|compromised|suspended)",
    r"\bsend\s+(?:[\w.$,]+\s+){0,4}to\s+(?:claim|receive|unlock|verify|release)\b",
    r"pay (?:a )?(?:small )?(?:fee|gas|tax) to (?:claim|receive|unlock|withdraw|release)",
    r"double your (?:crypto|bitcoin|btc|eth|usdt|money|funds)", r"guaranteed (?:profit|returns?|income)",
    r"\b100% (?:profit|guaranteed|safe returns?)\b", r"dm (?:me|us) (?:to|for) (?:claim|receive|get)",
    r"contact (?:our )?(?:support|admin|agent) (?:on|via|at) (?:telegram|whatsapp)",
)

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>)\]]+", re.IGNORECASE)
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "cutt.ly", "shorturl.at", "rb.gy",
              "is.gd", "ow.ly", "tiny.cc", "rebrand.ly", "s.id", "lnkd.in"}
OFFICIAL = {
    "binance": re.compile(r"(?:^|\.)(?:binance\.(?:com|us|info|org|me|th|jp|sg)|bnbstatic\.com|binance\.vision|bnbchain\.org)$"),
    "bybit": re.compile(r"(?:^|\.)(?:bybit\.(?:com|eu|kz|nl)|bybitglobal\.com|bytick\.com)$"),
    "okx": re.compile(r"(?:^|\.)okx\.com$"),
    "coinbase": re.compile(r"(?:^|\.)coinbase\.com$"),
    "kucoin": re.compile(r"(?:^|\.)kucoin\.com$"),
    "bitget": re.compile(r"(?:^|\.)bitget\.com$"),
    "kraken": re.compile(r"(?:^|\.)kraken\.com$"),
}

AMOUNT_RE = re.compile(
    r"(?:(?P<cur1>\$|usd\s?|€|£)\s?(?P<n1>\d[\d,]*(?:\.\d+)?)\s?(?P<mult1>k|m|b|million|billion|thousand)?\b)"
    r"|(?:(?P<n2>\d[\d,]*(?:\.\d+)?)\s?(?P<mult2>k|m|b|million|billion|thousand)?\s?"
    r"(?P<cur2>usdt|usdc|usd|busd|bnb|btc|eth|sol|matic|ton|arb|op|tokens?|coins?|points?|xp)\b)",
    re.IGNORECASE)
MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
DEADLINE_RE = re.compile(
    r"(?:until|till|ends?(?: on| in| at)?|deadline(?: is)?|before|expires?(?: on| in)?|valid (?:until|through)|"
    r"closes? (?:on|in)|within|by)\s+([^.!?\n]{3,45})", re.IGNORECASE)
DATE_HINT = re.compile(
    r"\d{1,2}[/\-.]\d{1,2}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b\d+\s*(?:hours?|days?|weeks?|hrs?)\b|\btoday\b|\btomorrow\b|\b20\d\d\b|\bmidnight\b|\butc\b",
    re.IGNORECASE)


# ============================================================
# Helpers
# ============================================================
def _normalize(message: str) -> str:
    text = unicodedata.normalize("NFKC", message or "")
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    text = text.replace("&amp;", "&").replace("’", "'").replace("‘", "'")
    text = re.sub(r"[*_~`]{1,3}", "", text)          # markdown noise
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+|[•▪●✅❌🔹🔸▶►→➡️]+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _hits(text: str, patterns) -> List[str]:
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]


def _domain(url: str) -> str:
    d = re.sub(r"^(?:https?://)?(?:www\.)?", "", url.strip(), flags=re.IGNORECASE)
    return re.split(r"[/?#:]", d, 1)[0].lower().rstrip(".,;")


def _link_risks(text: str) -> Tuple[List[str], bool]:
    """→ (flags, hard_reject). Look-alike brand domains are treated as phishing."""
    flags, hard = [], False
    for url in URL_RE.findall(text):
        dom = _domain(url)
        if not dom:
            continue
        if dom in SHORTENERS:
            flags.append("url_shortener")
        for brand, official in OFFICIAL.items():
            if brand in dom and not official.search(dom):
                flags.append(f"lookalike_domain:{dom}")
                hard = True
        if re.search(r"\.(?:xyz|top|click|icu|cfd|shop|work|support)$", dom) and \
                any(b in dom for b in OFFICIAL):
            hard = True
    return sorted(set(flags)), hard


def _cost_scan(sentences: List[str]) -> Dict[str, List[str]]:
    """Classify each cost-word occurrence as required / mentioned / negated."""
    out = {"required": [], "mentioned": [], "negated": []}
    for sent in sentences:
        low = sent.lower()
        action_to = bool(ACTION_TO_REWARD.search(low) or IMPERATIVE_COST.search(low))
        req_cue = bool(REQUIREMENT_CUES.search(low))
        for ctype, patterns in COST_TYPES.items():
            for pat in patterns:
                for m in re.finditer(pat, low):
                    before = low[max(0, m.start() - 45):m.start()]
                    after = low[m.end():m.end() + 40]
                    negated = bool(NEGATION_BEFORE.search(before)) or bool(NEGATION_AFTER.search(after))
                    if negated:
                        bucket = "negated"
                    elif req_cue or action_to or ctype == "locked_product":
                        bucket = "required"
                    else:
                        bucket = "mentioned"
                    out[bucket].append(ctype)
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


def _extract_reward(text: str) -> str:
    best, best_val = "", 0.0
    for m in AMOUNT_RE.finditer(text):
        raw = (m.group("n1") or m.group("n2") or "").replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        mult = (m.group("mult1") or m.group("mult2") or "").lower()
        val *= MULT.get(mult, 1)
        cur = (m.group("cur2") or m.group("cur1") or "").strip().upper()
        if cur in ("POINT", "POINTS", "XP"):
            val *= 0.001                      # points rank below real money
        if val > best_val:
            best_val = val
            shown = (m.group("n1") or m.group("n2"))
            best = f"{'$' if m.group('cur1') in ('$',) else ''}{shown}{(' ' + mult.upper()) if mult in ('k','m','b') else (' ' + mult) if mult else ''}" \
                   f"{(' ' + cur) if cur and m.group('cur1') != '$' else ''}".strip()
    return best


def _extract_deadline(text: str) -> str:
    for m in DEADLINE_RE.finditer(text):
        snippet = m.group(1).strip(" ,;:-")
        if DATE_HINT.search(snippet):
            return snippet[:45]
    return ""


# ============================================================
# Main API
# ============================================================
def analyze_message(message: str, min_score: int = 45, policy: str = "free",
                    context_hint: str = "") -> Dict:
    """Classify one announcement.

    policy:
      ``free``    reject only when a capital action is *required* (default)
      ``strict``  additionally need an explicit zero-cost statement and no
                  un-negated cost mentions (v1 behaviour)
      ``any``     ignore cost, still reject scams
    context_hint: optional source info (e.g. an exchange category such as
      "Latest Activities") that supplies a weak "this is a campaign" signal.

    Returns a JSON-safe dict. ``accepted`` means worth alerting — never that
    the opportunity is verified.
    """
    policy = policy if policy in ("free", "strict", "any") else "free"
    text = _normalize(message)
    if not text:
        return {"accepted": False, "score": 0, "category": "unknown", "reason": "empty message",
                "risk_flags": [], "cost_policy": policy, "cost_tier": "unstated", "needs_review": False}
    lower = text.lower()
    sentences = _sentences(text)

    # ---- hard safety gates (never overridden, never sent to an LLM) ----
    if _hits(lower, DANGEROUS):
        return {"accepted": False, "score": 0, "category": "suspicious", "cost_policy": policy,
                "reason": "credential / payment-to-claim request (phishing pattern)",
                "risk_flags": ["credential_or_payment_phishing"], "cost_tier": "unstated",
                "needs_review": False, "hard_reject": True}
    link_flags, link_hard = _link_risks(text)
    if link_hard:
        return {"accepted": False, "score": 0, "category": "suspicious", "cost_policy": policy,
                "reason": "look-alike / suspicious domain impersonating an exchange",
                "risk_flags": link_flags, "cost_tier": "unstated", "needs_review": False,
                "hard_reject": True}

    # ---- mechanism detection ----
    cats = {}
    for name, spec in CATEGORIES.items():
        hits = _hits(lower, spec["patterns"])
        if hits:
            cats[name] = hits
    hint = (context_hint or "").lower()
    if hint and any(h in hint for h in ("activit", "airdrop", "campaign", "promotion", "reward",
                                        "competition", "giveaway", "launchpool", "megadrop")):
        cats.setdefault("exchange_activity", ["context_hint"])

    cost = _cost_scan(sentences)
    explicit_free = bool(_hits(lower, FREE_EXPLICIT))
    nature_free = any(CATEGORIES[c]["free"] for c in cats)

    if cost["required"]:
        cost_tier = "required"
    elif cost["mentioned"]:
        cost_tier = "mentioned"
    elif explicit_free or nature_free:
        cost_tier = "free"
    else:
        cost_tier = "unstated"

    base = {"cost_policy": policy, "cost_tier": cost_tier,
            "cost_evidence": cost, "link_flags": link_flags}

    if not cats:
        base.update({"accepted": False, "score": 0, "category": "unknown", "needs_review": False,
                     "reason": "no opportunity concept detected", "risk_flags": link_flags})
        return base

    # ---- scoring ----
    weights = sorted((CATEGORIES[c]["weight"] for c in cats), reverse=True)
    mech = weights[0] * 8 + sum(weights[1:3]) * 3
    score = 10 + min(36, mech)
    evidence = [f"category:{c}" for c in sorted(cats)]
    for group, patterns in POSITIVE.items():
        if _hits(lower, patterns):
            score += {"reward": 10, "participation": 8, "timing": 3}[group]
            evidence.append(group)
    if explicit_free:
        score += 14
        evidence.append("explicit_free")
    elif nature_free:
        score += 8
        evidence.append("free_by_nature")
    if cost["negated"]:
        score += 4
        evidence.append("negated_cost:" + ",".join(cost["negated"]))
    reward = _extract_reward(text)
    if reward:
        score += 8
        evidence.append(f"reward:{reward}")
    deadline = _extract_deadline(text)
    if deadline:
        score += 4
        evidence.append("deadline")
    platforms = sorted({p for p in PLATFORMS if p in lower})
    if platforms:
        score += 4
    if URL_RE.search(text) or "t.me/" in lower:
        score += 3
        evidence.append("link_present")
    risk_flags = list(link_flags)
    if "url_shortener" in link_flags:
        score -= 6
    if cost_tier == "mentioned":
        score -= min(24, 8 * len(cost["mentioned"]))
    score = max(0, min(100, score))

    category = max(cats, key=lambda c: (CATEGORIES[c]["weight"], len(cats[c])))
    result = dict(base, category=category, categories=sorted(cats), score=score, evidence=evidence,
                  reward=reward, deadline=deadline, platforms=platforms, risk_flags=risk_flags,
                  verification="unverified; independently verify the project and URL")

    # ---- decision ----
    reason, accepted = "potential free opportunity", score >= int(min_score)
    if policy == "free" and cost_tier == "required":
        accepted, reason = False, "capital action required: " + ", ".join(cost["required"])
        result["risk_flags"] = risk_flags + ["financial_requirement"]
    elif policy == "strict":
        if cost_tier in ("required", "mentioned"):
            accepted, reason = False, "strict policy: cost mentioned (" + ", ".join(cost["required"] + cost["mentioned"]) + ")"
            result["risk_flags"] = risk_flags + ["financial_requirement"]
        elif not explicit_free:
            accepted, reason = False, "strict policy: no explicit zero-cost statement"
            result["risk_flags"] = risk_flags + ["cost_not_established"]
    if accepted is False and reason == "potential free opportunity":
        reason = "below confidence threshold"

    # borderline → candidates for the optional LLM second opinion
    gap = int(min_score) - score
    uncertain_cost = cost_tier in ("required", "mentioned") and policy != "any" and \
        (bool(cost["negated"]) or explicit_free or nature_free)
    needs_review = (not accepted) and (
        (0 < gap <= 25) or uncertain_cost or (policy == "strict" and score >= int(min_score)))
    result.update(accepted=accepted, reason=reason, needs_review=needs_review,
                  tier=("high" if score >= 70 else "medium" if score >= 45 else "low"))
    return result


def explain(analysis: Dict) -> str:
    """One-line human summary (used by the settings test button)."""
    bits = [f"score {analysis.get('score', 0)}/100", analysis.get("category", "?"),
            f"cost: {analysis.get('cost_tier', '?')}"]
    if analysis.get("reward"):
        bits.append(f"reward {analysis['reward']}")
    if analysis.get("deadline"):
        bits.append(f"until {analysis['deadline']}")
    bits.append(analysis.get("reason", ""))
    return " · ".join(b for b in bits if b)


def _cost_note(analysis: Dict) -> str:
    tier = analysis.get("cost_tier")
    ev = analysis.get("cost_evidence") or {}
    if tier == "free":
        return "✅ no capital requirement detected"
    if tier == "mentioned":
        return "⚠️ mentions " + ", ".join(ev.get("mentioned", [])) + " — check it isn't required"
    if tier == "required":
        return "💰 requires " + ", ".join(ev.get("required", []))
    return "❔ cost not stated — verify before acting"


def format_alert(message: str, analysis: Dict, source: str, link: str, max_chars: int = 3500) -> str:
    lines = ["🧭 CRYPTO OPPORTUNITY SIGNAL",
             f"Category: {_label(analysis.get('category', 'unknown'))}",
             f"Confidence score: {analysis.get('score', 0)}/100", f"Source: {source}",
             f"Cost: {_cost_note(analysis)}"]
    if analysis.get("reward"):
        lines.append(f"Reward: {analysis['reward']}")
    if analysis.get("deadline"):
        lines.append(f"Deadline: {analysis['deadline']}")
    if analysis.get("llm", {}).get("summary"):
        lines.append(f"AI summary: {analysis['llm']['summary']}")
    lines.append("Status: UNVERIFIED — do not trust links or connect a wallet without checking")
    if analysis.get("categories"):
        lines.append("Signals: " + ", ".join(analysis["categories"]))
    if link:
        lines.append("Source message: " + link)
    lines.extend(["", "Original message:", (message or "")[:max_chars]])
    return "\n".join(lines)


def format_alert_html(message: str, analysis: Dict, source: str, link: str,
                      prefix: str = "", max_chars: int = 3000) -> str:
    """Telegram-HTML version: bold headline, reward/deadline up front."""
    e = lambda v: html.escape(str(v), quote=False)   # Bot API HTML needs only & < >
    tier = {"high": "🟢", "medium": "🟡", "low": "🟠"}.get(analysis.get("tier", "medium"), "🟡")
    cat = _label(analysis.get("category", "unknown"))
    out = []
    if prefix:
        out.append(f"<b>{e(prefix)}</b>")
    out.append(f"{tier} <b>{e(cat)}</b> · score <b>{analysis.get('score', 0)}</b>/100")
    out.append(f"📡 <i>{e(source)}</i>")
    if analysis.get("reward"):
        out.append(f"🎁 <b>{e(analysis['reward'])}</b>")
    if analysis.get("deadline"):
        out.append(f"⏳ until <b>{e(analysis['deadline'])}</b>")
    out.append(e(_cost_note(analysis)))
    if analysis.get("llm", {}).get("summary"):
        out.append(f"🤖 <i>{e(analysis['llm']['summary'])}</i>")
    out.append("")
    body = (message or "")[:max_chars]
    out.append(f"<blockquote expandable>{e(body)}</blockquote>" if len(body) > 280 else e(body))
    if link:
        out.append(f"\n🔗 {e(link)}")
    out.append("\n<i>⚠️ UNVERIFIED — check the project and URL; never connect a wallet blindly.</i>")
    return "\n".join(out)[:4000]
