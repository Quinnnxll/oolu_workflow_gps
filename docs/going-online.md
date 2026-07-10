# Going online — the public OoLu host

Milestone C of `coming-alive-plan.md`. The code side ships in this repo:
`oolu host` is the multi-tenant gateway with local accounts, Google
sign-in, self-serve registration, and a Postgres-backed durable store.
This document is the operations half — what to provision and how to wire
it — plus how a desktop install pairs with the host so the sign-in screen
never asks for a server.

## What you provision (once)

1. **A domain** — e.g. `oolu.example` with an `app.` record for the host.
2. **A small VM** (2 vCPU / 4 GB is plenty to start) with ports 80/443
   open. Anything that runs Python 3.11 works.
3. **A managed PostgreSQL** database (or Postgres on the same VM to
   start). SQLite remains the default and is fine for a single-node
   pilot; Postgres is what lets several app clients share one online
   store.
4. **A Google OAuth client** (type: *Web application* for the online
   host, with `https://app.oolu.example/v1/auth/google/callback` as the
   authorized redirect URI — the desktop keeps its own *Desktop app*
   client).

## Running the host

```bash
python -m venv .venv && .venv/bin/pip install -e ".[serve,oidc,postgres]"

export OOLU_HOST_SECRET="<32+ character random string, kept forever>"
export OOLU_ADMIN_PASSWORD="<the first admin's password>"
export OOLU_DATABASE_URL="postgresql://oolu:...@db-host/oolu"   # optional
export OOLU_GOOGLE_CLIENT_ID="....apps.googleusercontent.com"    # optional
export OOLU_GOOGLE_CLIENT_SECRET="GOCSPX-..."                    # optional
export OOLU_MAIL_URL="https://api.resend.com/emails"             # outbound mail
export OOLU_MAIL_KEY="re_..."                                    #  (Resend-style
export OOLU_MAIL_FROM="OoLu <hello@oolu.example>"                #   JSON API)
export OOLU_PLATFORM_ANTHROPIC_KEY="sk-ant-..."   # the hosted subscription
export OOLU_PLATFORM_OPENAI_KEY="sk-..."          #  brain's keys (optional)
export OOLU_STRIPE_KEY="sk_live_..."              # real payments (optional)
export OOLU_STRIPE_WEBHOOK_SECRET="whsec_..."     #  + its webhook endpoint

.venv/bin/oolu host \
  --data /var/lib/oolu \
  --port 8788 \
  --open-registration \
  --allow-origin "https://app.oolu.example" \
  --allow-origin "http://127.0.0.1:5173"      # any desktop origins you serve
```

Notes:

- `OOLU_HOST_SECRET` signs session tokens. Rotating it signs everyone
  out; losing it does too. Keep it in your secret manager.
- `--open-registration` enables `POST /v1/auth/register`. With a mail
  sender configured (the three `OOLU_MAIL_*` variables, or
  `OOLU_MAIL=console` to log mail during development) registration is
  verification-first: the account gets no session until the mailed
  6-digit code comes back through `POST /v1/auth/verify`, and "Forgot
  password?" works via `/v1/auth/reset/request` + `/v1/auth/reset/confirm`.
  Without a mail sender the register route still answers with an
  immediate token (fine for private testing); a `--global-service` host
  refuses that combination outright — public registration requires
  verified e-mail. Leave it off for a private host.
- The gateway already honours `x-forwarded-proto`, so behind TLS the
  Google redirect URI derives as `https://...` automatically.
- **The hosted subscription brain**: set `OOLU_PLATFORM_ANTHROPIC_KEY`
  (and/or `OOLU_PLATFORM_OPENAI_KEY`) and tenants whose `model.source`
  is `subscription` are answered through the platform's keys — Claude
  first — metered per tenant against their plan's monthly allowance
  (free: none; plus/pro/enterprise: $5/$20/$100). The keys follow the
  environment on every boot: set means stored (encrypted), unset means
  removed, so rotation is a restart. `GET /v1/usage/model` shows a
  tenant their books and remaining allowance.
- **Real payments**: set `OOLU_STRIPE_KEY` and the card vault + payout
  adapter talk to Stripe instead of the test doubles; add
  `OOLU_STRIPE_WEBHOOK_SECRET` (the endpoint's `whsec_...`) and point a
  Stripe webhook at `POST /v1/webhooks/stripe` for refunds, disputes,
  and payout confirmations. Charging real cards additionally needs the
  `--transactions` flag (which refuses to start without the Stripe key)
  — and even then each class of work charges only after its prices
  settle and its function has enough verified successes (the launch
  guard), on a PostgreSQL durable with production identity
  (`require_production_money`).
- **KYC reviews**: reviewers are accounts holding the `kyc:review`
  permission (the bootstrap admin's `*` covers it; grant a dedicated
  `kyc-reviewer` role for anyone else). Their inbox is
  `GET /v1/kyc/reviews`, and the Work screen shows the queue with
  approve/reject right on the row. `OOLU_KYC_TRUSTED_DOMAINS`
  fast-tracks applications from domains you've verified out of band.

## TLS termination

Put Caddy (simplest) or nginx in front. Caddy in two lines:

```
app.oolu.example {
    reverse_proxy 127.0.0.1:8788
}
```

Caddy provisions certificates automatically and sets
`X-Forwarded-Proto: https`, which is exactly what the gateway expects.

## Keeping it up (systemd)

```ini
[Unit]
Description=OoLu host
After=network-online.target

[Service]
User=oolu
WorkingDirectory=/opt/oolu
EnvironmentFile=/etc/oolu/host.env      # the exports above
ExecStart=/opt/oolu/.venv/bin/oolu host --data /var/lib/oolu \
    --port 8788 --open-registration --allow-origin https://app.oolu.example
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Pairing the desktop app with the host

Set one environment variable before launching the desktop shell:

```powershell
# Windows PowerShell
$env:OOLU_SERVER_URL = "https://app.oolu.example"
.\setup.bat
```

```bash
# macOS / Linux
OOLU_SERVER_URL="https://app.oolu.example" ./setup.sh
```

The local gateway then advertises the pairing on its public
`GET /v1/client-config`, and the sign-in screen stops asking for a
server: it shows *"Signing in to app.oolu.example"*, offers username +
password, **Create one** (when the host opened registration), and
**Continue with Google** (when the host configured a client). The
engine and all execution stay on the loopback either way — the online
account is identity, not a data migration.

`GET /v1/client-config` answers, for any client that asks:

```json
{
  "server": "https://app.oolu.example",
  "google": true,
  "registration": true,
  "verification": true
}
```

(`verification` says registering here ends with a mailed-code step, so
the sign-in screen knows to show it.)

## What the host unlocks next

- **Public-API webhook deliveries** — third parties receive signed run
  events at their own URLs; outbound needs nothing, but issuing API keys
  to outsiders only makes sense with a public host.
- **The Friends pane** — person-to-person conversations ride the same
  host accounts.
