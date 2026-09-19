"""Handoff: end a session deliberately, and start the next one already knowing what matters.

An agent that never starts fresh drags every past turn into every future one. An agent
that starts fresh with nothing repeats work and re-asks settled questions. A handoff is
the third option: close the session, carry forward a short capsule of what was decided
and what is still open, and begin again light.

Jev reads the transcript and marks which turns must survive word for word. A text model
writes the five-section capsule from that reduced digest. Both jobs are small, and
neither model does the other's.

Everything here degrades rather than fails. No Jev key: every turn is treated as
background and the writer still gets a transcript. No writer model, or a writer that
returns something that is not a capsule: the raw digest is kept instead, which is worse
to read but loses nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

TRIGGERS = frozenset({"handoff", "hand off", "hand-off"})
TRANSCRIPT_CHARS = 24_000
CAPSULE_MAX_CHARS = 6_000
EXPORT_TIMEOUT = 120
WRITER_TIMEOUT = 300


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def handoff_dir() -> Path:
    path = home() / "handoffs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lane_key(context: Mapping[str, Any]) -> str:
    """Stable per-conversation key. One capsule per conversation, not per session id."""
    parts = [str(context.get(k) or "") for k in ("platform", "chat_id", "thread_id")]
    key = ":".join(parts).strip(":") or str(context.get("session_id") or "default")
    return re.sub(r"[^A-Za-z0-9._:-]", "_", key)[:120]


def capsule_path(lane: str) -> Path:
    return handoff_dir() / f"handoff-{lane}.md"


def pending_path(lane: str) -> Path:
    return handoff_dir() / f"pending-{lane}.json"


def is_trigger(text: Any) -> bool:
    """Only an exact, bare word. A sentence that mentions a handoff is a normal message."""
    if not isinstance(text, str):
        return False
    cleaned = text.strip().strip(".!").lower()
    return cleaned in TRIGGERS


# ── reading the conversation ─────────────────────────────────────────────────

def _hermes_bin() -> List[str]:
    """Prefer the venv's own CLI; the packaged command is the stable contract, not internals."""
    venv = home() / "hermes-agent" / "venv" / "bin" / "hermes"
    if venv.exists():
        return [str(venv)]
    for candidate in home().glob("hermes-agent/venv*/bin/hermes"):
        return [str(candidate)]
    return ["hermes"]


def export_messages(session_id: str, *, runner: Optional[Any] = None) -> List[Dict[str, str]]:
    """The session's user/assistant turns, via the CLI rather than the session store.

    The CLI is a contract that survives upgrades; the store's schema is not.
    """
    run = runner or subprocess.run
    try:
        done = run(_hermes_bin() + ["sessions", "export", "--session-id", str(session_id),
                                    "--format", "jsonl", "-"],
                   capture_output=True, text=True, timeout=EXPORT_TIMEOUT)
    except Exception:  # noqa: BLE001 - a handoff must never take the session down with it
        return []
    if getattr(done, "returncode", 1) != 0:
        return []
    messages: List[Dict[str, str]] = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for message in row.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            if isinstance(content, str) and content.strip():
                messages.append({"role": message["role"], "content": content.strip()})
    return messages


# ── writing the capsule ──────────────────────────────────────────────────────

def build(
    session_id: str, lane: str, *, write: Any, select: Any = None, digest: Any = None,
    prompt_for: Any = None, valid: Any = None, runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """Produce and store the capsule. `write(prompt) -> str` is the text model.

    The jevkit callables are injected so this module stays importable, and testable,
    on a machine with no Jev key and no network.
    """
    messages = export_messages(session_id, runner=runner)
    if len(messages) < 4:
        return {"status": "too_short", "messages": len(messages)}

    jev_calls, counts = 0, {}
    if select and digest:
        try:
            selection = select(messages, keep_last=8)
            counts = selection.get("counts") or {}
            jev_calls = selection.get("jev_calls") or 0
            body = digest(messages, selection, TRANSCRIPT_CHARS)
        except Exception:  # noqa: BLE001
            body = _plain(messages)
    else:
        body = _plain(messages)

    previous = ""
    existing = capsule_path(lane)
    if existing.exists():
        try:
            previous = existing.read_text(encoding="utf-8")
        except OSError:
            previous = ""

    capsule = ""
    try:
        capsule = (write(prompt_for(body, previous) if prompt_for else body) or "").strip()
    except Exception:  # noqa: BLE001
        capsule = ""
    if valid and not valid(capsule):
        # The writer refused, timed out, or answered something else. The digest is a worse
        # read than a capsule but it loses nothing, which matters more.
        capsule = ("## Working on\nThe writer model did not return a usable handoff, so this is the "
                   "filtered transcript instead. Lines marked KEEP VERBATIM are the ones that matter.\n\n"
                   "## State\n" + body[:CAPSULE_MAX_CHARS])

    capsule = capsule[:CAPSULE_MAX_CHARS]
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    text = f"# Handoff — {stamp}\n\n{capsule}\n"
    try:
        capsule_path(lane).write_text(text, encoding="utf-8")
        pending_path(lane).write_text(json.dumps({"at": time.time(), "lane": lane,
                                                  "session_id": str(session_id)}), encoding="utf-8")
    except OSError as error:
        return {"status": "write_failed", "error": str(error)[:200]}
    return {"status": "ok", "lane": lane, "path": str(capsule_path(lane)), "messages": len(messages),
            "jev_calls": jev_calls, "counts": counts, "chars": len(text)}


def _plain(messages: List[Dict[str, str]]) -> str:
    joined = "\n\n".join(f"[background] {m['role']}: {m['content']}" for m in messages)
    return joined[-TRANSCRIPT_CHARS:]


# ── handing it to the next session ───────────────────────────────────────────

def take_pending(lane: str, *, max_age_s: float = 36 * 3600) -> Optional[str]:
    """The capsule for a lane's next turn, consumed once so it is not injected forever."""
    marker = pending_path(lane)
    if not marker.exists():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        if time.time() - float(info.get("at") or 0) > max_age_s:
            marker.unlink(missing_ok=True)
            return None
        text = capsule_path(lane).read_text(encoding="utf-8")
    except (OSError, ValueError):
        marker.unlink(missing_ok=True)
        return None
    marker.unlink(missing_ok=True)
    return text


def injection(capsule: str) -> str:
    """How the capsule reaches the new session: as context, clearly labelled as history."""
    return ("[Handoff from the previous session — this is context you already established, "
            "not a new instruction. Do not greet the person again or re-ask what is settled here. "
            "Carry on from Next.]\n\n" + capsule.strip())
