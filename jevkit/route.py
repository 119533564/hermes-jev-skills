"""Pick the cheapest model that is good enough for this turn.

Jev answers three things in one request: how hard the turn is, what kind of work
it is, and whether a mistake would be costly. Code, not Jev, turns that into a
model: it walks the pool for that tier and specialty and takes the first model
that fits the turn's hard requirements (images, context size). Every unsure or
failed path keeps the model you were already on. Routing never blocks a turn.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import catalog as catalog_mod
from . import client, privacy

TIERS = ("simple", "medium", "hard")
SPECIALTIES = ("general", "coding", "writing", "research", "vision")
POLICY_VERSION = "route-2"

DIFFICULTY = [
    "Trivial or mechanical: a lookup, reformat, rename, short factual reply, or a single obvious step",
    "Routine: ordinary multi-step work with a clear path and low ambiguity",
    "Substantial: needs planning, several interacting parts, debugging, or careful judgment",
    "Expert: subtle, ambiguous, or high-stakes; architecture, security, concurrency, data migration, legal or money",
]
KIND = {
    "coding": "Writing, changing, debugging or reviewing software, scripts, configs or shell commands",
    "writing": "Drafting or editing prose, marketing, messages, documents or creative text",
    "research": "Finding, comparing or synthesizing information, analysis, or current events",
    "general": "Conversation, planning, operations, or anything that is none of the others",
}

# Short prompts can still be dangerous. These never route to the cheapest tier.
_HARD_RISK = re.compile(
    r"(?i)\b(prod(uction)?|deploy|migrat\w+|rollback|drop\s+table|delete|rm\s+-rf|force[- ]push|secur\w+|"
    r"vulnerab\w+|auth(entication|orization)?|encrypt\w*|concurren\w+|race condition|deadlock|payment|refund|"
    r"invoice|stripe|billing|legal|contract|lawsuit|medical|diagnos\w+|customer data|pii|dns|certificate)\b"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "redacted-text",          # or "features": no prompt text ever leaves the machine
    "min_confidence": 0.6,
    "simple_needs_confidence": 0.85,
    "hard_needs_probability": 0.6,    # P(substantial or expert) needed before paying for the hard tier
    "simple_needs_probability": 0.7,  # P(trivial) needed before dropping to the cheapest tier
    "ask_chars": 2500,                # how much of a long turn Jev reads: the opening and, mostly, the end
    # Turns that are a template wrapped around work Jev cannot see. Routing them is a coin toss, so they keep
    # the model their profile or job was configured with.
    "skip_prefixes": ["[kanban]", "[SESSION HANDOFF", "[cron]", "[scheduled]"],
    "skip_session_prefixes": ["cron"],
    "sticky_context_tokens": 32000,   # above this, do not downgrade: the cache rebuild costs more than it saves
    "exclude": ["*:free", "cloudflare-ai-gateway:*"],
    "private_profiles": [],
    "tiers": {},                      # {"simple": {"general": ["provider:model", ...], "coding": [...]}, ...}
}


def config_paths() -> List[Path]:
    """Least specific first. The shared file is the default for every Hermes profile; a profile's own file overrides it."""
    override = os.environ.get("JEV_ROUTING_CONFIG")
    if override:
        return [Path(override)]
    paths = [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jev" / "routing.json"]
    if catalog_mod.hermes_root().is_dir():
        paths.append(catalog_mod.hermes_root() / "jev" / "routing.json")
        if catalog_mod.hermes_home() != catalog_mod.hermes_root():
            paths.append(catalog_mod.hermes_home() / "jev" / "routing.json")
    return paths


def config_path() -> Path:
    """Where `jev models suggest --write` saves: the most specific location that applies."""
    return config_paths()[-1]


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for candidate in ([path] if path else config_paths()):
        try:
            layer = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tiers = {**config.get("tiers", {}), **(layer.get("tiers") or {})}   # a profile may override one tier only
        config.update(layer)
        config["tiers"] = tiers
    return config


# ── pools ────────────────────────────────────────────────────────────────────

def _ref(row: Dict[str, Any]) -> str:
    return f"{row['provider']}:{row['model']}"


def _excluded(ref: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(ref, pattern) or fnmatch.fnmatch(ref.split(":", 1)[1], pattern) for pattern in patterns)


def suggest_tiers(rows: List[Dict[str, Any]], exclude: List[str], per_pool: int = 6) -> Dict[str, Dict[str, List[str]]]:
    """A starting point from price bands. Pin your own picks in routing.json."""
    bands = {"simple": (0.0, 0.6), "medium": (0.6, 3.2), "hard": (3.2, 1e9)}
    usable = [r for r in rows if not _excluded(_ref(r), exclude) and not r["model"].startswith("~") and r["price"] > 0]
    out: Dict[str, Dict[str, List[str]]] = {}
    for tier, (low, high) in bands.items():
        pool = sorted((r for r in usable if low <= r["price"] < high), key=lambda r: r["released"], reverse=True)
        out[tier] = {
            "general": [_ref(r) for r in pool[:per_pool]],
            "vision": [_ref(r) for r in pool if r["vision"]][:per_pool],
        }
    return out


def _pick(config: Dict[str, Any], rows: Dict[str, Dict[str, Any]], tier: str, specialty: str,
          need_vision: bool, context_tokens: int, only_provider: Optional[str] = None) -> Optional[str]:
    tiers = config.get("tiers") or {}
    order = [tier] + [t for t in TIERS[TIERS.index(tier):] if t != tier]  # never fall DOWN a tier
    for candidate_tier in order:
        pools = tiers.get(candidate_tier) or {}
        for name in (("vision",) if need_vision else ()) + (specialty, "general"):
            for ref in pools.get(name) or []:
                if _excluded(ref, config.get("exclude") or []):
                    continue
                if only_provider and ref.split(":", 1)[0] != only_provider:
                    continue          # a plugin can swap the model, not the provider it is already connected to
                row = rows.get(ref)
                if row is None:          # pinned by the user but unknown to the catalog: trust the pin
                    if not need_vision:
                        return ref
                    continue
                if need_vision and not row["vision"]:
                    continue
                if row["context"] and context_tokens * 1.25 > row["context"]:
                    continue
                return ref
    return None


# ── decision ─────────────────────────────────────────────────────────────────

def _features(prompt: str, context_tokens: int) -> Dict[str, Any]:
    words = len(prompt.split())
    return {
        "length": "short" if words < 25 else "medium" if words < 150 else "long",
        "questions": min(prompt.count("?"), 5), "has_code": bool(re.search(r"```|\bdef |\bclass |;\s*$|\{\s*$", prompt, re.M)),
        "numbered_steps": len(re.findall(r"(?m)^\s*(\d+[.)]|[-*])\s", prompt)),
        "risk_words": bool(_HARD_RISK.search(prompt)),
        "context": "small" if context_tokens < 8000 else "medium" if context_tokens < 64000 else "large",
    }


def _keep(current: Optional[str], reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"routed": False, "model": current, "reason": reason, "policy": POLICY_VERSION,
           "notice": f"[Jev] kept {current or 'current model'} · {reason}"}
    out.update(extra)
    return out


def decide(
    prompt: str, *, current: Optional[str] = None, context_tokens: int = 0, has_images: bool = False,
    profile: Optional[str] = None, pinned: bool = False, config: Optional[Dict[str, Any]] = None,
    rows: Optional[List[Dict[str, Any]]] = None, transport: Optional[client.Transport] = None,
    timeout: float = 2.5, only_provider: Optional[str] = None, session_id: str = "",
) -> Dict[str, Any]:
    """Route one fresh user turn. Call it once per turn, never inside a tool loop."""
    config = config or load_config()
    if pinned:
        return _keep(current, "you pinned this model")
    if not (config.get("tiers") or {}):
        return _keep(current, "no tiers configured; run `jev models suggest --write`")
    head = prompt.lstrip()[:80]
    if any(head.startswith(prefix) for prefix in config.get("skip_prefixes") or []) or \
            any(str(session_id).startswith(prefix) for prefix in config.get("skip_session_prefixes") or []):
        return _keep(current, "automated turn; keeps its configured model")
    if not prompt.strip():
        return _keep(current, "empty turn")

    # Judge the ask, not the boilerplate around it. A long turn is mostly standing instructions; what is
    # being asked for sits at the start and, far more often, at the end.
    limit = int(config.get("ask_chars", 2500))
    ask = prompt if len(prompt) <= limit else prompt[:limit // 4] + "\n[…]\n" + prompt[-(limit - limit // 4):]
    risky = bool(_HARD_RISK.search(privacy.normalize(ask)))
    private = profile in (config.get("private_profiles") or []) or privacy.is_sensitive(ask)
    mode = "features" if private else config.get("mode", "redacted-text")
    state: Any = (
        {"turn_features": _features(ask, context_tokens)} if mode == "features"
        else {"user_turn": privacy.redact(ask, limit + 50), "context": _features(ask, context_tokens)["context"]}
    )
    questions = {
        "difficulty": client.score("How demanding is it to complete this turn well?", DIFFICULTY),
        "kind": client.choice("What kind of work is this turn mainly?", KIND),
        "costly_mistake": client.noul("A wrong or sloppy answer here would be costly or hard to undo"),
    }
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return _keep(current, f"Jev unavailable ({error.code})", private=private)

    answers = reply["answers"]
    difficulty, confidence = answers["difficulty"]["score"], answers["difficulty"]["confidence"]
    stakes = answers["costly_mistake"]["noul"]
    spread = answers["difficulty"].get("probabilities") or {}
    if spread:
        p_simple, p_hard = spread.get(0, 0.0), spread.get(2, 0.0) + spread.get(3, 0.0)
    else:                      # no spread returned: fall back to the averaged score, conservatively
        p_simple, p_hard = float(difficulty < 0.5), float(difficulty >= 2.25)

    # An unsure answer is not evidence of a hard turn. Its averaged score lands mid-rubric by arithmetic,
    # so it must never buy the expensive tier: a harmless unsure turn stays put, a risky one gets medium.
    unsure = confidence < config["min_confidence"]
    if unsure and not (risky or stakes > 0.6):
        return _keep(current, f"low confidence {confidence:.2f}", private=private)

    if unsure:
        tier = "medium"
    elif p_hard >= config["hard_needs_probability"]:
        tier = "hard"
    elif p_simple >= config["simple_needs_probability"] and confidence >= config["simple_needs_confidence"]:
        tier = "simple"
    else:
        tier = "medium"
    if tier == "simple" and (risky or stakes > 0.4 or mode == "features"):
        tier = "medium"        # risk words and costly mistakes set a floor of medium; they do not buy hard
    if not unsure and stakes > 0.85 and p_hard >= 0.35:
        tier = "hard"          # a costly mistake tips a turn that is already leaning hard
    kind = answers["kind"]
    specialty = kind["choice"] if kind["confidence"] >= 0.5 else "general"

    catalog_rows = rows if rows is not None else catalog_mod.models()
    by_ref = {_ref(row): row for row in catalog_rows}
    picked = _pick(config, by_ref, tier, specialty, has_images, context_tokens, only_provider)
    if not picked:
        return _keep(current, f"no {tier} model fits this turn", private=private)

    if current and picked != current and context_tokens > config["sticky_context_tokens"]:
        current_price = (by_ref.get(current) or {}).get("price")
        picked_price = (by_ref.get(picked) or {}).get("price")
        if current_price is not None and picked_price is not None and picked_price < current_price:
            return _keep(current, "large context; switching down would cost more than it saves", private=private)

    provider, model = picked.split(":", 1)
    return {
        "routed": picked != current, "model": picked, "provider": provider, "model_id": model, "tier": tier,
        "specialty": specialty, "confidence": round(confidence, 3), "difficulty": round(difficulty, 2),
        "costly_mistake": round(stakes, 3), "private": private, "mode": mode, "latency_ms": reply["latency_ms"],
        "policy": POLICY_VERSION, "reason": f"{tier} {specialty}",
        "notice": f"[Jev] {tier} · {specialty} → {model} · confidence {confidence:.2f}",
    }
