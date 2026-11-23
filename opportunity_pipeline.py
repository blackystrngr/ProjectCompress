"""One decision function shared by the Telegram listener and the
Binance/Bybit poller:  rules → (optional) LLM → final verdict.

Synchronous on purpose (the LLM call blocks); async callers run it in an
executor. Never raises — worst case it falls back to the rule engine.
"""

import re
import logging

from .crypto_opportunity_detector import analyze_message
from . import llm_classifier

logger = logging.getLogger(__name__)

LLM_MIN_CONFIDENCE = 60        # LLM must be at least this sure to promote / veto
LLM_VETO_CONFIDENCE = 80       # …and this sure to veto a rule-accepted message
LLM_FULL_PREFILTER = 15        # full mode: skip the LLM below this rule score


def _cost_gate(policy, cost, conf):
    if policy == "any":
        return True
    if cost == "free":
        return True
    # 'unclear' only passes (free policy) when the model is very sure it's an opportunity
    return policy == "free" and cost == "unclear" and conf >= 80


def evaluate(text, cfg, include_re=None, exclude_re=None, context_hint="", allow_llm=True):
    """→ dict(forward, reason, engine, analysis, llm)

    cfg keys: mode (intelligent|legacy), min_score, cost_policy,
              llm_mode (off|assist|full), llm_api_key/llm_model/llm_base_url
    """
    text = text or ""
    # Veto regex applies in every mode (matches the original behaviour)
    if exclude_re is not None and exclude_re.search(text):
        return {"forward": False, "reason": "excluded", "engine": "exclude", "analysis": None, "llm": None}

    if cfg.get("mode", "intelligent") == "legacy":
        ok = bool(include_re is None or include_re.search(text))
        return {"forward": ok, "reason": "legacy include match" if ok else "no_include",
                "engine": "legacy", "analysis": None, "llm": None}

    min_score = max(0, min(100, int(cfg.get("min_score", 45))))
    policy = cfg.get("cost_policy", "free")
    a = analyze_message(text, min_score=min_score, policy=policy, context_hint=context_hint)

    if a.get("hard_reject"):
        return {"forward": False, "reason": a["reason"], "engine": "rules", "analysis": a, "llm": None}

    llm_mode = cfg.get("llm_mode", "off")
    use_llm = (allow_llm and llm_mode in ("assist", "full") and llm_classifier.resolve_key(cfg)
               and ((llm_mode == "assist" and a.get("needs_review"))
                    or (llm_mode == "full" and (a.get("categories") or a.get("score", 0) >= LLM_FULL_PREFILTER))))
    if not use_llm:
        return {"forward": bool(a.get("accepted")), "reason": a.get("reason", ""),
                "engine": "rules", "analysis": a, "llm": None}

    v = llm_classifier.review(text, cfg, heuristic_hint=f"{a.get('reason','')} (score {a.get('score',0)}, cost {a.get('cost_tier')})")
    if v is None:                                   # LLM unavailable → rules decide
        return {"forward": bool(a.get("accepted")), "reason": a.get("reason", "") + " (llm unavailable)",
                "engine": "rules", "analysis": a, "llm": None}

    a = dict(a, llm=v)
    if v["scam_risk"] == "high":
        return {"forward": False, "reason": f"llm: scam risk high — {v['reason']}", "engine": "llm", "analysis": a, "llm": v}

    llm_yes = (v["is_opportunity"] and v["confidence"] >= LLM_MIN_CONFIDENCE
               and _cost_gate(policy, v["cost"], v["confidence"]))
    if a.get("accepted"):
        # rules said yes: only a confident LLM "no" overrides
        if (not v["is_opportunity"] or v["cost"] == "paid") and v["confidence"] >= LLM_VETO_CONFIDENCE and policy != "any":
            return {"forward": False, "reason": f"llm veto — {v['reason']}", "engine": "llm", "analysis": a, "llm": v}
        return {"forward": True, "reason": a["reason"] + " (llm agrees)" if llm_yes else a["reason"],
                "engine": "rules+llm", "analysis": a, "llm": v}
    if llm_yes:
        if v.get("reward") and not a.get("reward"):
            a["reward"] = v["reward"]
        if v.get("deadline") and not a.get("deadline"):
            a["deadline"] = v["deadline"]
        a["category"] = a.get("category") if a.get("category") not in (None, "unknown") else v["category"]
        return {"forward": True, "reason": f"llm promoted — {v['reason']}", "engine": "llm", "analysis": a, "llm": v}
    return {"forward": False, "reason": a.get("reason", "") + f" (llm: {v['reason'] or 'not an opportunity'})",
            "engine": "rules+llm", "analysis": a, "llm": v}
