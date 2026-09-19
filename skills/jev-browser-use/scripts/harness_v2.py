"""WindTunnel v2 harness adaptations for the Jev Ultrafast loop.

WindTunnel (Apache-2.0, https://github.com/nekuda-ai/WindTunnel) measured two Jev
+ Mercury 2.5 arms on its September 18 cohort: 25/49 tasks with the DOM/ultrafast
interface and 49/49 with WebMCP. The DOM arm is the same pinned upstream commit we
vendor (``452c1ad``) plus the adaptations this module applies. None of them was
attributed in isolation, so treat them as one measured bundle, not four wins.

This module makes no copies of the agent loop. It changes two seams of the pinned
checkout at run time and fails loudly if the checkout has drifted:

* ``jev_ultrafast.browser.READ_STATE`` -- the page snapshot script is swapped for
  ``windtunnel_v2_snapshot.js`` (occlusion audit, fallback controls, fair action
  cap, password redaction).
* ``jev_ultrafast.model.post_json`` -- wrapped so every decision request carries
  the v1 selection policy and the measured DONE/BLOCKED descriptions.

Public API: :func:`apply`, :func:`rewrite_decision_request`, :func:`load_*`.
Nothing here writes to the vendored checkout, and nothing here is applied unless
the runner asks for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
SNAPSHOT_PATH = HERE / "windtunnel_v2_snapshot.js"
POLICY_PATH = HERE / "windtunnel_v2_policy.txt"
OVERRIDES_PATH = HERE / "windtunnel_v2_policy_overrides.json"

HARNESS_V2 = "v2"
HARNESS_UPSTREAM = "upstream"
HARNESS_CHOICES = (HARNESS_V2, HARNESS_UPSTREAM)


def load_snapshot() -> str:
    """The v2 page snapshot script, comments intact."""
    return SNAPSHOT_PATH.read_text()


def load_policy() -> str:
    """The v1 selection policy, with its provenance header stripped."""
    lines = POLICY_PATH.read_text().splitlines(keepends=True)
    return "".join(line for line in lines if not line.startswith("#")).strip()


def load_overrides() -> Dict[str, str]:
    """The measured terminal-operation descriptions."""
    return dict(json.loads(OVERRIDES_PATH.read_text())["descriptions"])


def rewrite_decision_request(body: Any, policy: str, overrides: Dict[str, str]) -> bool:
    """Put ``policy`` and ``overrides`` into one decision request body.

    Returns True when at least one rule or description was replaced, so a caller
    can tell "the checkout changed shape" from "the policy was applied". Pure:
    the body is mutated in place and nothing else is touched.
    """
    if not isinstance(body, dict):
        return False
    questions = body.get("questions")
    if not isinstance(questions, dict):
        return False
    changed = False
    for question in questions.values():
        if not isinstance(question, dict):
            continue
        instructions = question.get("instructions")
        if not isinstance(instructions, dict) or "rules" not in instructions:
            continue
        rules = instructions["rules"]
        if isinstance(rules, str):
            instructions["rules"] = policy
            changed = True
        elif isinstance(rules, list) and rules:
            # Keep the head-specific rules that follow the shared policy (TARGET).
            instructions["rules"] = [policy, *rules[1:]]
            changed = True
    operation = questions.get("operation")
    criteria = operation.get("criteria") if isinstance(operation, dict) else None
    if isinstance(criteria, dict):
        for name, description in overrides.items():
            if name in criteria:
                criteria[name] = description
                changed = True
    return changed


def apply(harness: str = HARNESS_V2, notes: List[str] | None = None) -> Dict[str, Any]:
    """Apply one harness to the already-imported ``jev_ultrafast`` package.

    ``notes`` collects human-readable findings (drift, already-applied). Call this
    after ``ensure_importable`` and before the first ``Agent`` is constructed.
    """
    applied: Dict[str, Any] = {"harness": harness}
    if harness == HARNESS_UPSTREAM:
        return applied

    from jev_ultrafast import browser as browser_module
    from jev_ultrafast import model as model_module

    # -- snapshot ------------------------------------------------------------
    snapshot = load_snapshot()
    if getattr(browser_module, "READ_STATE", None) == snapshot:
        if notes is not None:
            notes.append("snapshot: already v2")
    else:
        browser_module.READ_STATE = snapshot
        # MARKER is built from READ_STATE at import time and is what `fresh()` uses
        # to decide whether a decision still refers to the observed page. Swapping
        # READ_STATE alone leaves the upstream marker in place, every page then
        # reads as permanently stale, and the loop observes forever without acting.
        browser_module.MARKER = (
            f"(() => {{ const state={snapshot}; return state?.marker ?? null; }})()"
        )
        if notes is not None:
            notes.append("snapshot: v2 applied (snapshot + marker)")
    applied["snapshot_chars"] = len(snapshot)

    # -- policy --------------------------------------------------------------
    policy = load_policy()
    overrides = load_overrides()
    if getattr(model_module.post_json, "_windtunnel_v2", False):
        if notes is not None:
            notes.append("policy: already v2")
        applied["policy_applied"] = True
        return applied

    original = model_module.post_json
    first: Dict[str, bool] = {}

    def post_json(url, key, body, _original=original, _policy=policy, _overrides=overrides,
                  _first=first, _notes=notes):
        changed = rewrite_decision_request(body, _policy, _overrides)
        if not _first:
            _first["seen"] = True
            if not changed and _notes is not None:
                _notes.append(
                    "policy: request has no rules/criteria to rewrite -- the vendored "
                    "checkout has drifted from 452c1ad; re-verify before trusting this run"
                )
        return _original(url, key, body)

    post_json._windtunnel_v2 = True  # type: ignore[attr-defined]
    model_module.post_json = post_json
    applied["policy_applied"] = True
    applied["policy_chars"] = len(policy)
    applied["terminal_overrides"] = sorted(overrides)
    if notes is not None:
        notes.append("policy: v2 applied")
    return applied
