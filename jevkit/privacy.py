"""The outbound boundary. Everything sent to Jev passes through here first.

Two tools: ``redact`` masks things that look like secrets or contact details, and
``is_sensitive`` says "do not send this at all". Callers that get a True from
``is_sensitive`` must skip Jev and take their fail-open path.
"""
from __future__ import annotations

import re
import unicodedata

_SECRET_WORDS = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|authorization\s*:|bearer\s+[a-z0-9._-]{8,}|password|passwd|"
    r"secret[_ -]?key|client[_ -]?secret|session[_ -]?cookie|credit[_ -]?card|card[_ -]?number|"
    r"\bcvv\b|\bssn\b|private[_ -]?key|BEGIN [A-Z ]*PRIVATE KEY)"
)
_TOKEN_SHAPES = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|apikey_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})\b"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_LONG_HEX = re.compile(r"\b[a-fA-F0-9]{32,}\b")


def normalize(text: str) -> str:
    """Fold look-alike and invisible characters so a gate cannot be dodged with Unicode."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) not in {"Cf", "Cc"} or c in "\n\t")


def is_sensitive(text: str) -> bool:
    probe = normalize(text)
    return bool(_SECRET_WORDS.search(probe) or _TOKEN_SHAPES.search(probe))


def redact(text: str, limit: int = 4000) -> str:
    out = normalize(text)
    out = _TOKEN_SHAPES.sub("[secret]", out)
    out = _LONG_HEX.sub("[hex]", out)
    out = _EMAIL.sub("[email]", out)
    out = _PHONE.sub("[phone]", out)
    if len(out) > limit:
        half = limit // 2
        out = out[:half] + "\n[…]\n" + out[-half:]
    return out
