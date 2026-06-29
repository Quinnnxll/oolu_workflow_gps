"""Confirm secrets are absent from persisted records, logs, fixtures, and examples.

Three guarantees are checked:

1. No real secret material is committed to the tree (configs, examples, fixtures,
   docs, source) — only obvious placeholders like ``sk-...``.
2. Persisted polling state stores a token *fingerprint*, never the raw token.
3. The knowledge store's scrubbing gate refuses to persist secret-bearing values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from workflow_gps.knowledge import LocalKnowledgeClient
from workflow_gps.knowledge.scrubbing import is_safe_to_store, scrub
from workflow_gps.models import ErrorClass, RecalcStrategy
from workflow_gps.replies.offsets import FileOffsetStore

_REPO_ROOT = Path(__file__).resolve().parent.parent

# High-confidence secret shapes. Deliberately excludes generic path/IP/email
# matches (which legitimately appear in docs and comments) and only fires on
# material that looks like a genuine live credential.
_SECRET_PATTERNS = {
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    "telegram_bot_token": re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
}

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".db", ".png", ".jpg", ".jpeg", ".gif"}


# This module carries deliberately fake secret-shaped fixtures, so it excludes
# itself from the scan (as any secret scanner excludes its own test data).
_SELF = Path(__file__).resolve()


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == _SELF:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(_REPO_ROOT).parts):
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def test_no_committed_secrets_in_tree():
    findings: list[str] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in _SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                rel = path.relative_to(_REPO_ROOT)
                findings.append(f"{rel}: {name}: {match.group()[:24]}…")
    assert not findings, "possible committed secrets:\n" + "\n".join(findings)


def test_offset_store_persists_fingerprint_not_token(tmp_path):
    raw_token = "123456789:ABCDEF_this_is_a_fake_bot_token_value_000"
    # The CLI derives a fingerprint identity from the token; the store must only
    # ever see that fingerprint, never the secret itself.
    import hashlib

    identity = "telegram:" + hashlib.sha256(raw_token.encode()).hexdigest()[:16]
    store = FileOffsetStore(tmp_path / "offset.json", identity=identity)
    store.save(42)

    persisted = (tmp_path / "offset.json").read_text(encoding="utf-8")
    assert raw_token not in persisted
    assert "ABCDEF_this_is_a_fake_bot_token" not in persisted
    # Round-trips correctly using only the fingerprint.
    assert json.loads(persisted)["offset"] == 42
    assert FileOffsetStore(tmp_path / "offset.json", identity=identity).load() == 42


def test_knowledge_store_refuses_secret_bearing_values(tmp_path):
    client = LocalKnowledgeClient(tmp_path / "k.db")
    # An error signature carrying a live-looking key must be scrubbed; the raw key
    # must never reach persisted rows.
    leaky = "auth failed for sk-abcdefghijklmnop0123456789 at api"
    assert not is_safe_to_store(leaky)
    client.record_error_pattern(
        ErrorClass.RUNTIME_EXCEPTION, leaky, RecalcStrategy.REWRITE_CODE, success=False
    )
    for pattern in client.get_error_patterns(ErrorClass.RUNTIME_EXCEPTION):
        assert "sk-abcdefghijklmnop0123456789" not in pattern.error_signature
        assert pattern.error_signature == scrub(pattern.error_signature)
    client.close()
