# Operating an OoLu host

The runbook for keeping a public (or private-network) host healthy:
backups, monitoring, releases, retention, legal documents, and what to do
at 3 a.m. Provisioning and first boot live in `going-online.md`; this
document starts where that one ends.

## Backups

One command captures everything a restore needs:

```bash
oolu backup --data /var/lib/oolu --out /var/backups/oolu
```

It copies every SQLite database through the **online backup API** (safe
while the server is running) plus `machine.key` — the keyring's
encryption key, without which every stored model key is unreadable —
into one timestamped folder. Schedule it (systemd timer or cron), keep
at least 7 dailies and 4 weeklies, and store copies off the machine.

With PostgreSQL as the durable store (`OOLU_DATABASE_URL`), add:

```bash
pg_dump --format=custom "$OOLU_DATABASE_URL" \
    > /var/backups/oolu/host-$(date +%Y%m%d).pgdump
```

`oolu backup` still captures the local auxiliary databases and the key
file, and says so in its output.

**Restore drill** (do this once BEFORE you need it): copy a backup
folder to a scratch machine as its `--data` directory, start
`oolu host` against it, sign in, and confirm runs/files/messages are
there. A backup nobody has restored is a hope, not a backup.

## Monitoring

- **Liveness**: `GET /v1/health` is public and answers `{"status":"ok"}`.
  Point your uptime checker at it (1-minute interval is plenty).
- **Counters**: `GET /v1/metrics` returns process-wide counters
  (requests, errors, runs submitted, registrations, …) plus
  `uptime_seconds`. It requires the `metrics:read` permission — create a
  monitoring account with a role granting exactly that, so your prober
  can read numbers and nothing else:
  the bootstrap admin creates the user, adds a `monitoring` role with
  `metrics:read`, and grants it.
- **What to alert on**: `/v1/health` failing (page someone), `errors`
  growing faster than `requests` (investigate), `uptime_seconds`
  resetting outside a deploy window (the process is crash-looping).

## Releases

Pushing a `v*` tag runs `build-installers.yml`: every platform builds
the shell binary, smoke-boots it against `/v1/health`, and the `release`
job publishes a GitHub Release with all three artifacts and generated
notes. `desktop-v*` tags build the Windows Tauri installer
(`desktop-windows.yml`) — signing certificates are configured there when
you have them. Ship order: tag → CI green → update the server (`git
pull` + restart under systemd) → spot-check `/v1/health` and one chat
turn.

Keep a **staging host** (a second small VM or a second port on the same
VM with its own `--data` directory) and land upgrades there first; the
restore drill above doubles as the staging refresh.

## Load testing

```bash
python scripts/load_test.py --base https://staging.oolu.example \
    --username admin --password ... --workers 8 --requests 200
```

Reports req/s and p50/p95 latency for the submit-and-read run pipeline.
Run it against **staging** before opening registration, and again before
anything you expect to spike. Only ever aim it at hosts you own.

## Retention

- **Execution logs**: per-node daily logs follow each tenant's
  `account.log_retention_days` setting (default 180, floor 7) and are
  pruned as new logs are written.
- **Assistant threads**: capped at 500 turns per account, oldest off the
  back, automatically.
- **Durable maintenance**: `oolu.durable.maintenance.prune_retention`
  clears terminal tasks and sent outbox rows past a cutoff; the audit
  log is append-only and tamper-evident by design — it is the record
  the law wants kept, so nothing prunes it.
- **Backups** age out on your schedule (the 7-daily/4-weekly rotation
  above is a sane floor).

## Legal documents

Three public, stable URLs every client can rely on:

- `GET /v1/legal/terms` and `GET /v1/legal/privacy` serve
  `<data_dir>/legal/terms.md` and `privacy.md` **verbatim** when those
  files exist. Until then, built-in templates answer — each headed by an
  unmissable "TEMPLATE — NOT LEGAL ADVICE" notice. Have counsel review
  your real text and drop the files in place; no restart needed.
- `GET /v1/legal/node-policy` serves the code-owned Node Policy (the
  hygiene machinery enforces exactly this text, versioned).

## The data-subject's rights

Self-serve, no ticket queue:

- **Export**: `GET /v1/account/export` (Settings → Privacy & data →
  "Download my data") returns the account, identity links, settings,
  the OoLu thread, friend messages, Life-drawer files, runs, model
  usage, earnings, and payment metadata as one JSON document.
- **Erasure**: `POST /v1/account/delete` (password confirmation
  required) erases messages (both sides — the store keeps one shared
  copy), the assistant thread, identity links, verification records,
  and card metadata, disables the account forever (the username is
  never reissued), and appends an `account.erased` audit record. The
  response lists exactly what was and was not removed. Append-only
  records the service must keep (audit chain, financial ledgers) are
  retained.

## Incidents

1. **Confirm** with `/v1/health` from outside the box, then from on it
   (`curl 127.0.0.1:8788/v1/health`) — that split tells you network vs
   process in one move.
2. **Look** at the service log (`journalctl -u oolu -n 200`) and
   `/v1/metrics` if reachable. The gateway turns handler errors into
   4xx/5xx JSON with codes — grep the code, not the prose.
3. **Restart** is safe: runs are durable; the worker resumes from the
   queue. `systemctl restart oolu`.
4. **Database trouble**: stop the service before touching SQLite files;
   restore from the newest backup folder into a fresh `--data` and swap
   directories rather than editing in place.
5. **Write it down**: one paragraph per incident (what broke, how you
   knew, what fixed it) appended to your ops journal — the next 3 a.m.
   is easier when the last one left notes.

## Secrets inventory

`OOLU_HOST_SECRET` (token signing — losing it signs everyone out),
`OOLU_ADMIN_PASSWORD`, `OOLU_DATABASE_URL`, `OOLU_MAIL_KEY`,
`OOLU_STRIPE_KEY` + `OOLU_STRIPE_WEBHOOK_SECRET`,
`OOLU_PLATFORM_ANTHROPIC_KEY` / `OOLU_PLATFORM_OPENAI_KEY`, and the
on-disk `machine.key`. All belong in your secret manager; only
`machine.key` also belongs in backups.
