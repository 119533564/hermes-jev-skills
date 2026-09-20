"""Sort a personal mailbox into lanes: needs reply, updates, promotional, sales, spam.

This is the *inbox* shape of triage. `triage.py` answers a support question — how urgent
is this, what kind of request is it, does a person have to decide — and routes it now /
today / queue / ignore. This module answers the other question a mailbox asks: which of
these thousands of messages is even addressed to me as a person.

The lane taxonomy and the message-state block are ported from **jevmail**
(https://github.com/fazlerocks/jevmail, MIT, Copyright (c) 2026 Fazle Rahman), which sorts
Gmail through the Vercel AI Gateway. The rubric text below is copied verbatim from their
`src/lib/classify.ts`; their permission notice is in ``NOTICE`` at the root of this repo,
which is what their licence asks for and the credit line alone was not.

Three things were kept, and three changed.

Kept, because they are the parts that earn their place:

- the five lanes, because "needs reply" and "promotional" are the split that decides
  whether anyone opens the thing at all;
- the two cheap header signals in the state — whether the mail carries a List-Unsubscribe
  header, and whether the recipient has already replied in the thread. Both are free, and
  both are *sent to Jev* as facts it would otherwise have to guess. Neither moves the
  answer here: no rule in this module reads them back, and an earlier version of this
  docstring claimed one did. The mailbox address itself is not sent: the domain and a
  locally-derived sender class stand in for it;
- per-answer probabilities kept next to the verdict, so a correction can be read against
  what Jev actually said.

Changed, because our own measurements say so:

- **Urgency is decided on probability mass, not on the rounded score.** jevmail stores
  `Math.min(5, Math.max(1, Math.round(score) + 1))`. On a live bank alert Jev answered
  with a *flat* urgency distribution, confidence 0.0, and a point estimate of 2.73 —
  which that formula stores as 4 of 5, a level nobody chose. `triage.py` already learned
  this; here the point estimate is reported as what it is, the mass at the top of the
  rubric is what a caller acts on, and a spread too flat to read is no reading at all.
- **Low confidence is a flag with a reason, not a hidden heuristic.** jevmail computes
  the top-two gap (`classify.ts`) and stores it (`low_confidence` in `schema.ts`), but
  nothing reads it back — it does not change what happens to the mail. The 0.15 threshold
  is theirs, inherited with the literal. Here anything within 0.15 of the runner-up is
  marked, and unsure mail is never filed away unseen, in any lane.
- **Fail-open means a person looks, never that mail disappears.** A Jev failure, a
  transport that crashes, a message carrying a secret, a message with nothing to read, or
  an answer outside the lane set all set `needs_attention` true and say why.
- **Mail is decoded before it is screened.** A newsletter footer carries the recipient's
  own address percent-encoded in an unsubscribe link and base64'd in a tracking link; a
  quoted-printable body writes `@` as `=40`. A plain-text redactor sees none of those, so
  every one of them put the address on the wire. See ``readable``.

Code still makes the routing decision. Jev supplies calibrated readings; the thresholds
here are ours, are readable, and can be argued with.
"""
from __future__ import annotations

import base64
import binascii
import html
import quopri
import re
import urllib.parse
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import client, privacy

BODY_CHARS = 2_500
SUBJECT_CHARS = 300
RECEIVED_CHARS = 64
# Nothing is screened at full length. A 10 MB body was normalised twice - once by
# is_sensitive and once by redact - before a single byte was capped, which cost ~5 s of
# CPU per message inside a worker pool whose --timeout budget does not start until the
# HTTP call does. Head and tail are both kept, because the address hides in the footer.
SCREEN_CHARS = 64_000

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
# The biggest single level an urgency answer must hold before it counts as a reading at
# all. Five levels flat is 0.2 each; below this the answer says nothing about urgency.
URGENCY_FLOOR = 0.3
URGENCY_MIN_CONFIDENCE = 0.2
# Jev's own calibration on the lane answer, below which a disposal lane is not acted on
# unseen. The same number triage.py uses to lift "ignore" back to "queue".
CALIBRATED_ENOUGH = 0.5


# Hints, so a plain-text mail is not put through a decoder it does not need.
_QP_HINT = re.compile(r"=(?:[0-9A-Fa-f]{2}|\r?\n)")
_PERCENT_HINT = re.compile(r"%[0-9A-Fa-f]{2}")
# 16 characters is 12 bytes, and _b64_text refuses anything that is not at least 8
# characters of printable text: "owner@recip.test" base64s to 22, and a shorter threshold
# is what it takes to see the address in a tracking link.
_BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/=_-])")
_URL_QUERY = re.compile(r"(?i)\bhttps?://[^\s<>\"'()\[\]]+")


