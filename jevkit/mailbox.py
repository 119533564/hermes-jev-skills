"""Sort a personal mailbox into lanes: needs reply, updates, promotional, sales, spam.

This is the *inbox* shape of triage. `triage.py` answers a support question — how urgent
is this, what kind of request is it, does a person have to decide — and routes it now /
today / queue / ignore. This module answers the other question a mailbox asks: which of
these thousands of messages is even addressed to me as a person.

The lane taxonomy and the message-state block are ported from **jevmail**
(https://github.com/fazlerocks/jevmail, MIT, Copyright (c) 2026 Fazle Rahman), which sorts
Gmail through the Vercel AI Gateway. Three things were kept, and three changed.

Kept, because they are the parts that earn their place:

- the five lanes, because "needs reply" and "promotional" are the split that decides
  whether anyone opens the thing at all;
- the two cheap header signals in the state — whether the mail carries an unsubscribe header,
  and whether the recipient has already replied in the thread. Both are free and both
  move the answer (a reply-to-me thread is never cold outreach). The mailbox address
  itself is never sent: the domain and a locally-derived sender class stand in for it;
- per-answer probabilities kept next to the verdict, so a correction can be read against
  what Jev actually said.

Changed, because our own measurements say so:

- **Urgency is decided on probability mass, not on the rounded score.** jevmail stores
  `round(score) + 1`. On a live bank alert Jev answered with a *flat* urgency
  distribution, confidence 0.0, and a point estimate of 2.7 — which rounds to an
  arbitrary 3. `triage.py` already learned this; here the point estimate is reported,
  and the mass at the top of the rubric is what a caller acts on.
- **Low confidence is a flag with a reason, not a hidden heuristic.** jevmail computes
  the top-two gap but only uses it in the UI. Anything within 0.15 of the runner-up is
  marked, and unsure mail is never filed away unseen.
- **Fail-open means a person looks, never that mail disappears.** A Jev failure, a
  message carrying a secret, or an answer outside the lane set all set
  `needs_attention` true and say why.

Code still makes the routing decision. Jev supplies calibrated readings; the thresholds
here are ours, are readable, and can be argued with.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import client, privacy

BODY_CHARS = 2_500
SUBJECT_CHARS = 300

# The lane set is closed: a Jev choice can only return one of these, so a caller never has
# to defend against an invented label.
LANES = {
    "needs_reply": (
        "A real person expects a reply from the recipient: a colleague, customer, friend, or "
        "existing contact asking something or continuing a conversation."
    ),
    "updates": (
        "Transactional or informational mail about the recipient's own accounts and activity: "
        "bank and card transaction alerts, OTPs and security notices, order, delivery, and "
        "booking status, receipts and invoices, bill reminders, service alarms, calendar or "
        "system notifications. Automated, addressed to the recipient, not trying to sell anything."
    ),
    "promotional": (
        "Marketing: newsletters, campaigns, discounts, product announcements, event invitations, "
        "and content digests sent to a list. Usually has an unsubscribe link and is not about a "
        "specific transaction of the recipient."
    ),
    "sales": (
        "Unsolicited cold outreach from a vendor, agency, or recruiter trying to start a "
        "conversation or book a call with the recipient."
    ),
    "spam": "Scams, phishing, fake invoices, or junk with no legitimate purpose.",
}

# Ordered low to high. The mass at the top two levels is the signal that matters; the
# expectation of a flat distribution is not a level at all.
URGENCY = [
    "No reply needed, or no time pressure at all",
    "A reply within a week is fine",
    "A reply within a few days is expected",
    "A reply today is expected",
    "A person is blocked, or a deadline falls within 24 hours",
]

LANE_ORDER = tuple(LANES)
LOW_CONFIDENCE_GAP = 0.15
# Probability mass at "today" + "blocked" that makes a personal message worth interrupting for.
URGENT_MASS = 0.4
PERSONAL_ENOUGH = 0.6


def _field(value: Any, limit: int) -> str:
    return privacy.redact(str(value or "").strip(), limit)


def _looks_bulk(sender: str, subject: str, body: str, headers: str = "") -> bool:
    """An unsubscribe route is the cheapest honest signal of list mail. No model needed."""
    probe = f"{sender} {subject} {headers} {body[:800]}".lower()
    return bool(re.search(r"(unsubscribe|list-unsubscribe|opt[- ]?out|preferences center)", probe))


def _thread_replied(message: Mapping[str, Any]) -> bool:
    """Did the recipient already write in this thread? Labels and headers both say so."""
    labels = str(message.get("labels") or message.get("label_ids") or "").upper()
    if "SENT" in labels:
        return True
    for key in ("replied_before", "is_reply_to_me", "user_replied", "in_reply_to_me"):
        if key in message and message.get(key) is not None:
            return bool(message.get(key))
    return False


def _sender_domain(sender: str) -> str:
    """The domain, never the mailbox: it is the signal, and the local part is a person's."""
    return sender.split("@")[-1].strip(">").lower().strip() if "@" in sender else ""


