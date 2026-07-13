# Threat model

Workflow-GPS synthesizes and executes code. Generated scripts and cached scripts must therefore be treated as untrusted input, even when they previously succeeded.

## Generated code

Model output can be destructive, deceptive, resource-intensive, or simply wrong. The execution contract limits the accepted result channel, but does not make code safe. Production use should select the Docker backend, enforce CPU, memory, time, filesystem, and process limits, and avoid mounting host paths.

## Dependency supply chain

Automatic installation can select malicious, compromised, or typo-squatted packages. Installations should use a pinned, allow-listed index or mirror, locked versions and hashes where possible, and auditable dependency policy. Package installation remains a privileged trust decision even though it is separated from execution.

## Docker isolation

Containers reduce exposure but are not a perfect security boundary. Images should be minimal, patched, pinned by digest, run as a non-root user, use a read-only root filesystem, drop Linux capabilities, and expose no Docker socket or host credentials.

## Network severance

Dependency installation may temporarily require network access. Synthesized code must run only after that access is severed. Operators should verify enforcement outside the process itself and deny access to metadata services, local networks, and control-plane endpoints.

## Node network egress

The sandbox stays severed; when a node needs the web it goes through the host-side HTTP executor, which is the honest enforcement point. Every registered node carries an egress grant on its account (`network_hosts`): the exact public hosts its HTTP actions may reach, given — and withdrawable — by the humans responsible for the node. The grant is stamped onto the node's actions when a contract is prepared for execution (held contracts are stamped at approval time, so a run always honors current consent) and enforced on every redirect hop, inside the machine-level allowlist and the always-on SSRF guard. An empty grant fails closed: a registered node reaches nothing until someone consents. Grants name bare public hostnames only — never URLs, ports, wildcards, IP literals, or localhost — and are capped at a short, reviewable list. Ad-hoc actions a user submits directly remain governed by the machine policy alone; the grant wall is for code that answers to someone else.

## Cache poisoning

A successful run is not proof that a script is benign for every equivalent-looking task. Cache keys include intent, engine and cache-schema versions, prompt policy, routing models, backend identity, and package index. Cached scripts are bypassed after two recorded failures. Local database permissions, provenance, integrity checks, inspection, expiry, and revocation should be strengthened before shared caches are introduced.

## Secret leakage

Prompts, generated code, logs, exception text, cached scripts, and result payloads can retain secrets. Secrets should not be placed in intents, environment variables exposed to the sandbox, mounted files, telemetry, or cache metadata. Logs and cache databases need restrictive permissions and an explicit retention policy.

Messaging credentials such as Telegram bot tokens must be supplied through a protected
environment or secret manager, never committed in reply-rule files. Reply templates and
trusted context can disclose location or operational status; keep them local, restrict
file permissions, and use exact context-gated rules for sensitive statements. A matched
rule sends immediately, so rule changes require the same review as application code.

Learned replies can preserve personal or sensitive conversation text and can be poisoned
by an incorrect manual demonstration. Learning is therefore local, scoped per account
connection, limited to a short inbound/outbound pairing window, and excludes replies sent
by the bot itself. Operators should protect, inspect, and periodically remove the learned
reply database. High-impact statements such as payments, identity claims, or emergency
instructions should not be auto-replied without an additional approval policy.

## Local CLI skills

The initial CLI skill adapter runs an explicitly allow-listed local executable with
`shell=False`, a reduced environment, workspace state guards, timeouts, and write
approval. These controls do not create an operating-system sandbox: an approved
executable or interpreter can still access resources outside its working directory by
its own behavior. Treat recorded commands as trusted local code. Production execution of
untrusted skills requires the Docker or future restricted-worker composition, and CLI
output must be treated as potentially sensitive audit data.

## Placing orders and bookings (spending money)

OoLu can act on external sites — order goods, reserve a table, book a room —
which means it can spend the user's money. Every such action is a RESERVED
operation whose only release valve is the payment-consent gate
(`billing/authorization.py`), and that gate has two locks the account holder
alone controls:

1. **Consent to the exact amount.** The authorization request records the
   merchant, the amount, the currency, and a plain-language description.
   Releasing it requires re-stating the amount to the cent — a draft order
   that silently grew cannot be waved through by habit.
2. **A second factor.** The consent must carry a fresh RFC-6238 TOTP code
   from the user's authenticator (`identity/totp.py`, secret sealed at rest
   in `identity/totp_store.py` with the install's machine key). A stolen
   session token is not enough to spend money; an account with no confirmed
   second factor cannot authorize a payment at all.

An order action must not execute until its authorization record reads
`authorized`; that record is the durable proof of consent. Pre-launch the
`LaunchGuard` keeps the real transaction port shut, so the whole flow is
verifiable end to end while no money can move. Orders are account-scoped:
one person on a shared host can neither see nor release another's.

Out of scope here: the site-automation that actually completes a checkout
on an arbitrary third-party site. This layer is the *authorization* — the
guarantee that nothing spends money without the user's amount-consent and
second factor — not the browser driver that fills the merchant's form.
