"""Scrubbing — the safety gate between the engine and any stored/shared knowledge.

Even though the knowledge layer only ever stores ABSTRACT data (import/package names
and pre-normalized error signatures), this module is the defense-in-depth that makes
the "never store anything sensitive" guarantee enforceable rather than aspirational.
Every value passes through here before it is written.

Two checks:
  * ``scrub(text)`` removes known secret/PII shapes (keys, tokens, emails, IPs, file
    paths, URL credentials, KEY=value secrets), replacing them with placeholders.
  * ``is_safe_to_store(text)`` is True only when scrubbing would change nothing — i.e.
    no detectable secret remains. The client REFUSES to store anything that isn't.

And a stricter gate for identifiers: ``is_safe_identifier`` accepts only PyPI/import
name shapes, so a "package name" can never smuggle a path or secret into the store.
"""

from __future__ import annotations

import re

# Order matters: most specific shapes first, generic last.
_SCRUBBERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY>"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "<API_KEY>"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"), "<GH_TOKEN>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"), "<JWT>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "bearer <TOKEN>"),
    (re.compile(r"[A-Za-z]:\\(?:[^\\\s\"']+\\?)+"), "<PATH>"),                       # windows
    (re.compile(r"/(?:home|Users|root|var|etc|tmp|opt|mnt|srv)/[^\s\"':]+"), "<PATH>"),  # unix
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    (re.compile(r"://[^/\s:@]+:[^/\s:@]+@"), "://<CREDS>@"),                          # url creds
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD))\s*[=:]\s*\S+"),
     r"\1=<REDACTED>"),
]

# PyPI / import identifier shape: starts and ends alphanumeric, dots/dashes/underscores
# inside, reasonable length. Rejects anything with spaces, slashes, or secret material.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_MAX_IDENTIFIER_LEN = 100


def scrub(text: str) -> str:
    """Replace detectable secrets/PII with placeholders. Idempotent."""
    out = text
    for pattern, replacement in _SCRUBBERS:
        out = pattern.sub(replacement, out)
    return out


def is_safe_to_store(text: str) -> bool:
    """True only when no detectable secret/PII is present (scrubbing is a no-op)."""
    return scrub(text) == text


def is_safe_identifier(name: str) -> bool:
    """True for clean PyPI/import-style names only — the strict gate for the fields
    we store verbatim (import_name, package_name)."""
    return bool(name) and len(name) <= _MAX_IDENTIFIER_LEN and bool(_IDENTIFIER_RE.match(name))
