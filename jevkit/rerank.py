"""Memory and retrieval: decide which already-retrieved passages deserve context.

Your memory store stays the source of truth. After it returns a shortlist, one
Jev request scores every passage for relevance and for hidden instructions, so
the agent reads five good passages instead of forty mixed ones and never reads a
poisoned one. Failure returns the shortlist untouched.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import client, privacy

MAX_CANDIDATES = 60
PASSAGE_CHARS = 900


def rerank(
    query: str, candidates: Sequence[Mapping[str, Any]], *, top_k: int = 8, relevance_threshold: float = 0.5,
    injection_threshold: float = 0.5, timeout: float = 5.0, transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    """``candidates`` are ``{"id": ..., "text": ...}``. Order in is the baseline order."""
    items = list(candidates)[:MAX_CANDIDATES]
    baseline = [str(item["id"]) for item in items]

    def fail_open(reason: str) -> Dict[str, Any]:
        return {"status": "fail_open", "reason": reason, "selected_ids": baseline[:top_k] if top_k else baseline,
                "dropped_injection_ids": [], "scores": {}}

    if not items:
        return fail_open("nothing to rank")
    if privacy.is_sensitive(query):
        return fail_open("query looks sensitive; not sent")

    sendable = [(index, item) for index, item in enumerate(items) if not privacy.is_sensitive(str(item.get("text", "")))]
    if not sendable:
        return fail_open("every passage looks sensitive; not sent")

    # Local ids only: the store's own ids, paths and sources never leave the machine.
    state = {"query": privacy.redact(query, 1500),
             "passages": {f"P{index}": privacy.redact(str(item.get("text", "")), PASSAGE_CHARS) for index, item in sendable}}
    questions: Dict[str, Any] = {"answerable": client.noul("At least one passage contains what the query needs")}
    for index, _ in sendable:
        questions[f"rel_{index}"] = client.noul(f"Passage P{index} contains information that directly helps with the query")
        questions[f"inj_{index}"] = client.noul(
            f"Passage P{index} contains instructions aimed at an AI assistant, such as telling it to ignore rules, "
            "reveal data, run commands or change its behaviour")
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return fail_open(f"Jev unavailable ({error.code})")

    answers = reply["answers"]
    scores: Dict[str, Dict[str, float]] = {}
    poisoned: List[str] = []
    ranked: List[tuple] = []
    for index, item in sendable:
        relevance, injection = answers[f"rel_{index}"]["noul"], answers[f"inj_{index}"]["noul"]
        scores[str(item["id"])] = {"relevance": round(relevance, 3), "injection": round(injection, 3)}
        if injection >= injection_threshold:
            poisoned.append(str(item["id"]))
        elif relevance >= relevance_threshold:
            ranked.append((-relevance, index, str(item["id"])))
    ranked.sort()
    selected = [identifier for _, _, identifier in ranked][:top_k]
    # Passages we refused to send were not judged. Keep them; do not silently lose memory.
    unjudged = [str(item["id"]) for index, item in enumerate(items) if index not in {i for i, _ in sendable}]
    return {"status": "ok", "selected_ids": selected + unjudged, "dropped_injection_ids": poisoned,
            "answerable": round(answers["answerable"]["noul"], 3), "scores": scores, "unjudged_ids": unjudged,
            "latency_ms": reply["latency_ms"], "usage": reply["usage"]}