def _sender_class(sender: str, bulk: bool) -> str:
    """Cheap, local, and free: whether a machine, a list, or a person sent this."""
    probe = sender.split("@")[0].lower() if "@" in sender else sender.lower()
    if re.search(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|notifications?|"
                 r"alerts?|support|billing|receipts?|orders?|noreply)", probe):
        return "automated"
    if bulk or re.search(r"(news|newsletter|updates?|hello|info|team|marketing|offers|digest)", probe):
        return "list"
    return "person"


def build_state(message: Mapping[str, Any], *, has_unsubscribe: Optional[bool] = None,
                replied_before: Optional[bool] = None) -> Dict[str, Any]:
    """The block Jev reads: what it says, plus the free signals that are not in the text.

    A mailbox address is never sent. The domain and a locally-derived sender class carry
    what the lane question actually needs, and the two header facts carry the rest.
    """
    sender = str(message.get("sender") or message.get("from") or "").strip()
    subject = _field(message.get("subject"), SUBJECT_CHARS)
    body = _field(message.get("content") or message.get("body") or message.get("snippet"), BODY_CHARS)
    received = str(message.get("received") or message.get("date") or "").strip()
    headers = str(message.get("headers") or "")
    if has_unsubscribe is None:
        has_unsubscribe = _looks_bulk(sender, subject, body, headers)
    if replied_before is None:
        replied_before = _thread_replied(message)
    return {
        "subject": subject or "(no subject)",
        "body": body or "(empty)",
        "from_domain": _sender_domain(sender),
        "sender_class": _sender_class(sender, bool(has_unsubscribe)),
        "received": received or "unknown",
        "has_unsubscribe_header": bool(has_unsubscribe),
        "user_replied_in_thread": bool(replied_before),
    }


def questions() -> Dict[str, Any]:
    return {
        "lane": client.choice(
            "Which lane does this email belong in? Judge from the sender, subject, headers, and body together.",
            LANES,
        ),
        "urgency": client.score(
            "How urgently does this email need a reply from the recipient? Pick the level that best matches.",
            URGENCY,
        ),
        "personal": client.noul(
            "Was this email written by a human specifically to this recipient, rather than sent "
            "to a list or generated by a system?"
        ),
    }


