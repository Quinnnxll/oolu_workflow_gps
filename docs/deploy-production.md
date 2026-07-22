# Launching OoLu in production

The stack this guide builds — each piece and the job it does:

```text
Cloudflare DNS  (the name, the proxy shield, TLS to the edge)
      |
DigitalOcean droplet  (one VPS, Docker on it)
      |
Caddy  (automatic HTTPS, reverse proxy)
      |
OoLu gateway  (multi-user accounts, tenant_id walls everywhere)
      |                         \
PostgreSQL  (durable truth)     Cloudflare R2  (blobs: files, CAD exports)
```

Model access is real from day one: every user can paste their own
Anthropic/OpenAI key in Settings (`own-api`), and the host can
optionally carry platform keys so paid plans get the hosted
"subscription" brain. Both paths go through the same metered router.

## 0. What you need

- A domain on Cloudflare (free plan is fine).
- A DigitalOcean account. A $12–24/mo droplet (2 GB+ RAM) runs the
  base stack; add RAM if you build the image with the `cad` extra.
- A Cloudflare R2 bucket (10 GB free tier).
- Optionally: an Anthropic and/or OpenAI API key for the platform brain.

## 1. The droplet

Create an Ubuntu 24.04 droplet (enable the free cloud firewall), then:

```bash
ssh root@<droplet-ip>
# Docker, the official way:
curl -fsSL https://get.docker.com | sh
# The cloud firewall (or ufw) should allow ONLY 22, 80, 443.
```

Clone the repository (or copy a release) onto the droplet:

```bash
git clone <your-repo-url> oolu && cd oolu
```

## 2. Cloudflare DNS

In the Cloudflare dashboard for your domain:

1. **DNS → Add record**, once per hostname — the deployment has two
   doors, so create (at least) two `A` records, both content = the
   droplet's IP, proxy status **Proxied** (orange cloud):
   - `app` — the user-facing chat shell (`OOLU_DOMAIN`). Add `www.app`
     too if you want that spelling to work, and list both in
     `OOLU_DOMAIN` separated by `", "`. Caveat: Cloudflare's free
     universal certificate covers `*.example.com` but NOT deeper names
     like `www.app.example.com` — make that record **DNS only** (grey
     cloud, Caddy's own certificate serves it directly) or buy
     Advanced Certificate Manager.
   - `admin` — the operator console (`OOLU_ADMIN_DOMAIN`): sign-in,
     users, health, runs.
2. **SSL/TLS → Overview**: set the mode to **Full (strict)**. This is
   the one setting people miss: it makes Cloudflare speak HTTPS to
   Caddy's real certificate instead of plain HTTP to your box.

Caddy will obtain its own Let's Encrypt certificate on first boot (the
HTTP-01 challenge passes through the proxy), and Cloudflare terminates
edge TLS in front of it — encrypted end to end.

## 3. R2 for blobs

1. **R2 → Create bucket** — e.g. `oolu-blobs`. No public access.
2. **R2 → Manage API Tokens → Create API Token**: permission
   **Object Read & Write**, scoped to that bucket. Keep the Access Key
   ID and Secret Access Key.
3. Your S3 endpoint is `https://<account-id>.r2.cloudflarestorage.com`
   (shown on the bucket page).

OoLu's blob layer (the file drawer's large files and the CAD hand's
STEP/STL exports) is content-addressed: identical bytes deduplicate,
and every reference is a self-verifying `sha256:` id — R2 holds the
bytes, PostgreSQL holds the references.

## 4. Configure and launch

```bash
cp .env.production.example .env
nano .env        # fill in EVERY value; secrets via: openssl rand -base64 48
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs oolu   # first admin sign-in
```

That's the whole launch. What each service is doing:

- **caddy** answers `https://$OOLU_DOMAIN` (the app) and
  `https://$OOLU_ADMIN_DOMAIN` (the operator console), renews
  certificates for both, and proxies each to the same gateway — which
  picks the face by the Host header: the app domain serves the chat
  shell, the admin domain serves the operator page.
- **oolu** runs `oolu host` — the multi-user gateway with local
  accounts. `DATABASE_URL` switches the durable layer to the
  PostgreSQL adapter (the production substrate; money-grade invariants
  ride this connection). `OOLU_BLOB_S3_*` switches blobs to R2.
- **postgres** holds every tenant's truth. The compose file keeps it
  off the public network entirely.

