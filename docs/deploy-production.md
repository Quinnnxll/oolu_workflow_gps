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

1. **DNS → Add record**: type `A`, name `app` (or whatever subdomain),
   content = the droplet's IP, proxy status **Proxied** (orange cloud).
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

- **caddy** answers `https://$OOLU_DOMAIN`, renews certificates, and
  proxies to the gateway.
- **oolu** runs `oolu host` — the multi-user gateway with local
  accounts. `DATABASE_URL` switches the durable layer to the
  PostgreSQL adapter (the production substrate; money-grade invariants
  ride this connection). `OOLU_BLOB_S3_*` switches blobs to R2.
- **postgres** holds every tenant's truth. The compose file keeps it
  off the public network entirely.

Sign in at `https://app.example.com` with the admin credentials from
the log, then create user accounts (or wire Google sign-in / OIDC —
the gateway supports both; see the `oolu host --help` flags).

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

# Updating OoLu:
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

The durable schema migrates itself forward on boot (versioned,
append-only migrations); a database newer than the code refuses to
open rather than corrupt.

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
