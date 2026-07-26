"""The host's legal documents: served from disk, templates as the floor.

A public service must show its terms, its privacy policy, and the Node
Policy at stable public URLs. The OPERATOR owns the words: files under
``<data_dir>/legal/`` (``terms.md``, ``privacy.md``) are served verbatim
when present. Until then the built-in TEMPLATES below answer — each one
headed by an unmissable notice that it is a template, not legal advice,
so no host accidentally ships placeholder text as if counsel wrote it.
The Node Policy is code-owned (it is enforced by the hygiene machinery)
and always served from :mod:`oolu.nodeplace`.

**The privacy promise is renegotiated in the open** (agents-expansion
plan, invariant 13). Privacy VERSION 2 amends the original "No
advertising" promise for the ad house (A4): sponsored placements on the
News and Poll surfaces only, always labeled, matched inside the
platform, revenue shared with contributors — and "no sale of personal
data" stands untouched. The change is versioned, and
:class:`LegalAcceptanceStore` records each member's explicit acceptance;
nothing sponsored renders to a member who has not accepted the current
version. The old promise was never silently rewritten — this docstring,
the version number, and the acceptance flow ARE the renegotiation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

# Versioned templates: acceptance binds to a number, never a vibe.
# Bump a version only with a deliberate amendment beside it.
LEGAL_VERSIONS = {"terms": 1, "privacy": 2}

TEMPLATE_NOTICE = (
    "> **TEMPLATE — NOT LEGAL ADVICE.** This is placeholder text shipped\n"
    "> with OoLu so the URL answers. The operator of this host must have\n"
    "> their own counsel review and replace it (put the final text in\n"
    "> `<data_dir>/legal/` — see docs/operations.md) before offering the\n"
    "> service commercially.\n"
)

TERMS_TEMPLATE = (
    TEMPLATE_NOTICE
    + """
# Terms of Service (template)

1. **The service.** This host runs OoLu, a workflow assistant that plans
   and executes tasks through nodes its users create and share.
2. **Accounts.** You register with a verified e-mail address and are
   responsible for what happens under your account. One person per
   account; keep your password to yourself.
3. **Your content.** Files, messages, and nodes you create remain yours.
   You grant the host the rights needed to store and process them to
   provide the service, and nothing more.
4. **The Node Policy.** Publishing nodes binds you to the Node Policy
   (served at /v1/legal/node-policy): no clones, no fraud, no zombies.
   Violations lead to restriction or removal as the policy states.
5. **Payments.** Where charging is open, prices are shown before you
   commit; noder earnings and consumer charges follow the marketplace
   records, which both sides can audit.
6. **Termination.** You may delete your account at any time (Settings →
   Privacy & data); the operator may disable accounts that break these
   terms. Financial records the law requires are retained.
7. **Liability.** The service is provided as-is; the operator's
   liability is limited to the extent the law allows.
"""
)

PRIVACY_TEMPLATE = (
    TEMPLATE_NOTICE
    + """
# Privacy Policy (template)

1. **What is stored.** Your account (username, verified e-mail, sign-in
   identities), your OoLu conversation, your messages with friends, your
   files, your task runs and their audit trail, model-usage totals, and
   — where payments are configured — card metadata (never card numbers)
   and earnings records.
2. **What it is used for.** Providing the service: running your tasks,
   syncing your conversation across devices, delivering messages,
   metering plan allowances, and paying noders and contributors.
   Advertising exists only as the clearly labeled sponsored placements
   described in section 7 — nowhere else. No sale of personal data:
   advertisers never receive it.
3. **Who sees it.** Model providers receive the content of turns your
   plan or keys route to them. The payment processor sees what payment
   processing requires. Nobody else.
4. **Export.** Settings → Privacy & data → "Download my data" returns
   everything above as one JSON document (GET /v1/account/export).
5. **Erasure.** "Delete my account" erases your messages, conversation,
   sign-in identities, verification records, and card metadata, and
   permanently disables the account. Append-only records the service is
   legally required to keep (the tamper-evident audit chain, financial
   ledgers) are retained; they are minimal and pseudonymous.
6. **Retention.** Execution logs follow your retention setting
   (account.log_retention_days). Backups age out on the operator's
   schedule (see the operations runbook).
7. **Advertising (privacy version 2).** Sponsored placements may appear
   on the News and Poll surfaces only, and are always labeled as
   sponsored. Matching happens inside the platform: with your
   personalization consent it may use your own reading and voting
   signals; without it, matching is contextual only (the content's
   genre). Advertisers receive aggregate campaign statistics — never
   your identity or personal data. Advertising revenue is shared with
   the members whose contributions the placements ran against. No
   sponsored placement is shown to you until you accept this version
   of the policy; declining costs you nothing but the ads.
"""
)

_FILES = {"terms": "terms.md", "privacy": "privacy.md"}
_TEMPLATES = {"terms": TERMS_TEMPLATE, "privacy": PRIVACY_TEMPLATE}


def legal_document(kind: str, *, legal_dir: str | Path | None = None) -> str:
    """The operator's document when one exists, the marked template
    otherwise. ``kind`` is "terms" or "privacy"."""
    if kind not in _FILES:
        raise KeyError(f"unknown legal document: {kind}")
    if legal_dir is not None:
        path = Path(legal_dir) / _FILES[kind]
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            pass  # unreadable operator file: the template still answers
    return _TEMPLATES[kind]


_ACCEPT_SCHEMA = """CREATE TABLE IF NOT EXISTS legal_acceptances (
    tenant_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    document TEXT NOT NULL,
    version INTEGER NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, principal, document)
)"""


class LegalAcceptanceStore:
    """Who accepted which version of which document, when.

    A legal-basis record: it is retained like the audit chain (privacy
    §5's "legally required records"), not erased with the account —
    the fact that consent was given is exactly what a dispute needs.
    Acceptance is monotone per document: the newest accepted version
    stands; accepting an old version after a new one is a no-op.
    """

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_ACCEPT_SCHEMA)

    def accept(
        self, *, tenant: str, principal: str, document: str, version: int
    ) -> int:
        """Record the acceptance; return the standing accepted version."""
        if document not in LEGAL_VERSIONS:
            raise KeyError(f"unknown legal document: {document}")
        standing = self.accepted_version(
            tenant=tenant, principal=principal, document=document
        )
        if standing is not None and standing >= int(version):
            return standing
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM legal_acceptances WHERE tenant_id = ?"
                " AND principal = ? AND document = ?",
                (tenant, principal, document),
            )
            db.execute(
                """INSERT INTO legal_acceptances
                     (tenant_id, principal, document, version, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    tenant,
                    principal,
                    document,
                    int(version),
                    self._clock().isoformat(),
                ),
            )
        return int(version)

    def accepted_version(
        self, *, tenant: str, principal: str, document: str
    ) -> int | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT version FROM legal_acceptances
                   WHERE tenant_id = ? AND principal = ? AND document = ?""",
                (tenant, principal, document),
            ).fetchone()
        return int(row["version"]) if row is not None else None

    def is_current(
        self, *, tenant: str, principal: str, document: str
    ) -> bool:
        accepted = self.accepted_version(
            tenant=tenant, principal=principal, document=document
        )
        return accepted is not None and accepted >= LEGAL_VERSIONS[document]