def classify(message: Mapping[str, Any], *, has_unsubscribe: Optional[bool] = None,
             replied_before: Optional[bool] = None, timeout: float = 5.0,
             transport: Optional[client.Transport] = None) -> Dict[str, Any]:
    """Read one message and say which lane it belongs in. Never raises."""
    result: Dict[str, Any] = {
        "lane": None, "urgency": None, "needs_attention": False, "personal": None,
        "low_confidence": False, "confidence": 0.0, "sent_to_jev": False, "reason": "",
    }

    subject = str(message.get("subject") or "").strip()
    body = str(message.get("content") or message.get("body") or message.get("snippet") or "").strip()
    if not (subject or body):
        result.update(lane=None, reason="empty message")
        return result

    if privacy.is_sensitive(f"{subject}\n{body}"):
        # A message carrying a credential is not sent anywhere, and is exactly the kind of
        # thing a person should see rather than have sorted into a tray.
        result.update(needs_attention=True,
                      reason="looks like it contains a secret; not sent to Jev, flagged for a person")
        return result

    state = build_state(message, has_unsubscribe=has_unsubscribe, replied_before=replied_before)
    try:
        reply = client.ask(state, questions(), timeout=timeout, transport=transport)
    except client.JevError as error:
        # Unsorted mail that nobody looks at is worse than mail in the wrong tray.
        result.update(needs_attention=True, reason=f"Jev unavailable ({error.code}); a person should look")
        return result

    answers = reply["answers"]
    lane_answer = answers["lane"]
    urgency = answers["urgency"]
    probs: Dict[str, float] = urgency.get("probabilities") or {}
    mass = {int(k): float(v) for k, v in probs.items() if str(k).lstrip("-").isdigit()}
    p_today = mass.get(3, 0.0) + mass.get(4, 0.0)
    p_none = mass.get(0, 0.0)
    level = float(urgency["score"])
    urgency_confidence = float(urgency["confidence"])

    lane_probs = {k: float(v) for k, v in (lane_answer.get("probabilities") or {}).items()}
    ranked = sorted(lane_probs.items(), key=lambda kv: -kv[1])
    gap = (ranked[0][1] - ranked[1][1]) if len(ranked) > 1 else 1.0
    lane = lane_answer["choice"]
    if lane not in LANES:
        # A label outside the closed set would be the model inventing one; treat as unsure.
        result.update(needs_attention=True, low_confidence=True,
                      reason=f"answered with an unknown lane ({lane}); a person should look")
        return result

    personal = float(answers["personal"]["noul"])
    confidence = float(lane_answer["confidence"])
    low_confidence = gap < LOW_CONFIDENCE_GAP

    # A human being writing to you is never filed under junk, whatever the lane says.
    if personal >= PERSONAL_ENOUGH and lane in ("promotional", "spam"):
        lane, low_confidence = "needs_reply", True

    needs_attention = bool(lane == "needs_reply" and (p_today >= URGENT_MASS or level >= 3.0))
    if low_confidence and lane in ("needs_reply", "updates"):
        needs_attention = True

    result.update({
        "lane": lane,
        "confidence": round(confidence, 3),
        "lane_probabilities": {k: round(v, 3) for k, v in ranked},
        "runner_up_gap": round(gap, 3),
        "low_confidence": low_confidence,
        "urgency": round(level + 1, 2),
        "urgency_confidence": round(urgency_confidence, 3),
        "urgent_mass": round(p_today, 3),
        "personal": round(personal, 3),
        "needs_attention": needs_attention,
        "sent_to_jev": True,
        "latency_ms": reply.get("latency_ms"),
        "reason": (f"{lane}, urgency {level + 1:.1f}/5"
                   + (", no-time-pressure mass {:.2f}".format(p_none) if lane == "updates" else "")
                   + (", unsure between lanes" if low_confidence else "")),
    })
    return result


def classify_many(messages: Sequence[Mapping[str, Any]], *, workers: int = 8,
                  timeout: float = 6.0, transport: Optional[client.Transport] = None,
                  ) -> List[Dict[str, Any]]:
    """Sort a batch. Each message is one independent Jev call, run side by side."""
    from concurrent.futures import ThreadPoolExecutor

    def one(message: Mapping[str, Any]) -> Dict[str, Any]:
        out = classify(message, timeout=timeout, transport=transport)
        out["id"] = message.get("id")
        out["subject"] = str(message.get("subject") or "")[:120]
        out["sender"] = str(message.get("sender") or message.get("from") or "")[:120]
        out["received"] = message.get("received") or message.get("date")
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, messages))


def summarize(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the batch says about the mailbox, where it was unsure, and what it cost."""
    lanes: Dict[str, int] = {lane: 0 for lane in LANE_ORDER}
    lanes["unsorted"] = 0
    for row in rows:
        lane = row.get("lane") or "unsorted"
        lanes[lane] = lanes.get(lane, 0) + 1
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms"))
    return {
        "messages": len(rows),
        "lanes": dict(sorted(lanes.items(), key=lambda kv: -kv[1])),
        "needs_attention": sum(1 for r in rows if r.get("needs_attention")),
        "unsure": [{"subject": r.get("subject"), "lane": r.get("lane"),
                    "runner_up_gap": r.get("runner_up_gap")}
                   for r in rows if r.get("low_confidence")][:10],
        "not_sent_to_jev": sum(1 for r in rows if not r.get("sent_to_jev")),
        "latency_ms": {"p50": latencies[len(latencies) // 2] if latencies else None,
                       "p90": latencies[int(len(latencies) * 0.9)] if latencies else None},
        # 0.042 USD per million input tokens, the published Jev rate; a state block here
        # measures ~450 tokens, so this is an estimate and says so.
        "cost_estimate_usd": round(len(rows) * 450 * 0.042 / 1e6, 5),
    }