def _clip(text: str, limit: int) -> str:
    """Keep the head and the tail, the way privacy.redact truncates: a list footer lives
    at the end, and screening only the first N characters would never see it."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n[…]\n" + text[-half:]


def _b64_text(run: str) -> Optional[str]:
    """The text a base64 run decodes to, or None when it does not decode to text."""
    padded = run.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < 8 or any(not (c.isprintable() or c in "\r\n\t") for c in text):
        return None
    return text


def readable(text: str) -> str:
    """Undo the transfer encodings mail actually arrives in, before anything reads it.

    Both screens in this module — the redactor and the credential gate — only recognise
    plain text, and real mail is not plain text. The same recipient address in one
    newsletter footer reached the API three ways at once: redacted where it was written
    out, percent-encoded in the unsubscribe URL, and base64'd in the tracking link. A
    quoted-printable body writes `@` as `=40` and can split an address across a soft line
    break; a base64 MIME body carried `api_key = sk-...` whole past the gate that exists
    to stop exactly that. privacy.normalize() already folds Unicode look-alikes for this
    same reason — this is that reasoning extended to the encodings mail uses.

    Each step falls back to its input, so a body that is not encoded is returned unharmed.
    A plain-text body that happens to contain `=` and two hex digits will decode as
    quoted-printable; that costs a character or two of a sentence, and the alternative
    costs the address.
    """
    out = text
    if _QP_HINT.search(out):
        try:
            out = quopri.decodestring(out.encode("utf-8")).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
            out = re.sub(r"=\r?\n", "", out)
    if _PERCENT_HINT.search(out):
        try:
            out = urllib.parse.unquote(out, errors="strict")
        except UnicodeDecodeError:
            pass
    out = html.unescape(out)
    return _BASE64_RUN.sub(lambda m: _b64_text(m.group(0)) or m.group(0), out)


def _drop_url_queries(text: str) -> str:
    """A URL's host and path carry the lane signal; its query string is where an address
    hides. `?e=<the recipient>&h=9f3c` is the ordinary shape of a list footer, not an
    edge case, and it survives truncation because the footer is the tail."""
    def _strip(match: "re.Match[str]") -> str:
        url = match.group(0)
        head = re.split(r"[?#]", url, 1)[0]
        return head + "?[redacted]" if head != url else url
    return _URL_QUERY.sub(_strip, text)


def _field(value: Any, limit: int) -> str:
    """Decode, drop URL queries, redact, cap — in that order. Redacting first screens a
    string nobody will ever read, and lets every encoded copy through."""
    text = _clip(str(value or "").strip(), SCREEN_CHARS)
    return privacy.redact(_drop_url_queries(readable(text)), limit)


# A real header, in the headers region a sender cannot fake into a body.
_LIST_HEADER = re.compile(r"(?i)\blist-(?:unsubscribe|id)\b")
_BULK_WORDS = re.compile(r"(?i)(unsubscribe|list-unsubscribe|opt[- ]?out|preferences center)")


def _has_unsubscribe_header(headers: str) -> bool:
    """A List-Unsubscribe header, and nothing else.

    This used to regex the first 800 characters of the BODY and store the answer under a
    header's name. A person writing "could you unsubscribe me from the weekly digest?"
    was then reported to Jev, as fact, as list mail carrying a header it did not have —
    and both of those fields push toward `promotional`, which is the one failure this
    module exists to prevent.
    """
    return bool(_LIST_HEADER.search(headers))


def _mentions_unsubscribe(sender: str, subject: str, body: str) -> bool:
    """The body-text hunch, under its own name. It is a guess, so it may not set
    sender_class and it is not reported as a header."""
    return bool(_BULK_WORDS.search(f"{sender} {subject} {body[:800]}"))


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


def _received(value: Any) -> str:
    """The timestamp, never the header it was cut from.

    A `Received:` header carries the recipient's own address and the internal IP of every
    hop, and this was the one caller-supplied string in the state block that was neither
    redacted nor capped — a long Received chain could also push the whole state past the
    client's size ceiling and silently take the message out of Jev. The lane question
    needs the time and nothing else, so anything that does not parse as a time is not
    sent at all.
    """
    text = str(value or "").strip()[:400]
    if not text:
        return ""
    candidates = [text]
    if ";" in text:  # "from mx.a.test (10.0.0.1) by mx.b.test for <...>; Mon, 20 Sep ..."
        candidates.insert(0, text.rsplit(";", 1)[1].strip())
    for candidate in candidates:
        try:
            datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return candidate[:RECEIVED_CHARS]
        except ValueError:
            pass
        try:
            parsedate_to_datetime(candidate)
            return candidate[:RECEIVED_CHARS]
        except (TypeError, ValueError):
            pass
    return ""


def build_state(message: Mapping[str, Any], *, has_unsubscribe: Optional[bool] = None,
                replied_before: Optional[bool] = None) -> Dict[str, Any]:
    """The block Jev reads: what it says, plus the free signals that are not in the text.

    A mailbox address is not sent, in any encoding it could have arrived in. The domain
    and a locally-derived sender class carry what the lane question actually needs, and
    the header facts carry the rest.
    """
    sender = str(message.get("sender") or message.get("from") or "").strip()
    subject = _field(message.get("subject"), SUBJECT_CHARS)
    body = _field(message.get("content") or message.get("body") or message.get("snippet"), BODY_CHARS)
    received = _received(message.get("received") or message.get("date"))
    headers = str(message.get("headers") or "")
    if has_unsubscribe is None:
        has_unsubscribe = _has_unsubscribe_header(headers)
    if replied_before is None:
        replied_before = _thread_replied(message)
    return {
        "subject": subject or "(no subject)",
        "body": body or "(empty)",
        "from_domain": _sender_domain(sender),
        "sender_class": _sender_class(sender, bool(has_unsubscribe)),
        "received": received or "unknown",
        "has_unsubscribe_header": bool(has_unsubscribe),
        # The hunch, kept as a signal for Jev but named for what it is.
        "body_mentions_unsubscribe": _mentions_unsubscribe(sender, subject, body),
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


def blank_row() -> Dict[str, Any]:
    """The shape every return path emits.

    `jev mail` prints these rows as JSON to an agent. The early returns used to omit five
    of the thirteen keys, so a consumer reading `row["urgent_mass"]` or
    `row["runner_up_gap"]` — the two fields a caller is told to correct against — got a
    KeyError on exactly the rows that need a person to look.
    """
    return {
        "lane": None, "urgency": None, "needs_attention": False, "personal": None,
        "low_confidence": False, "confidence": 0.0, "sent_to_jev": False, "reason": "",
        "lane_probabilities": {}, "runner_up_gap": None, "urgency_confidence": 0.0,
        "urgent_mass": 0.0, "latency_ms": None,
    }


def classify(message: Mapping[str, Any], *, has_unsubscribe: Optional[bool] = None,
             replied_before: Optional[bool] = None, timeout: float = 5.0,
             transport: Optional[client.Transport] = None) -> Dict[str, Any]:
    """Read one message and say which lane it belongs in. Never raises."""
    result = blank_row()

    subject = str(message.get("subject") or "").strip()
    body = str(message.get("content") or message.get("body") or message.get("snippet") or "").strip()
    if not (subject or body):
        # Fail-open means a person looks, never that mail disappears. An attachment-only
        # mail, an HTML-only body the caller's parser did not fill, and a truncated sync
        # all land here, and all used to be filed as "unsorted" with no flag on them.
        result.update(lane=None, needs_attention=True,
                      reason="nothing to read (no subject or body); a person should look")
        return result

    # Decoded first. The gate reads plain text, and a base64 MIME body carrying an API
    # key, or "pass=77ord", walked past it untouched and was forwarded whole.
    probe = readable(_clip(f"{subject}\n{body}", SCREEN_CHARS))
    if privacy.is_sensitive(probe):
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
    except Exception as error:  # noqa: BLE001
        # The client turns the failures it knows into JevError. A transport can still
        # raise something else — http.client.IncompleteRead on a truncated reply is not
        # an OSError, and a nan --timeout reaches the socket as a ValueError — and that
        # came out of a function whose contract is "never raises". Through
        # ThreadPoolExecutor.map it also took every other message in the batch with it.
        # rerank.py already carries this guard; this was the one Jev caller without it.
        result.update(needs_attention=True,
                      reason=f"Jev call failed ({type(error).__name__}); a person should look")
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
    # An answer with no peak anywhere is not a quiet message, it is no reading: five levels
    # flat is 0.2 each and lands p_today on exactly URGENT_MASS, so the incident this
    # module was built from — flat spread, confidence 0.0, point estimate 2.73 — came back
    # as a confident interrupt with low_confidence False. urgency_confidence used to be
    # computed, reported, and consulted by nothing; it is one of the two gates now.
    urgency_readable = bool(mass) and max(mass.values()) >= URGENCY_FLOOR \
        and urgency_confidence >= URGENCY_MIN_CONFIDENCE

    lane_probs = {k: float(v) for k, v in (lane_answer.get("probabilities") or {}).items()}
    ranked = sorted(lane_probs.items(), key=lambda kv: -kv[1])
    # No distribution, or a single entry, is the one case with no evidence at all. Reading
    # it as a gap of 1.0 reported it as maximally confident and the low-confidence
    # machinery never fired; the client accepts `probabilities: {}`, so this shape arrives.
    gap = (ranked[0][1] - ranked[1][1]) if len(ranked) > 1 else 0.0
    lane = lane_answer["choice"]
    if lane not in LANES:
        # Unreachable through client.ask, which refuses a choice that was not offered
        # (client._check_answer). Kept as the guard for a client that stops validating, or
        # a caller that injects at the transport layer: an invented label must not be
        # filed. Covered by a test that bypasses the client on purpose.
        result.update(needs_attention=True, low_confidence=True,
                      reason=f"answered with an unknown lane ({lane}); a person should look")
        return result

    personal = float(answers["personal"]["noul"])
    confidence = float(lane_answer["confidence"])
    lane_unsure = gap < LOW_CONFIDENCE_GAP
    low_confidence = lane_unsure or not urgency_readable

    # A human being writing to you is never filed under junk, whatever the lane says.
    if personal >= PERSONAL_ENOUGH and lane in ("promotional", "spam"):
        lane, lane_unsure, low_confidence = "needs_reply", True, True

    # Urgency speaks for any lane a person owns, not just needs_reply. Every example this
    # module was built from — a card-declined alert, an OTP, a fraud notice — is `updates`
    # by its own taxonomy, and in that lane the whole urgency computation was inert: mass
    # 1.0 at "a person is blocked" returned needs_attention False.
    needs_attention = bool(lane in ("needs_reply", "updates")
                           and (p_today >= URGENT_MASS if urgency_readable else level >= 3.0))
    # Unsure mail is never filed away unseen — in any lane. Escalating only for needs_reply
    # and updates covered the two lanes where being wrong is harmless and skipped spam,
    # promotional and sales, where a misfile means nobody ever reads the message.
    if low_confidence:
        needs_attention = True
    # Jev's own calibration, the guard triage.py:139 already has. A peaked but uncalibrated
    # answer must not drop mail into a disposal lane on nobody's authority.
    if confidence < CALIBRATED_ENOUGH and lane in ("promotional", "sales", "spam"):
        needs_attention = True

    reason = [f"{lane}, urgency {level + 1:.1f}/5"]
    if urgency_readable:
        reason.append(f"urgent mass {p_today:.2f}")
        if p_none >= URGENT_MASS:
            reason.append(f"no-time-pressure mass {p_none:.2f}")
    else:
        # Naming the shape, not quoting the point estimate as though it were a reading.
        reason.append("urgency spread too flat to read")
    if lane_unsure:
        reason.append("unsure between lanes")

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
        "reason": ", ".join(reason),
    })
    return result


def classify_many(messages: Sequence[Mapping[str, Any]], *, workers: int = 8,
                  timeout: float = 6.0, transport: Optional[client.Transport] = None,
                  ) -> List[Dict[str, Any]]:
    """Sort a batch. Each message is one independent Jev call, run side by side."""
    from concurrent.futures import ThreadPoolExecutor

    def one(message: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            out = classify(message, timeout=timeout, transport=transport)
        except Exception as error:  # noqa: BLE001
            # Belt and braces behind classify's own guard. pool.map re-raises on the first
            # result, so one message that crashes used to lose every other message in the
            # batch — unsorted and unreported, with a traceback and no rows.
            out = blank_row()
            out.update(needs_attention=True,
                       reason=f"sorting failed ({type(error).__name__}); a person should look")
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
    # `is not None`, not truthiness: a call measured at 0 ms is a genuinely fast call, and
    # dropping it meant the percentile branch never ran under a fake transport at all.
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)
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