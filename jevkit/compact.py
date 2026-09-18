"""Compaction and handoffs: Jev decides what survives, a text model only writes it up.

Jev cannot summarize. What it can do, in one fast request, is read a transcript
turn by turn and mark each turn as something to carry forward word for word, to
fold into the summary, or to drop. The summarizing model then gets a fraction of
the transcript with the decisions, open work and pointers already pulled out, so
the handoff is shorter, cheaper, and stops losing the one line that mattered.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import client, privacy

TURN_CHARS = 700
BATCH = 40
FATE = {
    "keep": "Carries a decision, a constraint, a user preference, an unfinished task, an exact value, path, id, "
            "command or error that later work depends on",
    "summarize": "Useful background whose gist matters but whose exact wording does not",
    "drop": "Chatter, acknowledgements, superseded attempts, repeated output, or detail nothing later depends on",
}


def _text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def select(
    messages: Sequence[Mapping[str, Any]], *, keep_last: int = 6, timeout: float = 8.0,
    transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    """Return a fate for every message index. The last ``keep_last`` are always kept."""
    total = len(messages)
    fates: Dict[int, str] = {index: "keep" for index in range(max(0, total - keep_last), total)}
    judged = [index for index in range(total) if index not in fates]
    for index in judged:
        fates[index] = "summarize"            # the fail-open default: nothing is dropped unless Jev said so
        if messages[index].get("role") == "system":
            fates[index] = "keep"
    judged = [i for i in judged if fates[i] != "keep" and _text(messages[i]).strip()]

    calls, errors, latency = 0, [], 0
    for group in client.batches(judged, BATCH):
        sendable = [i for i in group if not privacy.is_sensitive(_text(messages[i]))]
        if not sendable:
            continue
        state = {"turns": {f"T{i}": f"{messages[i].get('role', 'user')}: {privacy.redact(_text(messages[i]), TURN_CHARS)}"
                           for i in sendable}}
        questions = {f"t{i}": client.choice(f"For continuing this work later, what should happen to turn T{i}?", FATE)
                     for i in sendable}
        try:
            reply = client.ask(state, questions, timeout=timeout, transport=transport)
        except client.JevError as error:
            errors.append(error.code)
            continue
        calls += 1
        latency += reply["latency_ms"]
        for i in sendable:
            answer = reply["answers"][f"t{i}"]
            # Dropping is the only irreversible fate, so it needs a confident answer.
            if answer["choice"] == "drop" and answer["confidence"] < 0.7:
                continue
            fates[i] = answer["choice"]

    counts = {fate: sum(1 for value in fates.values() if value == fate) for fate in FATE}
    return {"status": "ok" if calls or not judged else "fail_open", "fates": {str(k): v for k, v in sorted(fates.items())},
            "counts": counts, "jev_calls": calls, "errors": errors, "latency_ms": latency}


def digest(messages: Sequence[Mapping[str, Any]], selection: Mapping[str, Any], limit: int = 24000) -> str:
    """The reduced transcript to hand to the summarizing model."""
    fates = selection["fates"]
    lines: List[str] = []
    for index, message in enumerate(messages):
        fate = fates.get(str(index), "summarize")
        if fate == "drop":
            continue
        body = _text(message).strip()
        if not body:
            continue
        if fate == "summarize":
            body = body[:400] + (" […]" if len(body) > 400 else "")
        marker = "KEEP VERBATIM" if fate == "keep" else "background"
        lines.append(f"[{marker}] {message.get('role', 'user')}: {body}")
    text = "\n\n".join(lines)
    return text if len(text) <= limit else text[-limit:]


def should_compact(used_tokens: int, window_tokens: int, *, soft: float = 0.6, hard: float = 0.85) -> Dict[str, Any]:
    """Pure arithmetic; no model needed to know the window is nearly full."""
    ratio = used_tokens / window_tokens if window_tokens else 0.0
    return {"ratio": round(ratio, 3), "compact": ratio >= soft, "urgent": ratio >= hard}
