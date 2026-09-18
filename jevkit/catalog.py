"""Every model this machine can actually call, with price, context and abilities.

The source is the public models.dev catalog (Hermes keeps a copy; otherwise it is
fetched and cached for a day). A provider counts as *available* when one of the
API-key names it declares is set in the environment or in a Hermes ``.env``, or
when Hermes holds a login for it. Only names are read, never values.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_TTL = 24 * 3600

# Hermes login names → models.dev provider ids, where they differ.
HERMES_ALIASES = {
    "xai-oauth": "xai", "gemini": "google", "kimi-coding": "kimi-for-coding", "openai-codex": "openai",
    "zai": "zai", "copilot": "github-copilot", "minimax": "minimax", "moonshot": "moonshotai",
}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def hermes_root() -> Path:
    """The shared Hermes folder. A profile's home is <root>/profiles/<name>."""
    home = hermes_home()
    return home.parent.parent if home.parent.name == "profiles" else home


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "jev" / "models_dev.json"


def load_models_dev(refresh: bool = False) -> Dict[str, Any]:
    candidates = [hermes_home() / "models_dev_cache.json", hermes_root() / "models_dev_cache.json", _cache_path()]
    if not refresh:
        fresh = [p for p in candidates if p.is_file() and time.time() - p.stat().st_mtime < CACHE_TTL]
        for path in fresh or [p for p in candidates if p.is_file()]:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    request = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "hermes-jev-skills"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https URL
        raw = response.read(60_000_000)
    data = json.loads(raw)
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _env_names() -> Set[str]:
    names = {name for name, value in os.environ.items() if value}
    for path in {hermes_home() / ".env", hermes_root() / ".env"}:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                name, sep, value = line.partition("=")
                if sep and value.strip() and name.strip().isidentifier():
                    names.add(name.strip())
        except OSError:
            continue
    return names


def _hermes_logins() -> Set[str]:
    try:
        path = hermes_home() / "auth.json"
        data = json.loads((path if path.is_file() else hermes_root() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    found: Set[str] = set()
    for section in ("credential_pool", "providers"):
        block = data.get(section)
        if isinstance(block, dict):
            found.update(str(name) for name in block)
    return {HERMES_ALIASES.get(name, name) for name in found}


def available_providers(catalog: Dict[str, Any], extra: Iterable[str] = ()) -> List[str]:
    env = _env_names()
    logins = _hermes_logins()
    out = []
    for provider_id, provider in catalog.items():
        declared = provider.get("env") or []
        if provider_id in logins or provider_id in extra or any(name in env for name in declared):
            out.append(provider_id)
    return sorted(out)


def _blended(cost: Dict[str, Any]) -> Optional[float]:
    """Dollars per million tokens for a typical agent turn (input-heavy)."""
    try:
        return 0.8 * float(cost["input"]) + 0.2 * float(cost["output"])
    except (KeyError, TypeError, ValueError):
        return None


def models(
    catalog: Optional[Dict[str, Any]] = None, providers: Optional[Iterable[str]] = None,
    require_tools: bool = True,
) -> List[Dict[str, Any]]:
    """Flat list of callable text models, cheapest first."""
    catalog = catalog if catalog is not None else load_models_dev()
    wanted = set(providers) if providers is not None else set(available_providers(catalog))
    rows: List[Dict[str, Any]] = []
    for provider_id in wanted:
        provider = catalog.get(provider_id) or {}
        for model_id, spec in (provider.get("models") or {}).items():
            if not isinstance(spec, dict):
                continue
            modalities = spec.get("modalities") or {}
            if "text" not in (modalities.get("output") or ["text"]):
                continue
            if require_tools and not spec.get("tool_call"):
                continue
            price = _blended(spec.get("cost") or {})
            if price is None:
                continue
            if spec.get("status") in ("deprecated", "retired"):
                continue
            limit = spec.get("limit") or {}
            rows.append({
                "provider": provider_id, "model": model_id, "name": spec.get("name") or model_id,
                "price": round(price, 4), "input": (spec.get("cost") or {}).get("input"),
                "output": (spec.get("cost") or {}).get("output"), "context": int(limit.get("context") or 0),
                "vision": "image" in (modalities.get("input") or []), "reasoning": bool(spec.get("reasoning")),
                "released": spec.get("release_date") or "",
            })
    rows.sort(key=lambda row: (row["price"], row["provider"], row["model"]))
    return rows