Sign in at `https://admin.example.com` with the admin credentials from
the log, then create user accounts (or wire Google sign-in / OIDC —
the gateway supports both; see the `oolu host --help` flags). Users
chat at `https://app.example.com` — same accounts, the product face.

## 5. Real model APIs

Two doors, both real, both metered:

- **Per-user (own-api)** — nothing to configure server-side. Each user
  opens Settings → Model, pastes their Anthropic or OpenAI key, and
  `model.source` switches to `own-api`. Keys are encrypted at rest in
  the keyring (per tenant), never logged, and every call is metered
  against the user's own spending cap.
- **Platform (subscription)** — set `OOLU_PLATFORM_ANTHROPIC_KEY`
  and/or `OOLU_PLATFORM_OPENAI_KEY` in `.env`. Tenants on a paid plan
  then get the hosted brain inside their plan's monthly allowance
  (Claude first, per the plan's order). Rotation is a restart: the
  keys follow the environment on every boot — set is stored encrypted,
  unset is removed.

### Mail: reset codes, e-mailed passwords, verification

Three account doors ride outbound mail — the **reset code**
(`/v1/auth/reset/request` → `/confirm`), the **e-mailed new password**
(`/v1/auth/reset/password`), and verification-first registration. They
all answer 404 "not offered" until the host has a sender. Pick ONE in
`.env` (the compose file already passes every variable through):

```bash
# The classic SMTP mailbox — a Gmail app password, Office 365, or your
# registrar's mail. STARTTLS on 587 is the default; ssl uses 465.
OOLU_SMTP_HOST=smtp.example.com
OOLU_SMTP_USER=codes@example.com
OOLU_SMTP_PASSWORD=app-password-here
OOLU_MAIL_FROM=codes@example.com
# OOLU_SMTP_PORT=587        # optional
# OOLU_SMTP_SECURITY=starttls  # starttls | ssl | none

# — or a Resend-style HTTP JSON door:
OOLU_MAIL_URL=https://api.resend.com/emails
OOLU_MAIL_KEY=re_xxx
OOLU_MAIL_FROM=codes@example.com

# — or, for a dry run only, print codes to the server log:
OOLU_MAIL=console
```

Restart (`docker compose up -d`) and the doors open. Sends are
throttled per address (60 s cooldown, 10/day), codes are hashed,
expire in 30 minutes, and never say whether an address has an account.

### Phone sign-in ("continue with phone")

The phone door (`/v1/auth/phone/start` → `/verify`) texts a one-time
code; a new number gets an account born with a usable texted password.
It answers 404 until an SMS provider is configured:

```bash
# Twilio (the common case):
OOLU_TWILIO_ACCOUNT_SID=ACxxx
OOLU_TWILIO_AUTH_TOKEN=xxx
OOLU_SMS_FROM=+15551230000

# — or any generic JSON endpoint (POST {from, to, body}):
OOLU_SMS_URL=https://sms.example.com/send
OOLU_SMS_KEY=xxx
OOLU_SMS_FROM=+15551230000

# — or, development only:
OOLU_SMS=console
```

Texts cost real provider money, so the same per-number throttle binds,
and a half-configured Twilio refuses to boot rather than silently
falling back to a door it cannot speak through.

### "Continue with Google" on the app domain

The button appears on the sign-in screen as soon as a Google OAuth
client is configured; without one, username/password (accounts you
create from the admin console, or self-serve registration if you
enabled it) is the only door. Setup, once, in the
[Google Cloud Console](https://console.cloud.google.com/):

1. Pick (or create) a project → **APIs & Services → OAuth consent
   screen**: External, fill in the app name and your domain, publish.
   No extra scopes needed — OoLu asks only for the basic profile.
2. **APIs & Services → Credentials → Create Credentials → OAuth
   client ID**, application type **Web application**. Under
   **Authorized redirect URIs** add one per app hostname you serve:

   ```
   https://app.example.com/v1/auth/google/callback
   ```

   (add `https://www.app.example.com/...` too if that name serves).
3. Put the client id and secret in `.env` and restart:

   ```
   OOLU_GOOGLE_CLIENT_ID=<...>.apps.googleusercontent.com
   OOLU_GOOGLE_CLIENT_SECRET=GOCSPX-...
   ```
   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```

The flow is the RFC 8252 shape: the shell opens Google's consent page
in a new tab, Google redirects back to the gateway's own
`/v1/auth/google/callback`, and the shell collects the session on its
own channel — the token never rides the redirect.

## 6. Tenant isolation — what you're relying on

`tenant_id` is not a column convention here; it is a wall enforced in
every store and door:

- Every durable store (files, runs, nodes, accounts, graph projects,
  proposals, messages) is keyed and queried by tenant; cross-tenant
  reads answer 404, never 403 — existence is not leaked.
- Model keys, settings, budgets, and spending histories are
  tenant-scoped; one tenant's cap or key can never serve another.
- The project graph adds path-scoped territory INSIDE a tenant
  (owner grants; forbidden wins; fail closed).
- The test suite pins these walls (`test_account_privacy`,
  `test_project_graph`, the gateway suites) — they are regression-
  protected, not aspirational.

One droplet hosts many isolated tenants; separate *databases* per
tenant are not required for this model, but nothing prevents running
several stacks side by side if you want physical separation.

## 7. Backups and updates

```bash
# PostgreSQL: nightly dump (add to cron; ship it off-box).
docker compose -f docker-compose.prod.yml exec postgres \
  pg_dump -U oolu oolu | gzip > /root/backups/oolu-$(date +%F).sql.gz

# R2 already IS the off-box copy for blobs; version the bucket if you
# want point-in-time recovery.

# Updating OoLu by hand:
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

The durable schema migrates itself forward on boot (versioned,
append-only migrations); a database newer than the code refuses to
open rather than corrupt.

### Continuous deployment from GitHub

`.github/workflows/deploy.yml` automates the update: every push to
`main` (or a manual run from the Actions tab) SSHes into the droplet,
resets its checkout to `origin/main`, rebuilds the stack, and fails
the workflow if any container is not running afterwards. One-time
setup:

```bash
# 1. On your own machine, mint a dedicated deploy key (pick a real
#    passphrase — the workflow needs all three values below):
ssh-keygen -t ed25519 -a 100 -f deploy_key -C "github-deploy"

# 2. Authorize its PUBLIC half on the droplet:
ssh root@<droplet-ip> "cat >> ~/.ssh/authorized_keys" < deploy_key.pub
```

Then in the repository: **Settings → Secrets and variables → Actions
→ New repository secret**, three times:

| Secret            | Value                                          |
| ----------------- | ---------------------------------------------- |
| `DROPLET_IP`      | the droplet's public IP (the DNS origin)       |
| `SSH_PRIVATE_KEY` | the full contents of `deploy_key` (the file    |
|                   | without `.pub`), BEGIN/END lines included      |
| `SSH_PASSPHRASE`  | the passphrase you chose                       |

`SSH_PRIVATE_KEY` must keep its line breaks — a paste that collapses
the key onto one line fails at `ssh-add` with "error in libcrypto".
The safe way is the CLI, which preserves the file byte for byte:

```bash
gh secret set SSH_PRIVATE_KEY < deploy_key
```

(pasting into the web form also works IF every line survives, BEGIN
and END lines included). Delete the local `deploy_key` file once the
secret is stored. The
workflow keeps the key encrypted at rest everywhere: on the runner it
is loaded straight into a transient `ssh-agent` and the temporary key
file is removed before the first connection. The droplet needs its
clone to be able to `git fetch origin main` (a public repo just
works; a private one needs its own read-only deploy key or token on
the droplet). If your checkout lives somewhere other than
`/opt/oolu_workflow_gps` or you SSH as a non-root user, adjust `APP_DIR`
/ `DEPLOY_USER` at the top of the workflow file.

## 8. Hardening notes

- The droplet firewall allows 22/80/443 only; PostgreSQL and the
  gateway port are never published.
- `OOLU_HOST_SECRET` signs sessions — long, random, rotated only if
  compromised (rotation signs everyone out).
- The sandbox stays severed on a public host: synthesized code needs
  the Docker isolation backend; the gateway's `require_isolation`
  refuses to wire the script hand unsandboxed.
- Node egress is consent-gated per node (`network_hosts` grants), and
  the machine-level HTTP allowlist (`OOLU_HTTP_ALLOWLIST`) narrows the
  host's own hand — see `docs/THREAT_MODEL.md`.
- Cloudflare's proxy gives you DDoS absorption and lets you add
  rate-limiting/WAF rules without touching the box.
