# Changelog

All notable changes to Workflow-GPS are documented here.

## Unreleased

The web through the model's own hands, and the desktop's own disk:

- **Model web search.** The Anthropic adapter can now carry the
  provider's server-side web-search tool (`web_search_20250305`, max 3
  uses per turn): the search runs INSIDE the API call on Anthropic's
  servers, so any keyed OoLu — an own key on Edge, or the Global
  subscription brain — answers current-facts questions from any
  install, with no web access needed on the machine itself. The new
  `model.web_search` setting (default on) closes the door; a local
  model never searches (local means local), and a keyless install stays
  deterministic by design — that part was a feature, not a bug.
- **The desktop finds its own files.** The chat's new
  `find_local_files` tool searches the user's computer by name or glob
  — home-rooted, listing only (path + size, never content), bounded
  (hidden and bulky tool directories skipped, scan capped, 40 matches
  max). ONLY `oolu desktop` wires it; `oolu host` never does — a server
  has no business in anyone's home directory, and the tool says so in
  words when asked there.
- Tests: the web-search tool riding the Anthropic request (and the
  setting removing it), the catalog knob, bounded home-rooted disk
  search with hidden-directory privacy, the host wall, and the chat
  tool answering on desktop / refusing on hosts. 180 vitest and the
  entire backend suite green.

The device's senses on demand, and reminders that point back:

- **Microphone, camera, location — asked for exactly when needed.** A
  new ＋ button on the chat composer opens the device door: "Share my
  location" reads the device's position (the browser/app permission
  prompt appears at that tap, never at startup) and sends it into the
  conversation; "Take a photo" opens the native camera on a phone or
  tablet (file picker on a computer), downscales the shot to fit the
  drawer, saves it to Files (folder: camera), and tells OoLu. A refused
  permission lands as honest words in the thread. The microphone was
  already live (hold Send to talk). Images in Files now display as
  pictures, read-only, instead of opening in the text editor.
- **The reminder's arrow.** When the idle reminder lists ongoing or
  snagged tasks, each task now carries an arrow (↦ task name) pointing
  straight back to its ACTION window: the click scrolls to the task's
  run card if it is in the thread — flashing it — or brings the card
  into the conversation, Retry buttons and all.
- Tests: location success/refusal/absence, shot naming, the composer
  device menu sharing coordinates and surfacing refusals, and the
  reminder arrow summoning the live run card. 180 vitest and the entire
  backend suite green; shell rebuilt.

The interact window becomes what it is — a conversation:

- **Nothing but the thread and the composer.** The interact tab's
  button row, task chips, and the "Automation reliability…" banner are
  gone; the conversation now takes every pixel the tab has (the thread
  stretches to fill). One hint line inside the EMPTY thread teaches the
  typed commands — “pending”, “sign <task id> as <your name>”, “reply”,
  “build” — and disappears with the first message. All commands still
  answer deterministically.
- **The stewardship blocks step aside while you talk.** With the
  Interact tab open, the KYC block, the member-node fleet, and the
  Pending desk fold away (they live on the other tabs as before), so
  the conversation window is large and clean. The reliability line
  moved to the Activity tab, where telemetry belongs.
- Tests: the clean-window assertions (no buttons, no banner, hint
  present), typed commands still driving the desk with task ids in the
  listing. 173 vitest and the entire backend suite green; shell rebuilt.

The retry that wouldn't press, the button acceleration never needed,
and a desk that hands you the task id:

- **Retry presses now.** The run card's 2.5-second poll rebuilt the DOM
  under the user's finger on every tick (a new task object every poll),
  so a click could land on a button that no longer existed — and a
  refused decision vanished into an unhandled rejection. The poll now
  re-renders only on REAL change, the decision buttons disable and
  relabel while the call is out ("Retrying…"), a refusal lands in the
  card as words, and the incident card counts the retries ("2 retries so
  far — the next retry lets OoLu plan and rebuild the path").
- **Acceleration is automatic, not a button.** Whatever can move on a
  node's path already moved; the interact window now surfaces exactly
  the work that waits on a human, by itself: each waiting task appears
  as a clickable chip (name + task id) the moment the window opens.
  Typing "accelerate" still answers honestly.
- **Pending · Sign · Build, one row.** The interact quick actions are
  now three: "Pending" lists what waits (each line carries the task id),
  "Sign" pre-fills `sign <task id> as ` — the id auto-appends when
  exactly one task waits, or comes from tapping a task chip / the
  pending list — and signing passes the task to the next node; "Build"
  pre-fills `build `. The thread's held-request heading is "Pending".
  The assistant's pending reply teaches the same commands.
- Tests: the incident Retry's press feedback, decision post, retry
  count, and surfaced refusal; the one-row quick actions; Sign's id
  append (single task) and open-endedness (several); the task chips'
  click-to-fill. 174 vitest and the entire backend suite green; shell
  rebuilt.

The settings that lied, and the forward menu that wouldn't behave:

- **The theme actually changes.** The whole stylesheet now reads
  variables (the status chips included) and carries a complete light
  palette: choosing "light"/"dark" pins `data-theme` on the root,
  "system" removes the pin so the OS preference decides, and the choice
  is cached so the right look paints before settings load. Saving the
  setting applies it the same instant.
- **Languages by their formal names, and a UI that follows.** The
  language dropdown shows English / 中文（简体） / Español / Français —
  never raw codes (theme values get words too; stored values stay
  stable codes). A new chrome dictionary (`ui.ts`) translates the
  navigation, labels, placeholders, and buttons live when
  `app.language` changes — Life/Work tabs, the conversation list, the
  chat composer, Settings headings, the forward menu. The assistant's
  own words follow the model, not this table; per-setting labels still
  come from the settings node.
- **The forward menu behaves like a menu.** A click anywhere else — or
  Escape — closes it (it used to stay open until cancelled), and a
  search box narrows long friend/node lists as you type, with the
  save-to-file escape hatch always in reach.
- Tests: theme pin/unpin + persistence, the language dictionary and
  its change notifications, formal choice labels, the Settings
  instant-apply and live chrome switch, and the forward menu's search /
  outside-click / Escape behaviors. 170 vitest and the entire backend
  suite green; shell rebuilt.

Phase 4 of going public — ship and operate: the data-subject's rights,
the legal surface, backups, the operator's numbers, and releases:

- **Export and erasure, self-serve.** `GET /v1/account/export` returns
  everything the host holds about the caller as one JSON document —
  account, identity links, settings, the OoLu thread, friend messages,
  Life-drawer files, runs, model usage, earnings, payment metadata.
  `POST /v1/account/delete` demands the password (a stolen session must
  not destroy an account), erases the per-person stores (messages both
  sides — the store keeps one shared copy — the assistant thread,
  identity links, verification records, card metadata with provider-
  side detach), disables the account forever (the username is never
  reissued — a freed name would let a stranger inherit its trust),
  appends an `account.erased` audit record, and answers with exactly
  what was and was not removed. Settings grows a "Privacy & data"
  section: Download my data, Delete my account (password-confirmed),
  and the legal links.
- **The legal surface.** Three public, stable URLs: `/v1/legal/terms`
  and `/v1/legal/privacy` serve the operator's `<data_dir>/legal/*.md`
  verbatim when present, and until then built-in templates headed by an
  unmissable "TEMPLATE — NOT LEGAL ADVICE" notice; `/v1/legal/
  node-policy` serves the code-owned, hygiene-enforced Node Policy.
- **`oolu backup`.** One command, one timestamped folder with everything
  a restore needs: every SQLite database through the ONLINE backup API
  (safe against a live server mid-write) plus the keyring's
  `machine.key` — without which every stored model key is unreadable.
  Says when the durable store is PostgreSQL and pg_dump owns that half.
- **The operator's numbers.** `/v1/metrics` is now permission-gated
  (`metrics:read` — grant a monitoring role that can read nothing else)
  and carries `uptime_seconds`, so a prober can spot crash-loops.
- **Releases and the runbook.** A pushed `v*` tag now publishes a
  GitHub Release carrying every platform's smoke-tested shell binary
  (new `release` job in build-installers.yml). `docs/operations.md` is
  the ops runbook: backup schedule + restore drill, monitoring and what
  to alert on, ship order, staging, retention, the legal files, the
  rights routes, and the 3 a.m. incident list. `scripts/load_test.py`
  measures the run pipeline (req/s, p50/p95) against a host you own.
- Tests: export completeness, password-gated erasure with store-level
  verification and the audit record, template-vs-operator legal
  documents, the metrics permission wall with uptime, and live-database
  backup round-trips in `test_account_privacy.py`; the Settings privacy
  flows (download, delete with wrong-password refusal) in vitest. 159
  vitest and the entire backend suite green; shell rebuilt.

Phase 3 of going public: people talking to people, one conversation
across devices, and a first minute that lands:

- **Friends for real.** Person-to-person messages between accounts on
  the same host, in a new durable store (`DirectMessageStore`): ordered
  threads, read state (opening a thread reads it), unread counts on the
  peer list. Discovery is EXACT — `POST /v1/friends/lookup` resolves a
  full username or e-mail (through the identity links) and nothing else;
  there is no directory to browse, so on a public host nobody is
  findable unless they shared their name. The peer must be a real,
  enabled account in the caller's own tenant. The Life screen's Friends
  group goes live: conversations with unread badges, a start-a-
  conversation pane, a per-person thread with the same composer as the
  OoLu chat — and the forward menu now offers friends as destinations
  (a real server delivery, marked with where it came from, never a
  local-storage append). Hosts without a server keep the honest
  placeholder.
- **One conversation across devices.** The OoLu thread now lives server-
  side per account (`AssistantHistoryStore`, capped at 500 turns like a
  messenger): `/v1/chat` records each user turn, assistant reply, and
  run marker, and `GET /v1/chat/history` is what a fresh device loads —
  the desktop, the browser, and the phone show the SAME thread. The
  local cache stays as the offline story and hosts that keep no history
  (404) keep working exactly as before. Idle-reminder bubbles remain
  client-side by design — presence, not conversation. The node-interact
  window stays its own context and is not recorded into the main thread.
- **A first minute that lands.** A one-time first-run guide inside the
  chat's welcome state: say hi (one tap), try a first task (drops a
  ready-to-send task into the box — nothing fires unseen), and where to
  add a model key. Used once or dismissed, it never returns.
- Tests: the store and every wall in `test_friends.py` (order + read
  state, tenant scoping, exact-lookup-only discovery, disabled accounts
  stop receiving, 404 on storeless hosts, chat turns landing per
  account, the messenger cap), plus the Life friends list/thread/start
  flows, Chat's server-history sync and cache fallback, the first-run
  guide's once-only walk, and friend forwarding in vitest. 156 vitest
  and the entire backend suite green; shell rebuilt.

Phase 2 of going public: the subscription brain becomes real, the money
stack wakes up behind honest walls, and KYC reviews get an inbox:

- **The hosted subscription brain.** `model.source="subscription"` now
  has something behind it: the host operator sets
  `OOLU_PLATFORM_ANTHROPIC_KEY` / `OOLU_PLATFORM_OPENAI_KEY` and tenants
  on that source are answered through the PLATFORM's keys (Claude first,
  the plan's order) — no pasted key needed. Every consultation lands in
  durable per-tenant monthly books (`ModelUsageStore`), and the plan's
  allowance gates it: free includes none (the refusal names the paid
  plans, own keys, and local models as the ways out), plus/pro/
  enterprise include $5/$20/$100 a month, and a spent allowance says
  when it renews. Platform keys follow the environment on every boot
  (set → stored encrypted under a reserved keyring tenant, unset →
  removed). New `GET /v1/usage/model` shows a tenant their books and
  remaining allowance. Hosts without platform keys keep the honest
  "isn't live yet" message.
- **The money stack is wired.** `build_host_runtime` now constructs the
  earnings ledger, payout store, dispute service, and payment adapters
  it previously left dormant — so `/v1/earnings`, `/v1/payout-accounts`,
  and `/v1/disputes/{event}` answer on every host. With `OOLU_STRIPE_KEY`
  the card vault and payout adapter are the real Stripe ones (card
  numbers never transit our servers — SetupIntent only); without it the
  test doubles stay. New `POST /v1/webhooks/stripe` verifies Stripe's
  `Stripe-Signature` over the exact raw payload and matches events back
  to our books through the `oolu_event_id`/`oolu_batch_id` metadata the
  adapters now attach to charges and transfers — refunds and disputes
  claw back the right event, payout confirmations settle the right
  batch, and replays are idempotent by event id.
- **The transaction port has a key, and it refuses test doubles.**
  `oolu host --transactions` opens the launch guard's operator gate —
  and refuses to start without `OOLU_STRIPE_KEY`, so the port never
  opens onto fakes. Even open, each class of work still charges only
  after its prices settle and its function has verified successes, and
  `require_production_money` still demands PostgreSQL + production
  identity. The subscription console's `charging_open` now tells this
  truth instead of a hard-coded `false`.
- **The KYC reviewer inbox.** Reviewers (the `kyc:review` permission —
  the bootstrap admin's `*` covers it) get `GET /v1/kyc/reviews`:
  pending applications, fast-tracked first, oldest first. The Work
  screen shows the queue with Approve/Reject right on the row (the
  existing decide route, authority-checked and audited); a verdict
  clears the row. Everyone else gets a 403 and sees no inbox at all.
- Tests: the brain's whole ladder in `test_subscription_brain.py`
  (platform key answers, free-plan refusal, spent-allowance renewal
  message, Claude-first fallback, own-api isolation, monthly book
  rollover, the usage surface), the money half in `test_stripe_money.py`
  (Stripe-Signature round trip and refusals, webhook→books matching,
  idempotent replays, adapter wire shapes, assembly's test-vs-live
  choice, the `--transactions` wall, and one full charge → accrue →
  settle → confirm → claw-back cycle on the fake processor), and the
  inbox in `test_kyc_inbox.py` + `Work.test.tsx`. 150 vitest and the
  entire backend suite green; shell rebuilt; the going-online runbook
  documents the new environment variables and flags.

Phase 1 of going public: proven e-mail addresses, a way back in, and
walls a public host cannot serve without:

- **E-mail verification on registration.** A host with a mail sender
  configured no longer hands out a session at `POST /v1/auth/register`:
  the account is created, a 6-digit code is mailed (`MailCodeStore` —
  hashed at rest, 30-minute expiry, 5 attempts, strictly single-use),
  and the answer is `{"verification_required": true}` with no token.
  `POST /v1/auth/verify` takes e-mail + code + password and mints the
  first session — the code alone is never a session, so a leaked inbox
  is not a leaked account. Sign-in answers 403 `verification_required`
  for registered-but-unproven addresses (bootstrap/operator accounts,
  which never registered an e-mail, are exempt). The sign-in screen
  grows the matching code-entry step, and `/v1/client-config` advertises
  `verification` so clients know the step is coming.
- **Password reset.** `POST /v1/auth/reset/request` always answers 202
  ("sent") whether or not the address exists — nothing enumerates
  accounts — and mails a reset code to real ones. `POST
  /v1/auth/reset/confirm` (e-mail + code + new password) changes the
  password and counts as address verification, since inbox control was
  just proven. The sign-in screen gets the matching "Forgot password?"
  flow.
- **The outbound door.** `oolu.mail`: `HttpMailSender` speaks the
  Resend-style JSON API (`OOLU_MAIL_URL` + `OOLU_MAIL_KEY` +
  `OOLU_MAIL_FROM`), `OOLU_MAIL=console` logs mail for development, and
  an unconfigured host keeps the old immediate-token registration (for
  private/testing installs that opted in knowingly).
- **`--global-service` walls.** A public host refuses to start with
  `--open-registration` and no mail sender (strangers must prove their
  address), and never wires the script hand unless the backend is real
  isolation (docker) — synthesized code does not run unsandboxed on a
  public host (`require_isolation` in `build_host_runtime`).
- **An honest "subscription" dead-end message.** With `model.source`
  still "subscription" and no keys, the router now says the hosted OoLu
  brain isn't live yet and points at own-api keys or a local model,
  instead of the generic "no model key is configured".
- Tests: the whole flow in `test_mail_verification.py` (verification-
  first registration, 403-before-verify, wrong/burned/expired codes,
  no-enumeration reset, reset-counts-as-verification, the code store's
  clock/attempt behaviors, the Resend wire shape, both public-host
  walls) plus the Login code-step and forgot-password flows and the new
  api adapters in vitest. 148 vitest and the entire backend suite
  green; shell rebuilt.

The BYO key actually takes over, and OoLu talks like it means it:

- **An added model key becomes THE model — and proves it.** The root of
  "I set my OpenAI key, it's billed, but nothing works": the default
  `model.source` is "subscription" (the OoLu plan's hosted brain, which
  a self-hosted/desktop install does not have), so a key added while
  still on that default was only ever a silent fallback behind a
  provider that will never answer. Now `POST /v1/keys/model` flips
  `model.source` subscription→own-api and points `model.provider` at the
  key just added (a deliberate "local" choice is left alone), so the key
  the user pasted is the model the user gets. New `POST
  /v1/keys/model/test` makes one real call through the live router and
  reports the model that answered — or the exact reason it could not —
  turning "billed but is it working?" into a definitive yes/no; the
  Settings "Add" now auto-tests and a "Test connection" button re-checks
  any time.
- **Energetic, mood-aware voice and tone.** OoLu's persona is rewritten
  upbeat and lively (the system prompt, the greetings, the acks, the
  presence lines), and it now speaks in its current MOOD: the chat turn
  carries the avatar's mood so the model's words match its face
  (`mood_directive`), and speech synthesis varies rate and pitch by mood
  (`toneForMood` — brighter and quicker when excited, steady when
  worried; the default is livelier than the old flat 1.05). The system
  prompt is also more conservative about turning chat into work — when
  in doubt it TALKS and offers, instead of silently kicking off a task
  that fails on a fresh machine.
- Tests: the source-switch + `/keys/model/test` pass/fail
  (`test_gateway_model_keys.py`), the Settings add-then-auto-test and
  Test-connection button (`SettingsPane.test.tsx`), the mood-driven
  speech tone (`voice.test.ts`), and updated Chat presence lines.
  Verified live through `build_host_runtime`: add key → source flips to
  own-api → test route answers → a chat turn uses the model with mood
  threaded. 141 vitest and the entire backend suite green; shell
  rebuilt.

Forwarding without friction, real hands on the local device, and the
creative-app lesson learned from the source file:

- **Forward messages and files anywhere.** Every chat bubble (the OoLu
  conversation and a node's interact window) carries a hover ↪: pick a
  destination — OoLu, any node on your desk, or "New file in Files" —
  and the message lands in that thread's history marked "↪ forwarded
  from <who>" (or becomes a document under the Life drawer's
  `forwarded/` folder). Files forward too: FileView's "forward" copies
  the file into the picked drawer's `forwarded/` folder — a COPY, so
  originals never move. `forward.ts` owns the logic; `ForwardMenu` is
  the picker.
- **The execution-access review, answered honestly, then fixed.** The
  desktop wired ONLY the GET-only HTTP hand: OoLu could not command the
  local device's CLI at all (the CLI executor existed but nothing
  passed it in); scripts ran only through the script-node path added by
  the retry work. Now `wfgps desktop` gives the engine
  `build_desktop_hands`: HTTP + the LOCAL DEVICE's command line — the
  discovered tools (ffmpeg, pandoc, …), workspace-confined under the
  data directory, on by default (commanding this machine is what the
  desktop engine is for), `OOLU_CLI_TOOLS=off` to disable and
  `OOLU_CLI_ALLOWLIST` to widen.
- **Creative apps: the source file is the lesson.** New
  `skills/creative.py`: a registry of creative applications (Photoshop,
  Illustrator, GIMP, SolidWorks, Fusion, AutoCAD, Blender, Figma,
  Premiere, After Effects) with their source extensions;
  `plan_creative_capture` sorts a session's artifacts with SOURCE FILES
  FIRST (.psd/.sldprt/.blend — the model-training payload) and the
  screenshot/mouse/keyboard trace as ADVISORY path context
  (`replayable` is a constant False — no flag can promote a pixel trace
  into execution). The learner refuses to compile a creative-app
  demonstration into a replayable skill (`creative_source_needed`, with
  the reason in words: the trace explains the user's path but "will
  never execute the work reliably"); ordinary applications learn
  exactly as before.
- Tests: `forward.test.ts` (marked thread insertion, message→file,
  file copy with drawer/folder, target list) and
  `tests/test_creative_learning.py` (app recognition, capture
  priority, the learner's refusal, desktop hands incl. CLI + the off
  switch). 139 vitest and the entire backend suite green; shell
  rebuilt.

A node IS its function; the record is a file; the feed reads like words:

- **No more empty nodes.** Building a node through OoLu now takes two
  verified gates in ONE model consultation (`author_node_function` +
  `NODE_FUNCTION_PROMPT`): first the sentence must be judged EXECUTABLE
  WORK — a greeting, a question, or conversation answers `NO_TASK` and
  nothing is created (`obviously_chat` refuses the obvious cases before
  any model) — then the model must actually WRITE the node's execution
  function. The published node carries it as its own script action
  (`adapter="script"`, the verified-before-trusted runtime from the
  retry work), never a placeholder draft; no model, no code, no node —
  "an empty node is unnecessary." Contract runs now receive the same
  executor set as the orchestrator (script hand included), so a node's
  own function executes and routes locally instead of falling back to
  the global machinery. The Work UI's manual create form is unchanged —
  a human's deliberate draft stays a human's choice.
- **Daily execution logs, kept as files for legal use.** Every activity
  fetch materializes the node's daily log in its own Files drawer
  (`logs/execution-YYYY-MM-DD.log`): full fidelity — ISO timestamps,
  run ids, executing node, raw event types — merged idempotently so
  nothing duplicates, and pruned after the new
  `account.log_retention_days` setting (default 180 days, 7–3650; set
  it to your legal record-keeping requirement).
- **The Supernode's feed reads like words.** A Supernode's activity now
  aggregates its members' executions, and every item names the node
  that EXECUTED it. The display simplifies for humans: the executing
  node's NAME instead of a run id, the clock down to the second
  (10:00:02, not an ISO blob), and plan/status words instead of
  function calls ("Carried out the actions", never
  `workflow.executed`) — with the full detail one tooltip away and in
  the log files.
- Tests: the four creation gates + the function riding the published
  version (`test_node_interact.py`), log materialization/idempotence/
  retention pruning and the member-named Supernode feed
  (`test_execution_logs.py`), and the humanized feed
  (`Work.test.tsx`). 135 vitest and the entire backend suite green;
  shell rebuilt.

The node's interaction window — OoLu called out to act ON a node:

- **An Interact tab beside Activity and Files** on every Work node
  thread: a node-scoped conversation with OoLu (`POST /v1/chat` +
  `node_id`, tenant-guarded to the caller's desk). Quick actions:
  Pending requests, Accelerate, Sign all…, Build a node…
- **OoLu's hands on the node's desk** (`NodeChatTools`): list its held
  requests, allow/reject them, SIGN them — single or "sign all as
  <name>", the fast manual floor of final-result audit signing — and
  reply to requesters. Every hand goes through the gateway's own
  handlers, so tenant scope, approve authority (a submitter still can't
  approve their own ask), the budget re-check, and the audit trail
  apply unchanged. Deterministic commands work with no model
  (pending / accelerate / allow / reject / sign … as … / reply …: … /
  build …); a configured model gets the same tools plus a node-context
  system note and is told never to decide a hold unasked.
- **Building on the node's path**: "build <goal>" (consent-gated on
  'Auto-build nodes on my paths') contributes a keyword-named draft
  node to the registry — a citizen the planner can find and route to,
  becoming callable as its runs verify — created UNDER the node when
  it is a Supernode (unclaimed: the node id is the claim ticket) or
  standalone on the caller's desk otherwise.
- **The automation vision, made visible and honest**: the Interact tab
  leads with the node's automation reliability ("99.2% over 133
  verified runs — every verified run takes this node closer to
  hands-off"), computed from platform-verified health. And when a
  node's automation FAILS, the failure now carries a stable error
  code — `EXEC_NODE_FAILED`, `EXEC_BLOCKED`, `PLAN_NO_ROUTE` — shown
  as a chip on the run view and spoken by chat ("saved with the run so
  you can fix it later").
- Tests: `tests/test_node_interact.py` (pending/accelerate listing,
  sign-all landing the typed signature in the audit trail and emptying
  the queue, the authority wall answering in words, replies, consent-
  gated building standalone and under a Supernode, 404 off the desk)
  and `NodeInteract.test.tsx` (reliability line, node-scoped turns +
  action chips, quick actions send vs pre-fill). Error-code asserts in
  `test_execution_retry.py`. 134 vitest and the entire backend suite
  green; shell rebuilt.

Folders in the drawers, KYC only where it binds, and lists that fold:

- **Folders organize a file drawer.** `UserFile` gains a `folder` path
  ('/'-separated, normalized, bounded; '' = root) — folders are derived
  from the files that name them, organization rather than a separate
  object. The gateway accepts `folder` on create and update (moving a
  file is just updating its folder), and every drawer — a node's files
  in Work (Supernodes included) and the Life drawer — navigates them:
  folder tiles, a breadcrumb with "up one level", "New folder" (held
  client-side until a document lands in it), and "New document" creating
  in the current folder.
- **KYC binds only on the Global service.** New
  `GatewayConfig.global_service` (set by `oolu host --global-service`;
  the desktop and private-network hosts never set it): a Supernode
  created under a GLOBAL account serves the whole ecosystem with a
  higher trust score, so the KYC policy and its paying-plan gate are
  enforced there — and only there. On an Edge install the KYC status
  answers `required: false`, the Work UI shows **no KYC block at all**
  (no form, no subscription nag), and applying is refused as
  unnecessary (409, never a 402 plan nag). And once a review IS done,
  the block disappears everywhere: a verified Supernode shows one quiet
  "✓ KYC verified · global trust ×N" badge instead of the section.
- **Lists fold for a clear view.** The Life sidebar's Friends and Noder
  groups and a Supernode's Member nodes section are now collapsible
  headers (▾/▸ with a count); Life's choices survive restarts via
  localStorage, and everything defaults open.
- Tests: folder round-trip/move/refusal through the gateway
  (`test_user_files.py`), Edge-vs-Global KYC (`test_supernode_kyc.py`:
  `required` flag, 409 apply, nothing stored), FilesPane folder
  navigation + empty-folder creation, the hidden-on-Edge KYC block, the
  verified badge, and folding Member nodes / Friends / Noder (persisted).
  131 vitest and the entire backend suite green; verified live through
  `build_host_runtime` (folder create/move; Edge default). Shell
  rebuilt.

The endless conversation keeps its promises, and names become labels:

- **The chat reminds an idle user of unfinished work.** A conversation
  with OoLu never ends, so open work must not rely on scrolling back:
  once the user has been idle for two minutes, a dashed "reminder"
  bubble lists what is still WAITING ON THEM (needs an answer / a
  decision / an approval / hit a snag) and what is still working —
  capped at three each with an "and N more", repeated at most every
  five minutes, and reset the moment the user speaks. Reminders are the
  chat's own words: they never enter the history sent to the model.
  Logic lives in pure `reminders.ts` (`reminderDue`/`reminderText`),
  wired into `Chat` on a 30-second check.
- **Names are labels, not transcripts.** New keyword-naming helpers —
  frontend `naming.ts` (`conciseName`) and backend `oolu/naming.py`
  (`concise_name`/`keyword_slug`) — distill a task sentence into its
  first four distinct non-stopword keywords ("convert the quarterly
  report to pdf and email it" → "Convert Quarterly Report Pdf"), with
  the trimmed original as the all-stopwords fallback. Applied wherever
  the system names things itself, explicit names always honored:
  the Life Noder list and thread header (full request kept as tooltip
  and quoted line), the chat run card, `wfgps record` without `--name`
  (the learned skill's name — and therefore its `learned.…` id — is now
  keywords; the full intent stays as the description), the gateway's
  listing-title fallback for contributions without a title, and the
  desk title fallback (a bare `learned.…` skill id now reads "Convert
  Quarterly Report Pdf", never a dotted sentence).
- Tests: `reminders.test.ts` (idle window, five-minute cadence,
  activity reset, capped concise listing), a fake-timers Chat
  integration test (the bubble appears once and does not repeat inside
  the window), `naming.test.ts` + `tests/test_naming.py` (keyword
  order, dedup, stopword fallback, learned-id derivation, desk-title
  condensation). 126 vitest and the entire backend suite green; shell
  rebuilt.

The messenger straightened out — the list, the Edge doors, who answers
for a node, and money in the user's own currency:

- **Independent scrolls, Settings under Files.** The app frame is pinned
  to the viewport (`height: 100vh`; the page itself never scrolls): the
  conversation list and the open pane each own their overflow, so
  scrolling one never moves the other. The Settings entry moved from the
  pinned bottom of the sidebar to directly below Files — a long Friends
  or Noder list can no longer hide it below the fold (`convo-bottom` is
  gone).
- **Edge is two doors: this device, or a private network.** The sign-in
  screen's Edge tab now offers "This device" (the old passthrough to the
  loopback engine) and "Private network" — a private server a group runs
  on its own network (a static address), entered once and remembered
  separately from the Global server (`oolu_edge_server`). The private
  network still uses real accounts: the same username/password sign-in
  and registration form, pointed at the private host (`oolu host
  --open-registration`), because onboarding a node created under a
  Supernode has to name an actual person.
- **A node created under a Supernode starts with NO responsible.**
  `create_account` leaves `responsible` empty for non-Supernode children
  (the regime stays fixed as before; a Supernode itself always keeps its
  creator — humans in full control cannot mean nobody). Onboarding is
  the claim: the first user account that presents the node id becomes
  the responsible, shown on the node thread as their user ID; after
  that, takeovers are refused as before. The Work UI shows "not
  onboarded yet" instead of an empty responsible, and warns — on the
  thread, in the member list, and in the create-under-Supernode form —
  not to show the node id publicly before onboarding, because the id is
  the claim ticket.
- **Caps in the user's regional legal currency.** New `oolu.currency`
  module: a closed catalog of 18 currencies with symbols, decimals, and
  FIXED reference rates (a cap is a safety rail, not an FX position;
  unknown codes read as USD, which errs toward stopping earlier). New
  `account.currency` setting (choice, default USD); all money settings
  (`budget.model_cap`, `hard_cap`, `review_threshold`, `monthly_limit`)
  carry `unit="currency"`, resolved by `describe()` to the tenant's
  code and shown next to the input; bounds widened to give high-rate
  currencies (JPY, KRW, MWK) headroom. `ChatModelRouter` converts the
  cap into the meter's USD unit at the comparison and speaks the
  budget-exceeded message in the user's currency. The Settings pane
  suggests the region's currency from the browser locale ("Your region
  suggests MWK — Use MWK"), one click, never automatic.
- Tests: `test_currency.py` (conversion round-trips, unknown-code
  safety, unit stamping/resolution, the router refusing in yen), the
  unclaimed→claim desk flow in `test_work_desk.py`, private-network
  sign-in + no-address refusal in `Login.test.tsx`, unclaimed/onboarded
  node threads in `Work.test.tsx`, and the Settings-below-Files order in
  `Life.test.tsx`. Verified live through `build_host_runtime`: currency
  switch re-labels every money field and refuses bogus codes. Shell
  rebuilt.

Execution retry, diagnosed and escalated — when a run breaks, the user
sees the plan, the exact broken node, and after two retries the model is
called out to plan and write the code:

- **The exact failing node is labelled everywhere.** `ExecutionRecord`
  gains `failed_action_id`/`failed_action_label` (the FIRST action that
  failed — cascade-cancelled dependents are consequences, not causes),
  set by both runners (`DagRouteRunner` incl. capability-blocked
  preflight, `ActionExecutorRouteRunner`). The monitor's summary — and
  therefore the incident, the pause payload, the abort's terminal
  `failure_reason`, and the audit events — all name the node.
- **The plan is visible.** `GET /v1/runs/{id}` now carries `plan` (the
  chosen route as ordered steps with live per-node statuses, the culprit
  marked), `failure` (node, error, attempt, retry count), `no_route`
  (when planning failed before a viable route existed: the reason,
  unresolved grounding terms, and every excluded candidate with its
  reason), `autobuild` (the consent check, below) and `user_retries`.
  Timeline frames and `/audit` entries gain a human-readable `detail`
  line. The Task pane renders all of it: the step list with per-node
  glyphs, a "failed here" tag, the no-route explanation, and a retry
  button that counts down to the AI rebuild.
- **Retry twice, then the model plans and writes the code.**
  `RunState.user_retries` counts the operator's incident retries; after
  two of them fail, `_phase_recovery` calls the new `RouteRebuilder`
  seam instead of raising a third identical incident.
  `LLMRouteRebuilder` (metered under `plan.rebuild`) asks the tenant's
  model for a numbered plan plus one script, builds an honest route
  (`origin="llm_rebuild"`, the plan in `plan_notes`, `risk="write"` so
  model-written code re-earns the human's confirmation), and the run
  re-enters HUMAN_CONTROL. One rebuild per run (`rebuild_attempts`);
  every failure mode is a refusal carried on the incident
  (`rebuild_refusal`), never a crash. `NodeScriptRunner` accepts a
  planner-`provided` script as a proposal — executed and classified
  before it is trusted or cached, with bounded missing-dependency
  healing — and `ChatModelSynthesizer` gives the repair ladder a
  lightweight single-shot synthesizer.
- **Auto-build now checks on EXECUTION failure, not just planning.**
  Previously `account.autobuild_consent` was consulted only on the
  chat's planning-time `cannot_execute` refusal; a run that failed while
  executing never mentioned it. The consent check now gates the rebuild
  itself, the run view carries the hint on every failed/incident run,
  and the chat surface folds an execution failure (failing node +
  hint/refusal) into its reply. `build_host_runtime` wires the
  rebuilder plus a script hand (`build_script_executor`: the configured
  isolation backend, node script cache at `scripts.db`) into every host.
- Tests: `tests/test_execution_retry.py` (labelling in both runners +
  blocked + abort, the two-retries→rebuild flow incl. confirmation,
  consent/no-code/no-runtime/exploding-rebuilder refusals, the
  one-rebuild cap, provided-script verify-then-cache, the gateway
  views) and `TaskPane.test.tsx` (plan steps + culprit, retry
  countdown, autobuild hint, AI-rebuild badge, no-route panel).
  Verified live end-to-end through `build_host_runtime`: submit → node
  labelled → two retries → consent-off refusal → settings flip → model
  consulted → abort keeps the diagnosis; and a provided script executed
  for real through `SubprocessBackend` (`emit_result` → payload).

Value patching — the mechanical-design scenario: deterministic
scaffolding (open the app, open the file, select the tool) chains by
slots, and at the creative step the run pulls the node's declared input
list and lets a smart plugin fill the values:

- **`ValueInput` on `NodeContract.inputs`**: a node declares its creative
  values — name, description, type (`number` / `string` / `choice`), an
  honest default, hard `minimum`/`maximum` bounds or a closed choice set —
  instead of hardcoding them. Actions reference them with two placeholder
  forms inside parameters: `{"$input": name}` (whole-value) and
  `{"$template": "...{hole}..."}` (named holes in source text; **numbers
  and choices only** — free strings in templates are refused at bind time
  as an injection vector).
- **`skills/inputs.py`**: `inputs_manifest` (qualified `"<node>.<name>"`
  names across a subgraph; duplicate child names refused), `validate_value`
  (numbers clamp into bounds, hallucinated choices revert to the default),
  `resolve_values` (precedence **user > patcher > default**, strict
  unknown-key refusal, garbage degrades to the default), and `bind_inputs`
  (substitutes resolved values into every placeholder; identity when there
  are none).
- **`orchestrator/patchers.py`** — the smart plugin seam: `ValuePatcher`
  protocol, `DefaultValuePatcher` (defaults, free), and
  `GatewayValuePatcher`, which fills the WHOLE manifest with **one batched
  model call** (the node adapts the model via its declared descriptions,
  defaults, and bounds), meters it under `values.patch`, and boxes every
  returned value through `validate_value` — unknown names drop, unusable
  output means defaults. `patch_or_defaults` guards the run path: no
  patcher, a raising patcher, or a dead endpoint all mean the declared
  defaults run; a creative model can improve a run, never block one.
- **Gateway wiring**: listings carry `inputs` (`POST /v1/nodeplace`
  passthrough to the marketplace `NodeContract`), `/v1/market/assemble`
  previews now list the assembled plan's needed inputs with defaults and
  bounds, and `POST /v1/runs/contract` accepts `{"inputs": {...}}`,
  patches + binds **before** compilation (held reserved contracts store
  the concrete values an approver will actually judge), adds the metered
  `patch_cost` to the budget-gated estimate, and surfaces it on the run
  response. Contract-run `outcomes` now include each action's `evidence`,
  so callers see what verification measured.
- **CAD**: `parametric_plate_pack()` — a plate whose width, depth,
  thickness, and hole radius are declared bounded inputs feeding a
  `$template` OpenSCAD source, with a verification spec derived from the
  bounds so EVERY admissible fill verifies (volume brackets computed from
  `t·(w·d − A₆₄(r))`, genus 1 provable for the whole box); and
  `rect_plate_with_hole` — an exact watertight genus-1 reference solid
  matching the closed form to 1e-9, used as the test instrument.
- Tests: `tests/test_value_patching.py` proves the scenario end to end
  through the public gateway — five marketplace nodes assembled by slots
  alone, scaffolding executed in order before the creative step, an LLM
  patch (one metered call) clamped into bounds with invented parameters
  dropped, user values outranking the model, defaults outlasting a dead
  one, and the rendered geometry verified against the analytic spec.

## v0.7.0 — 2026-07-05

Release notes: `docs/releases/v0.7.0.md`.

Unified-surface migration, final step — the loopback surface is gone:

- **Removed** the `workflow_gps.desktop` package (`DesktopService`, the
  loopback app, its view-models and inline UI), `build_desktop_runtime`,
  the `--legacy-loopback` / `--unified` flags, and **`wfgps web`** (the
  shared-token mode built on the loopback shell — superseded by
  `wfgps host`, which is multi-user with real accounts). One surface
  remains: the multi-tenant gateway, with `wfgps desktop` (loopback,
  auto signed in) and `wfgps host` (network, accounts) as its two
  bindings. `wfgps desktop` keeps its flags, port, and data layout — the
  setup scripts and the packaged app work unchanged.
- The `Dockerfile` / `docker-compose.yml` now run `wfgps host`
  (`WFGPS_HOST_SECRET` + `WFGPS_ADMIN_PASSWORD` instead of
  `WFGPS_WEB_TOKEN`); the README's self-hosting section is rewritten
  around accounts.
- Tests moved with the code they prove: the desktop-runtime lifecycle
  tests (planning-only failure, model-driven clarification, injected
  planner end-to-end, CLI-executor confirmation, reopen persistence) are
  ported to the host runtime through real gateway routes; the
  planning-cost/expected-success/cost-weight/default-advising surface
  tests are ported to `/v1/market/assemble`; the loopback-only suites
  (~57 tests whose behaviors have gateway twins) are deleted; the shared
  browser harness moved to `tests/browser_harness.py`.

Unified-surface migration, step 3 — the flip:

- **`wfgps desktop` now serves the unified gateway surface by default**:
  same routes and front-end as `wfgps host`, loopback-only, auto signed
  in, with `--registry` / `--seed-starter` keeping their meaning (the
  starter pack seeds the registry and its skills plan `POST /v1/runs`
  intents) — the setup scripts and the packaged app work unchanged. The
  pre-migration surface stays available behind **`--legacy-loopback`**
  for the transition window; `--unified` remains as a no-op flag.
- Parity screens: the Earnings screen gained the **payout-account card**
  (KYC status when onboarded; a country/currency onboarding form when
  not — a host without a payout adapter answers with the same honest
  404), and Health gained **execution isolation** (a new gateway
  `GET /v1/worker-health` route rendering the enforced
  `IsolationPolicy` via a helper both shells now share — the labels are
  computed from the policy, never restated by hand).
- **First-run crash fixed** (the packaged app's field failure,
  reproduced by the new seeding test): `SkillRegistry`, `TraceStore`,
  `PriceBook`, and `LocalKnowledgeClient` did not create their parent
  directories, so a fresh machine died with sqlite's "unable to open
  database file" before any table logic ran. All path-owning stores now
  create their directories at construction, pinned by a test that
  builds each one under a deliberately nonexistent path.

Unified-surface migration, step 2 — task-flow parity:

- The unified front-end's run detail now makes **every pause kind
  actionable**: clarification questions render as a form (suggested
  values as placeholders) posting `/answers`; route confirmation shows
  the chosen blueprint, estimated cost, and reserved actions with
  Confirm/Decline posting `/confirmation`; approval shows
  granted-of-required with an Approve button (self-approval refusals
  surface as the server's own error); incidents list with Retry/Abort
  posting `/incidents`; and any non-terminal run has a Cancel button.
- Added the **Skills screen** (`/v1/listings?q=` search over published
  marketplace nodes — title, summary, status, tags), degrading honestly
  where nodeplace is not wired.
- A real-Chromium test drives a run that pauses twice — clarification,
  then route confirmation — to completion entirely from the browser,
  via the paste-a-token sign-in path an IdP-fronted host would use.
  Route pins cover every new fragment the page calls.
  With this, the unified surface covers the loopback shell's task flow;
  what remains before flipping the default is the payout-onboarding
  screen and worker health (the Health screen shows gateway metrics).

Unified-surface migration, step 1 — plus field fixes:

- Added **`wfgps desktop --unified`** (opt-in preview): the desktop shell
  served over the SAME multi-tenant gateway `wfgps host` uses — same
  routes, same front-end, same identity semantics — bound to loopback
  with a `local` user auto-provisioned and signed in. The browser opens
  straight into the shell via a `#auth=<token>` bootstrap (the token
  moves into sessionStorage and out of the URL immediately): zero
  ceremony locally, because the loopback bind — not a password — is the
  trust boundary on the user's own machine. Credentials are ephemeral
  per launch by design (fresh secret, rotated password); the data
  directory persists like any host. The default `wfgps desktop` is
  unchanged; the loopback surface remains until the remaining screens
  are ported (step 2).
- **GitHub Actions**: bumped `checkout` v4→v5, `setup-python` v5→v6,
  `upload-artifact` v4→v5, `setup-node` v4→v5 across all workflows
  (the Node 20 runtime deprecation), and the Tauri frontend toolchain
  Node 20→22 (Node 20 is end-of-life). Pinned by a test so a stale
  major cannot creep back.
- **Startup schema guarantee, pinned**: new tests prove a fresh data
  directory answers every read surface before any write (the packaged
  app's exact startup path), restarts reopen and migrate cleanly (the
  admin created before a host restart still signs in after), and every
  SQLite store creates its schema at CONSTRUCTION time — "no such
  table" is structurally impossible on a fresh install.

The multi-user gateway grows a face:

- Replaced the gateway front-end (served by `GatewayASGI` at `GET /`)
  with a **sign-in page + shell**: username/password → `POST
  /v1/auth/login`, the bearer token lives in `sessionStorage` for that
  tab (a 401 signs the tab out; sign-out drops it), and every fetch
  carries `Authorization: Bearer`. IdP-fronted hosts (no local accounts)
  get a paste-a-token fallback on the same page.
- Screens over the authenticated surface: **Runs** (start an intent,
  list, detail with audit timeline + live WebSocket frames via the
  bearer subprotocol), **Assemble** (goal → priced preview with
  planning cost, expected success, and budget verdicts → run the
  contract; a 202 hold links to the inbox), **Inbox** (approve/decline
  held reserved contracts), **Earnings**, **Users** (admin-only:
  create, disable/enable), **Health**. Screens degrade honestly: 404 →
  "not enabled on this host", 403 → "no authority for this screen".
  XSS-safe by construction (DOM building, no HTML templates, no
  `innerHTML`), pinned by tests along with every route the page calls.
- Real-Chromium end-to-end tours against a real host runtime: sign-in
  (wrong password says only "invalid credentials"), admin provisions
  and disables a user from the browser and the disabled account is
  locked out, and a member sees the Users screen refuse and the
  unwired Earnings screen say so — instead of breaking.

Multi-user web hosting — accounts, not a shared token:

- Added `identity.accounts`: **local user accounts** as the identity
  provider a self-hoster lacks. Passwords are scrypt-hashed (stdlib;
  per-user salt, cost parameters recorded next to the hash), login mints
  a short-lived HS256 token through the SAME `OidcValidator` path an
  external IdP would use, and **roles become stored grants** — a forged
  token claim still buys nothing. Login failures are uniform ("invalid
  credentials" for unknown / wrong-password / disabled alike — no account
  enumeration), unknown users cost the same scrypt work as wrong
  passwords (decoy verification), and repeated failures lock the username
  briefly.
- New gateway routes (answering only when accounts are configured —
  IdP-fronted installs keep their 404): public `POST /v1/auth/login`;
  `GET/POST /v1/auth/users` and `POST /v1/auth/users/{name}/disabled`
  behind stored `users:manage` authority, tenant-scoped (admins provision
  their own tenant; the tenant comes from the session, never the body).
- Added `build_host_runtime(data_dir=, secret=)`: the full multi-tenant
  gateway (runs, marketplace, ratings, pricing, traces, approvals) over
  one backupable data directory, wired with local accounts. Refuses
  secrets under 32 characters.
- Added **`wfgps host`**: serves it, bootstraps the first admin
  (idempotently — never resets an existing password) from
  `WFGPS_ADMIN_PASSWORD` or a generated password shown once, warns when
  the signing secret is ephemeral, and says loudly to put HTTPS in front.

The self-host runner for online web users:

- Added **`wfgps web`**: the desktop shell served over the network,
  wrapped in `desktop.web.TokenGuardedApp` — the one property that makes
  a non-loopback bind defensible: nobody without the access token gets
  anything. Browsers sign in once at `/login?token=…` (HttpOnly /
  SameSite=Lax session cookie; sessions are in-memory, so a restart signs
  everyone out), API clients send `Authorization: Bearer <token>`, and
  WebSocket upgrades ride the cookie (4401 without). Token comparison is
  constant-time; the 401 page is deliberately information-free. The token
  comes from `WFGPS_WEB_TOKEN` (or is generated and printed once), must be
  ≥16 characters, and the startup banner says loudly to put HTTPS in
  front. `wfgps desktop` stays loopback-only, unchanged.
- Added a **`Dockerfile` + `docker-compose.yml`**: the shell behind the
  token on one backupable `/data` volume; compose refuses to start
  without `WFGPS_WEB_TOKEN`. Both pinned by tests, documented in the
  README's "Self-hosting for online web users".

Onboarding hardening (from a field DX audit) — every install trap grows
directions:

- Added **`wfgps doctor`**: checks Python version, data-dir writability,
  each optional stack (with the exact `pip install "workflow-gps[…]"` to
  run), the configured model endpoints (a probe that treats any HTTP
  answer as alive — 401 is not "down"), and the API-key requirement.
  Missing *optional* stacks are guidance, not failure: a desktop-only
  machine reports healthy. Exit 1 only on real problems, each with its
  one-line fix.
- **`wfgps run` preflights** the three classic fresh-install traps before
  any engine machinery can produce a misleading traceback: missing
  `[engine]` extras, no model server answering at the configured
  `api_base` (the silent `localhost:8000` trap — the error now names
  vLLM/Ollama/LM Studio and `--config models.yaml`), and an unset
  `OPENAI_API_KEY` (any value works for vLLM). `--no-preflight` bypasses;
  injected builders (tests, embedders) are never preflighted.
- **Dead ends answer with directions**: running
  `python src/workflow_gps/cli.py` as a bare file now prints how to run
  it properly (setup scripts / `wfgps` / `python -m`) instead of a
  relative-import traceback, and `uvicorn workflow_gps.gateway.asgi:app`
  serves a 503 signpost explaining that `GatewayASGI` is a class needing
  a wired `GatewayApp` — with the real local commands — instead of
  uvicorn's "Attribute 'app' not found".
- **Setup scripts bootstrap pip**: a `.venv` created by a stripped-down
  Python (no pip) is repaired via `ensurepip` instead of failing later
  with "No module named pip".
- Added the **`ci` GitHub Actions workflow** (lint + full test suite on
  every push/PR and on demand via `workflow_dispatch`); all three
  workflows are hand-dispatchable, pinned by a test.
- Moved model-call pricing from `metering.model_calls` to
  **`billing.model_calls`** — the metering package's own tested invariant
  is that it exposes no money symbols (metering counts usage; billing
  prices it), and the meter violated the layering. Import paths change;
  behavior does not.

- The learned planner is now **wired in by default**: when a surface has
  a `trace_store` and no explicit `proposal_model`, producer picks are
  advised by `TraceProposalModel` over the caller's own recorded runs —
  free, evidence-only, and tenant-scoped (the gateway constructs the
  model per request with the calling tenant's context, so one tenant's
  history never enters another's evidence pool; the desktop uses its
  single-user bucket). An explicitly passed `proposal_model` always
  wins. Pinned in tests with run-level evidence per-node personalization
  cannot see: steps that succeeded inside runs that failed as wholes.

The first domain pack — CAD, with verification grounded in mathematics:

- Added `domains.cad.geometry`: exact mesh mathematics with stated
  hypotheses. Volume by the divergence theorem (Σ v0·(v1×v2)/6 — exact
  on closed, consistently oriented meshes; translation invariance and
  orientation antisymmetry are *asserted in tests*, not assumed),
  surface area, extents, and a combinatorial `ManifoldReport`
  (boundary / non-manifold / misoriented edges, degenerate triangles,
  connected components, Euler characteristic, and genus via χ = 2c − 2g).
  STL both directions, with binary detection by the exact length
  equation — never the header, which real files lie about.
- Added `domains.cad.verify`: `GeometrySpec` (watertightness, volume and
  area intervals, extent box-fit, exact genus) → `GeometryReport` with
  measured numbers behind every failure. Volume is *withheld* on open
  meshes — the formula's hypothesis failed, so no number beats a wrong
  number.
- Added `domains.cad.OpenSCADExecutor` (adapter `cad`): deterministic
  `render_stl` through the OpenSCAD CLI (binary configurable as an argv
  prefix — tests drive the real subprocess path via a stub renderer;
  a `skipif` test runs the true binary when installed) and pure-Python
  `verify_geometry`. A failed predicate fails the action → the run → the
  earnings, and the trace posterior records the failure honestly: the
  platform's money-on-verified-success promise, enforced by geometry.
- Added `domains.cad.cad_starter_pack()`: a parametric mounting plate and
  its verification node, slot-chained. The spec's bounds bracket
  closed-form values (inscribed-polygon hole area (n/2)r²sin(2π/n),
  perimeter 2nr·sin(π/n); volume ≈ 3087.08 mm³, area ≈ 2098.9 mm²,
  genus exactly 1) — tight enough to refute a hole-less or double-holed
  part outright, recomputed from the formulas in the tests. A gateway
  test contributes both nodes and goal-assembles them: CAD nodes are
  ordinary marketplace citizens.

The trace corpus and the first learned planner:

- `TraceStore` now logs every recorded run **verbatim** (`trace_runs`, a
  new migration existing databases adopt cleanly): the aggregates grade
  nodes; the log answers "what did whole successful plans look like".
  Read it with `runs(context=, goal=, limit=)` — newest first, `None`
  filters mean all, the empty string stays a real bucket.
- Added `knowledge.corpus`: `build_examples` turns runs into
  (goal, plan-prefix → next node) training examples — the shape a
  forward-generating sequence model trains on — and `export_jsonl`
  writes them oldest-first as a portable file for offline model
  training. Failed runs export flagged (`run_success: false`), never
  silently dropped.
- Added `orchestrator.TraceProposalModel`: the baseline learned planner
  behind the same `ProposalModel` seam a Mamba/SSM checkpoint later
  implements. It proposes live from the caller's own run log, judged
  against the most specific evidence pool available (runs of this goal →
  runs sharing an already-selected node → all runs; the budget layer's
  class-first shape), weights candidates by the Beta mean of the runs
  they appeared in, has no opinion where it has no evidence, and costs
  nothing. A future sequence checkpoint must beat it in the replay
  harness to earn its inference cost.

Thompson v2 — the learning loop gets honest about time, money, and proof:

- `TraceStore` gained `recency_decay` (default 1.0 = today's exact
  counting): every fresh observation of a node first discounts its
  existing counts, so the posterior tracks what the node has done
  *lately* — a node that regressed last month stops looking as good as
  ever, old glory fades into honest uncertainty, and Thompson sampling
  re-explores it. Posterior (and `NodeStats`) counts are floats now.
- `ContractAssembler` (and previews on both surfaces, via `cost_weight`
  in the request body) can rank picks by expected **utility** — quality
  minus weighted personal cost — instead of quality alone, so a
  slightly-less-proven cheap node can honestly beat a proven expensive
  one, by exactly the trade the caller declared. Default 0 keeps cost a
  tie-break, unchanged.
- Previews now report `expected_success`: the plan's chance of verified
  success in the caller's own hands (product of picked nodes' posterior
  means over the personalized library; gap nodes count at their uniform
  prior 0.5). Shown on the desktop assemble screen.
- Added `knowledge.replay`: an offline harness where planner strategies
  audition before they ship. `ReplayWorld`s (fittable from recorded
  history via `from_trace_store`) run in drift-modeling phases,
  `PosteriorStrategy` replays the assembler's exact pick math with its
  own private trace store, and every strategy sees the same seeded
  outcome stream — reports compare decisions, not luck. Pinned in tests:
  decay adapts to drift faster, cost-awareness buys success cheaper.

- Added the `ProposalModel` seam to `ContractAssembler`: a model may weigh
  in on contested producer picks, but only as a **prior** — its `[0, 1]`
  weights enter the same Beta posterior verified history feeds, as
  pseudo-observations (`proposal_strength`, default 3) that decide
  thin-history ties and wash out as real evidence accumulates. Advisory by
  construction: unknown ids are dropped, wild weights clamp, exceptions
  (including a dead model endpoint) downgrade to verified-history-only
  assembly, and a single-candidate pick never spends a model call.
- Added `billing.model_calls`: `ModelCallMeter` records every completion's
  token telemetry under a purpose tag and a `ModelPriceTable` (per-tier
  cost per million tokens; unknown tiers priced conservatively) turns it
  into money — model calls are never free.
- Added `orchestrator.proposals.GatewayProposalModel`: the seam implemented
  over the same routing `Gateway` the synthesis engine uses (frozen
  cache-safe system prompt, fast tier, small completion budget, strongest
  candidates shortlisted), with defensive weight parsing — unreadable
  advice is no advice — and every call metered.
- Assembly previews now surface `planning_cost` (what the advice cost,
  distinct from market gross since no noder earns it) on both surfaces,
  and the budget verdict judges **gross + planning cost**: a plan that
  needed advice is honestly dearer. New ctor knob `proposal_model` on
  `GatewayApp`, `DesktopService`, and `build_desktop_runtime`; the desktop
  assemble screen shows the planning line when it is nonzero.

## v0.6.0 — 2026-07-05

Release notes: `docs/releases/v0.6.0.md`.

Reward & pricing system (`claude/oolu-workflow-planning-review`) — the
economic layer for Noders and route planning; design in
`docs/REWARD_PRICING_DESIGN.md`.

- Added `nodeplace.economics`: `CandidateAssembler` joins the registry,
  metering ledger (verified successes + measured provider cost), audit log
  (real failure counts via run bindings), and rating store into
  `CandidateEconomics` + `RewardSignals` per listing, with substitutes
  computed per class key; listing tags (`class:`, `market:`) carry market
  classification, and the contribute endpoint now accepts a `pricing` ask.
- Added gateway routes `GET /v1/market/candidates` (utility-ranked live
  candidates with cleared-price breakdowns and reward multipliers;
  read-only — browsing previews prices without moving the book) and
  `POST /v1/market/quotes` (full workflow quote from live economics;
  previews by default, never a ledger write), documented in the OpenAPI.
- `PriceBook.clear` and `QuoteEngine.quote` gained preview modes
  (`commit=False` / `commit_prices=False`) so read paths cannot shift
  market reference prices.
- `POST /v1/runs` accepts an optional `node_version_id`: the gateway
  assembles that version's live economics, clears the price (committing —
  a real run moves the market), and binds the run to its noder shares via
  `build_run_binding` inside the idempotent submit, before returning. The
  metering deriver turns the binding into earnings only when the audit log
  shows a platform-verified success for that run — closing the last manual
  gap between quoting and the exactly-once earnings pipeline. Unlisted or
  revoked versions are refused; plain runs are untouched.

- Added `nodeplace.market`: node pricing classes (commodity / workflow /
  professional / regulated pass-through), `CostVector`, and a persisted
  `PriceBook` that clears asks through cost floor -> competition pull ->
  value anchor -> per-class damping bands, with an explainable
  `ClearedPrice` breakdown. Regulated fees pass through untouched.
  Route economics (`utility`, `rank_candidates`) score candidates by
  platform-verified quality per retry-adjusted dollar under four quote
  modes (budget/standard/premium/certified) — never by self-declared
  quality.
- Added `nodeplace.rewards`: bounded reward multipliers from non-gameable
  signals (ratings reputation, metered reliability, scarcity, maintenance,
  commodity decay), class-aware platform commission (lowest for scarce
  professional supply, zero on pass-through), geometric lineage royalties
  for derived nodes, and `build_run_binding` — the bridge into the
  exactly-once metering -> billing -> ledger -> settlement pipeline, so
  money still moves only on platform-verified success and every split
  conserves to the micro.
- Added `nodeplace.quotes`: `QuoteEngine` with subscription coverage vs
  outside-plan pass-through lines, retry-adjusted budget projection,
  accumulating budget/quota warnings, per-step noder payout *previews*
  (forecasts, clearly labeled — never ledger entries), and usage settling.

Adaptive planning (`claude/oolu-workflow-planning-review`) — implements the
typed-capability-graph proposal in `docs/WORKFLOW_PLANNING_REVIEW.md`; the
planner now grows automatically with the user's executions and learned skills.

- Native installer packaging (`packaging/`): `python packaging/build_installer.py`
  produces a single self-contained executable (`dist/WorkflowGPS-Shell`,
  `.exe` on Windows) via PyInstaller — copy it anywhere, double-click,
  the shell starts, the browser opens, data lives in `~/.workflow-gps`.
  The frozen launcher (`shell_launcher.py`) is a thin wrapper over the
  same `wfgps desktop` invocation the setup scripts use (one launch path
  to keep honest), with a free-port fallback so a busy 8765 never turns
  into an error dialog. The spec bundles the starter-pack data
  (importlib.resources inside the frozen app) and uvicorn's dynamic
  imports statically, and excludes every heavy optional stack.
  PyInstaller cannot cross-compile, so `.github/workflows/
  build-installers.yml` builds Windows/macOS/Linux binaries on every
  version tag. Validated live: the Linux binary built here serves the
  UI, seeded skills, and earnings standalone. `tests/test_packaging.py`
  pins the launcher argv against the real CLI, the port fallback, the
  spec's bundling, and the CI wiring.
- One-step setup for non-developers: download the repo ZIP, unzip, and
  run `setup.bat` (Windows, double-clickable) or `./setup.sh`
  (macOS/Linux). The scripts find Python 3.11+ (with a friendly pointer
  when it's missing), create a private `.venv` inside the folder,
  install only the `serve` extra (the shell never needs the heavy
  `engine`), and launch `wfgps desktop --seed-starter --open` — which
  now auto-opens the browser (new `--open` flag) and prints a
  human-readable startup message. Idempotent: re-running reuses the
  environment and just starts the shell; nothing lands outside the
  folder. The README opens with a "Quickstart — download → run" section,
  and `tests/test_setup_scripts.py` pins every link of the story (the
  scripts' install command and launch flags against the CLI parser, the
  README pointers) so the setup path can never silently rot.
- Browser-level end-to-end tests (`tests/test_browser_e2e.py`): a real
  Chromium drives the real front-end over a minimal in-test ASGI HTTP
  server (no external server dependency). The tour: assemble the seeded
  marketplace chain, watch the budget verdict, confirm the run through
  the shared money path, onboard a payout account (KYC pending blocks
  payouts), and render health; a second test proves the task screen
  degrades gracefully where the transport has no websockets. Skips
  cleanly wherever the `browser` extra (playwright) or a Chromium
  executable is unavailable; falls back to the host-installed
  `/opt/pw-browsers/chromium` when playwright's own download is absent.
- Payout-account onboarding in the shell: `DesktopService.payout_account`
  / `onboard_payout_account` (new `payout_adapter` ctor hook, also a
  `build_desktop_runtime` passthrough) over `GET`/`POST
  /v1/payout-account`. Not-onboarded is a rendered state (200), never an
  error; onboarding is idempotent (an account is an external resource,
  returned rather than minted twice) and audited (`payout.onboarded`);
  the KYC status is refreshed from the processor on every read and the
  refresh persisted — verification happens on THEIR side, the shell only
  mirrors it, and `payouts_enabled` flips only on `verified`. The
  Earnings screen gains a payout-account card: onboarding form when
  absent, status badges ("payouts blocked until KYC verifies") after.
- Earnings wired into `build_desktop_runtime`: shells get the earnings
  screen out of the box — the runtime creates an `EarningsLedger` and
  `PayoutStore` over its own durable connection (honest zeros until the
  user's contributions earn), passes them to the shell under the new
  `noder_principal` parameter (default `"local-noder"`; `None`
  disables), and exposes them on `DesktopRuntime.earnings` /
  `.payouts` — hand THOSE to a settlement job so the screen and the
  money pipeline share one truth.
- Desktop earnings screen: `DesktopService.earnings()` (new
  `earnings_ledger` / `payout_store` / `noder_principal` ctor wiring)
  projects the local noder's ledger into a secret-free `EarningsView` —
  available/pending/reserved/lifetime-paid balance tiles, the ledger
  lines (kind, amount, event, availability; most recent first), and
  payout batch history — served at `GET /v1/earnings` (404 when the
  shell has no earnings wiring). Amounts cross the loopback in currency
  units; the ledger keeps its integer micros, and the shell can show
  the money but never move it. The front-end gains an Earnings screen
  with color-coded entry kinds and an explicit negative-balance
  explainer (a clawback exceeded the reserve; new earnings repay first).
- Desktop front-end (replacing the scaffold screen by screen, still one
  self-contained page with no build step): a DOM-builder kernel (`h()`)
  replaces innerHTML templates — every dynamic value is a text node, so
  the page is XSS-safe by construction; a hash router gives each screen
  and each task a deep-linkable address (`#/task/{run_id}`). The screens
  now drive the WHOLE loopback surface: the new task-detail screen
  answers clarification questions, previews and approves/declines
  routes, resolves incidents (retry/abort), cancels, and streams the
  live timeline over the websocket; Assemble renders per-step clearing
  forces and keeps its form across navigation; Inbox links run pauses to
  their task screens; a new Skills screen searches the library. The
  wiring test now pins all of it.
- Desktop UI scaffolding (`desktop/ui.py`, served by the loopback at
  `GET /`): one self-contained page — plain HTML + vanilla JS, no build
  step — over the same loopback endpoints the tests drive, so the page
  can never do anything the API cannot. Four screens: **Assemble** (goal
  + slots + budget knobs + explore/fill-gaps, preview with per-step
  prices/payouts, learned orderings, and the budget verdict; confirm
  with review acknowledgement, rendering held-for-approval outcomes),
  **Tasks** (submit + session task table), **Inbox** (all pause kinds;
  contract-approval items get approve/decline buttons using a bearer
  token held in page memory only — every decision is verified
  server-side), and **Health**. Light/dark aware, XSS-escaped rendering.
  Tests pin the page's wiring to the real routes and syntax-check the
  inline script with node (skipped where node is absent).
- Hardening passes: property-style fuzzing of the money invariants and
  concurrency stress on the shared stores (no new dependencies — seeded
  `random`, explicit seeds, failures replay exactly). The money machine
  (12 seeds x 50 random ops: accruals, clock advances, settlement cycles
  with a flaky processor, upheld/rejected disputes) checks after every
  step that the reserve is never negative, lifetime payouts never exceed
  gross accruals (money is never minted), only upheld clawbacks can
  drive a balance negative, and the ledger's PAYOUT outflow equals what
  the processor actually paid — then jumps past the risk window for the
  eventually-100% endgame (gross == paid + available + reserved, residue
  below threshold). The concurrency suite races 16 barrier-synchronized
  threads at the primitives: idempotent `run` executes exactly once (and
  exactly once again after `release`), ledger dedup admits one row per
  unique key with no lost distinct writes, a hold is decided by exactly
  one contender (with sweeps racing adds), and trace statistics lose
  nothing across threads.
- Reserve release — the holdback is a loan, not a fee: the settlement
  reserve target is now scoped to the chargeback **risk window**
  (`risk_window_days`, default `DEFAULT_RISK_WINDOW_DAYS = 90`; `None`
  restores accumulate-forever). The true-up is symmetric: fresh earnings
  top the reserve up, and accruals that age out of the window release
  their share back to the noder as one more RESERVE entry — paid out on
  the next settlement, so the noder eventually receives 100% of
  undisputed earnings. Aged-out accruals demand no reserve at all
  (only at-risk earnings are held against).
- Dispute deepening — reserve-funded clawbacks, final decisions:
  upholding a dispute still reverses every accrual the event minted
  (CLAWBACK entries, per noder), but a shortfall from already-paid
  earnings is now funded from the noder's RESERVE first — the settlement
  holdback finally doing the job it exists for — via a negative RESERVE
  release entry, so the balance projection stays one formula. Only what
  the reserve cannot cover remains as honest negative balance (debt)
  that future accruals repay before anything pays out again. The
  settlement reserve target now nets clawbacks (reversed earnings no
  longer demand reserve, so a clawback isn't re-collected as a fresh
  top-up). Decisions are final: uphold-after-reject and
  reject-after-uphold raise, the same decision twice is a no-op/replay,
  and both resolutions are audited (`dispute.upheld` with clawed/drawn/
  debt micros, `dispute.rejected`). Uphold reports a per-noder breakdown.
- Settlement cycles + payment-failure containment:
  `SettlementService.settle_all(period_key=...)` settles every noder on
  the ledger (`EarningsLedger.principals()`) for one period — outcomes
  are per-noder and independent, so one processor failure never blocks
  anyone else's payout; the cycle summary (paid/failed/skipped counts
  and paid micros) is appended to the durable audit as
  `settlement.cycle`. A `PaymentError` inside `settle` is now a
  first-class outcome instead of a crash: the batch is marked FAILED for
  the record, the ledger is never debited, and the period's idempotency
  claim is released via the new `IdempotencyLedger.release(key)` — fixing
  a real poisoning bug where a raised `fn` left a claim that replayed
  `None` forever. Re-running the same period IS the retry mechanism:
  paid noders replay their cached receipts (the processor is never
  called twice), failed ones get a fresh attempt with a fresh batch.
- Approver notification — the holds SSE feed:
  `GET /v1/runs/contract/holds/events` streams the tenant's hold
  lifecycle so approvers subscribe instead of polling the listing. Same
  snapshot semantics as the per-run event stream: frames are derived
  from the audit log (`contract.held` is now audited at hold time on
  both surfaces, and held/approved/declined/expired payloads carry the
  tenant), so nothing is invented for the transport and the feed is
  strictly tenant-scoped. Each frame carries `id: <seq>`; `?after=<seq>`
  resumes past frames already seen (SSE Last-Event-ID semantics). The
  request itself sweeps, so an expiry becomes an event, never silence.
- Hold expiry: a held reserved contract carries an `expires_at` stamped
  at submission (the promise made then — TTL changes never retroactively
  extend old holds). Gateway: `GatewayConfig.contract_hold_ttl_seconds`
  (default 7 days; `None` = never), `expires_at` on the 202 response and
  hold listings, and a late decision returns 410 `expired`. Desktop:
  `hold_ttl_seconds` (+ injectable `clock`) ctor knobs, default never.
  Expiry is lazy — `PendingContractStore.sweep_expired` runs on every
  list/inbox and decision, so a stale hold can never rot in the queue or
  be released long after the submitter's intent went cold; each sweep is
  audited per hold as `contract.expired`.
- Gateway hold-for-approval for reserved contracts: `POST
  /v1/runs/contract` no longer 403s a contract with reserved actions —
  it HOLDS it (202 `awaiting_approval` with a `pending_id`, idempotent
  under the Idempotency-Key, budget knobs captured at submission).
  `GET /v1/runs/contract/holds` lists the caller tenant's holds;
  `POST /v1/runs/contract/holds/{pending_id}` decides one. Decisions are
  tenant-scoped (another tenant's hold is a 404 — existence never
  leaks), require approve authority in the hold's own tenant (the
  submitter's own token gets 403 and the hold survives), re-run the
  budget gate on the SUBMITTER's terms and histories (402/409 leave the
  hold intact), and execute with the run bound to the ORIGINAL
  submitter — the approver authorizes, never takes the consumer seat.
  Declining removes the hold; both outcomes are audited with the
  decider's principal. The shared `PendingContractStore` moved to
  `nodeplace.holds` (table `pending_contracts`, records now carry the
  submitting tenant/principal, `list(tenant=...)` filters) and backs
  both surfaces, so gateway holds also survive restarts and every
  process over one database sees one consistent set.
- Held approvals survive restarts: pending reserved contracts moved from
  process memory into the shell's own durable database
  (`desktop.pending.PendingContractStore`, table
  `desktop_pending_contracts`) — a hold is a commitment the user made,
  so it lives with the runs. The record stores the contract as posted
  plus the budget knobs captured at confirm time; the compiled blueprint
  is deliberately NOT persisted (script bodies mint fresh action ids per
  compile) — whichever process decides the hold recompiles once and
  executes exactly what it inspected. A fresh `DesktopService` over the
  same durable connection lists and decides holds made before a restart,
  and every service over that store sees decisions immediately.
- Loopback route for the approval decision:
  `POST /v1/assembly/approvals/{pending_id}` with `{"approved": bool}`
  decides a held reserved contract from the desktop UI. The loopback
  stays a no-auth boundary with one deliberate exception: this route
  REQUIRES an `Authorization: Bearer` token, which
  `DesktopService.decide_assembly` turns into a verified identity
  session (`SessionManager.login`) before handing off to
  `approve_assembly` — caller text never becomes authority. Missing/bad
  token -> 401, valid-but-unauthorized principal -> 403 (the hold
  survives every failed attempt), missing `approved` field -> 400,
  unknown or already-decided hold -> 404, no session manager wired ->
  404. New `session_manager` ctor hook on the shell.
- Desktop reserved contracts become approvable inbox tasks: confirming a
  contract with reserved (irreversible) actions no longer 403s — it is
  HELD (`awaiting_approval`) and appears in the inbox as kind
  `contract-approval`, naming the reserved operations.
  `DesktopService.approve_assembly(pending_id, session=...)` decides it:
  approval mints from a verified identity session (same
  `IdentityApprovalAuthority` gate as run approvals — an unauthorized
  session raises and the hold survives), re-runs the budget gate (prices
  may have moved while held; approval grants the reserved actions, not
  the money), then executes through the shared money path; declining
  removes it. Both outcomes are audited with the decider's principal.
  `nodeplace.execution` splits `compile_contract` (no reserved gate, for
  approval flows) + `reserved_operations` out of `compile_runnable`
  (which still refuses — the gateway's unattended path is unchanged).
- Recency decay on spending profiles: history weighs `recency_decay`
  (default 0.9) less per run back, so comfort tracks where spending is
  *trending*. `SpendingProfile.typical` is now a recency-weighted median,
  and the ceiling is driven by `recent_peak` — a decaying maximum — so
  one lavish run long ago stops waving outliers through as it ages, and
  a user who has tightened gets a ceiling that followed them down; `peak`
  stays the raw historical maximum for honest display. Applies to global
  and class profiles alike (histories are most-recent-first, as
  `consumer_spend` returns them); `recency_decay: 1.0` in the budget
  policy restores flat history exactly.
- Per-goal-class spending profiles: behavioral budgets are judged within
  the plan's own class of goal — spending lucratively on gifts while
  keeping everyday automation tight is two different spenders, and
  neither habit loosens (or flags) the other. `RunBinding` gains a
  `goal_class` (the class key of the run's costliest child, stamped by
  `execute_contract` and `build_run_binding`), `consumer_spend` filters
  by it, and `estimate_contract_gross` returns a `ContractEstimate`
  (gross + dominant class). `assess_budget` is class-first: a class with
  enough history REPLACES the global profile for the behavioral check
  (reasons name the class); a class with thin history falls back to the
  global profile — so a first lavish run in a new class gets exactly one
  review, then the class speaks for itself. Verdicts carry `goal_class`
  and `class_profile`; `preview_assembly` takes a `spend_lookup`
  (class -> history) since the plan's class is only known after assembly.
- Cost-aware assembly budgets (`nodeplace.budget`): three signals with
  three authorities judge an assembled plan's estimated cost. A
  caller-set `hard_cap` refuses outright (`BudgetExceededError` -> 402
  `budget_exceeded`; no acknowledgement overrides it); a user-set
  `review_threshold` holds the run (`ReviewRequiredError` -> 409
  `review_required`) until `review_acknowledged: true`; and a
  **behavioral comfort ceiling** learned from the user's own committed
  run grosses (`AttributionStore.consumer_spend`; review above BOTH
  median x multiplier AND their demonstrated peak, never judged on
  fewer than 3 runs) flags outliers even with no declared budget.
  The linked wallet is deliberately the weakest signal — its balance may
  be a slice of the user's true assets, so it NEVER caps or scales the
  budget: an estimate above the remaining balance only adds a review
  reason, and a large balance grants nothing. Estimation
  (`estimate_contract_gross`) clears in preview mode, so the gate runs
  BEFORE any price commits or binding writes. Reasons accumulate across
  all signals like quote warnings. Wired everywhere: verdicts ride
  `/v1/market/assemble` and the desktop preview (`budget` field, from a
  `budget` request object / `budget_cap` + `review_threshold` params);
  enforcement guards `POST /v1/runs/contract` and the desktop confirm
  (403 at the loopback); `wallet_lookup` ctor hooks on both surfaces.
- Trace-derived learned orderings in assembled subgraphs: when the
  caller's own runs consistently completed one child before another
  (`TraceStore.derive_edges`: enough observations, one direction nearly
  always, transitively reduced), `preview_assembly` stamps that order
  onto the assembled contract as `provenance="learned"` `ContractEdge`s —
  which the compiler already turns into real dependencies, so the
  scheduler stops racing steps the user's history says are ordered. Slot
  flow outranks statistics: learned edges that data-flow or explicit
  edges already imply or contradict are dropped (a contradiction stays
  parallelism, never a learned cycle), and ambiguous child names are left
  out. Surfaced as `learned_order` (`[{"first", "then"}, ...]`) on the
  assemble response and the desktop `AssemblyPreviewView`.
- Thompson-sampled assembly (`explore: true`): `preview_assembly` accepts
  an `rng` and passes it to `ContractAssembler`, so producer picks are
  sampled from the same personalized Beta posteriors instead of taken
  greedily — unproven alternatives get chances proportional to their
  remaining uncertainty, and exploration collapses onto the winner as
  confirmed runs accumulate. Opt-in per request: `explore: true` on
  `POST /v1/market/assemble` and on the desktop's
  `POST /v1/assembly/preview` (`DesktopService.assembly_preview(...,
  explore=True)`); the default stays deterministic (best posterior mean,
  stable tie-breaks) — the right mode for a preview the user is about to
  pay for. The gateway and shell hold a seedable `rng` (ctor param).
- Confirmed runs feed the TraceStore: `execute_contract` accepts a
  `trace_store` (+ `trace_context`) and records one node-granular trace
  per run — each top-level child's verdict (a child succeeds only if
  every action it contributed did), the price it actually cleared at as
  its cost EWMA, and completion order into the precedence matrix — under
  the same `route:{name}` keys the assembler scores by.
  `compile_with_owners` (orchestrator) returns the blueprint plus an
  action-to-child attribution map from ONE compile pass (script bodies
  mint fresh action ids per compile, so a second pass would not match);
  `compile_runnable` now returns a `CompiledContract` carrying both.
  On the pick side, `preview_assembly` folds the caller's own history
  into each contract's `NodeStats` (evidence adds; the personally paid
  cost supersedes the listed one). Gateway: new `trace_store` ctor param,
  bucketed per tenant (`trace_context=tenant_id`) so one tenant's
  failures personalize only their own picks; desktop: `trace_store` ctor
  param on the shell (single user: the global bucket). Every confirmed
  run sharpens the next assembly — no separate training step.
- Desktop confirm button: `DesktopService.confirm_assembly` runs the
  contract the preview returned — through the shared
  `nodeplace.execution.execute_contract`, the exact code path behind the
  gateway's `POST /v1/runs/contract` (extracted in this change), so there
  is one place where contract runs turn into money: committed per-node
  clearing, one aggregate lineage-weighted `RunBinding`, and the
  deriver-payable `workflow.executed` audit event. Served over the
  loopback at `POST /v1/assembly/confirm`; reserved actions are refused
  with 403 (`ReservedActionsError`, a `PermissionError`), executors are
  backend-configured (never UI-supplied), and a client `confirm_id` makes
  the click idempotent — double-clicks replay the first result without
  re-executing.
- Direct contract execution + desktop assembly preview: `POST
  /v1/runs/contract` takes the contract `/v1/market/assemble` returned,
  compiles it to a DAG blueprint (`contract_to_blueprint`), and executes it
  on the gateway's configured `contract_executors` (`DagRouteRunner`) —
  every marketplace node in the subgraph clears at a *committed* price and
  the run gets one aggregate `RunBinding` whose shares merge each node's
  lineage split weighted by its cleared price, so the metering deriver pays
  every noder in the chain from the same platform-verified audit event.
  Reserved (irreversible) actions are refused with 403 — those still
  require the orchestrator's approval flow. The shared preview computation
  moved to `nodeplace.assembly.preview_assembly`, and the desktop shell
  surfaces it: `DesktopService.assembly_preview` (optional
  `market`/`price_book` wiring) maps it into the secret-free
  `AssemblyPreviewView`, served over the loopback at
  `POST /v1/assembly/preview` — read-only, prices never commit.
- Slot vocabularies on listings + goal-based assembly over the marketplace:
  `Listing` gains typed `consumes`/`produces` slots — declared at
  contribution (service + gateway body fields) or derived from the skill
  itself (induced parameters -> consumes, artifact validators ->
  produces). `CandidateAssembler.contracts(query)` turns every active
  public listing into an assembler-ready `NodeContract` (listing slots as
  typed I/O, the sanitized skill's actions as the executable body,
  verified history as stats). `POST /v1/market/assemble` backward-chains
  a goal's wanted slots through those vocabularies and returns the
  assembled subgraph contract with per-node cleared-price previews and
  lineage-aware payout previews — read-only: the price book never moves,
  and no money does either. Missing slots report honestly, or
  (`fill_gaps: true`) become synthesized script gap nodes.
- Goal-directed assembly: `orchestrator/assembler.py` adds
  `ContractAssembler` — give it a `GoalSpec` (wanted slots + slots on hand)
  and a contract library (a list, or a callable over a live registry via
  `contract_from_registered`, which carries trace-store history), and it
  backward-chains producers by verified success (deterministic, or
  Thompson-sampled with an `rng`), skips what is on hand, dedupes shared
  producers, and returns one `SubgraphBody` contract whose ordering falls
  out of slot flow at compile time. Unproducible slots are reported as
  `missing` — or, with `fill_gaps_with_scripts=True`, become synthesized
  `ScriptBody` gap nodes the node-cached script runner realizes and
  memoizes at execution time.
- Lineage records on `NodeVersion`: `contribute(derived_from=...)` (service
  and gateway) records the parent and its ancestors as immutable
  `LineageRecord`s (levels shift by one per generation, capped at
  `MAX_LINEAGE_DEPTH=5`; unknown parents are refused). When a marketplace
  run binds (`POST /v1/runs` with `node_version_id`), royalty ancestors now
  fill automatically from the version's recorded lineage
  (`CandidateAssembler.lineage_for`) instead of caller input — derivation
  provenance is the source of truth, and the geometric royalty split pays
  upstream noders on every verified success.
- NodeContract unification (build-order item 6 — the review is complete):
  `skills/contract.py` defines the one node schema the three vocabularies
  converge on — typed `Slot` consumes/produces, three body kinds
  (`ActionsBody` | `ScriptBody` | `SubgraphBody`), the existing
  `ConstraintSpec` preconditions/validators, a verified-history `NodeStats`
  snapshot, a `fallback` contract, and the canonical `classify_risk` (the
  orchestrator now re-exports it). `NodeContract.from_skill`/`to_skill`
  round-trip losslessly; `derive_data_edges` orders subgraph children from
  slot unification (unrelated children stay parallel; mutual production is
  rejected as a cycle, never silently reordered).
  `orchestrator/contract.py::contract_to_blueprint` compiles any contract
  into an executable DAG blueprint: script bodies become node-cached
  `NodeScriptRunner` actions keyed by the contract id, subgraphs flatten
  recursively, and fallback contracts become repair branches. The
  scheduler's fallback substitution now gates dependents on the *entire*
  multi-step repair (all of a failed trigger's fallback targets), and a
  route only counts repaired when the whole repair verified.
- Node-granular script caching (build-order item 4):
  `runtime/script_node.py` adds `NodeScriptRunner`, an `ActionExecutor`
  (adapter `"script"`) that makes synthesized code a third node body kind
  inside DAG blueprints. Scripts memoize per node — cache key = node key +
  slot-binding fingerprint + environment fingerprint
  (`cache.NodeScriptSignature`), never the parent intent — so the same
  sub-task recurring across different workflows hits the same entry. Hits
  run the cached script straight on the backend (no gateway call); on a
  miss or environment drift only that node re-synthesizes, via
  `GraphEngineSynthesizer` driving the graph engine's full recalculating
  loop for the single node goal. Every synthesis is verified by executing
  through the runner's own backend before it is reported or cached, and a
  repaired script replaces the stale entry on verified success.

- `Blueprint` is a real partial order: `BlueprintEdge` (`before`/`fallback`
  relations, `sop`/`learned`/`data` provenance) plus an `ordering` mode —
  `sequential` (backward-compatible default) chains actions and layers
  explicit edges on top; `graph` runs unrelated actions in parallel.
- Added `orchestrator.scheduler.DagRouteRunner`: a readiness scheduler
  (drop-in `WorkflowExecutor`) with transitive failure cascade (no deadlocks),
  substitution-semantics fallback branches (a repaired failure keeps the
  route green and downstream nodes wait on the repair), per-action timeouts
  via the executor `cancel` hook, cycle/capability preflight, and optional
  per-run trace recording.
- Added `knowledge.traces.TraceStore`: private, SQLite-persisted execution
  statistics — per-node Beta success posteriors (context-bucketed), a
  precedence matrix that recovers a DAG from linear traces under a
  consistency threshold, and per-node cost EWMAs. Replaces sequence
  memorization; statistics accumulate across sessions with no training step.
- Added `orchestrator.adaptive`: `AdaptivePlanner` (blueprints rebuilt from
  the live `SkillRegistry` on every plan, learned edges promoted only with
  sufficient evidence, SOPs compiled in), `ThompsonRouteOptimizer` (route
  choice by sampling the user's own success posteriors, cost as tiebreak),
  `TraceFeedbackSink`, and `apply_sop_to_blueprint`.
- Added `skills.sop`: declarative YAML SOPs (`require_order`, `forbid`,
  `approval`, `require_verify`, `risk_budget`) compiled into hard edges,
  reserved actions, exclusions, and skill validators — human structure the
  learner can never overwrite.
- Generalizing compiler: `DemonstrationCompiler.compile_generalized` diffs
  repeated demonstrations into typed slots (varying values become
  parameters, identical variations unify, workspace paths are templated to
  `{workspace}`), `bind_parameters` rebinds them, and
  `SkillLearner.generalize` runs the same scrub -> compile -> verify ->
  register gate as exact learning.

HTTP gateway (`codex/http-gateway`).

- Added `workflow_gps.gateway`: a private, tenant-aware HTTP control-plane prototype
  as a transport-agnostic application over `Request`/`Response` (a WSGI/ASGI binding
  is the production seam), sitting on the durable runtime.
- Versioned REST surface (`/v1`) for runs/contracts, questions, routes, approvals,
  incidents, provider connections, and feedback, with a served OpenAPI document.
- OIDC bearer authentication, tenant-aware RBAC, per-tenant quotas and token-bucket
  rate limits, and request idempotency (duplicate submissions return one run).
- Asynchronous run submission (`202` + run id; progress via status, SSE event
  stream, or audit export) — never a long synchronous request.
- Verified, replay-protected webhooks (HMAC + timestamp tolerance + delivery-id
  dedupe), pagination, cancellation, security headers, and CORS; operational
  metrics endpoint.
- Added tests for multi-process and cross-tenant behaviour, restart, duplicate
  submission, rate-limit/quota, RBAC, the full clarification/confirmation/approval
  flow, and webhook replay.

Desktop shell (`codex/desktop-shell`).

- Added `workflow_gps.desktop`: the local single-user application service
  (`DesktopService`) a desktop UI binds to over a loopback boundary, with frozen,
  secret-free, serializable view-models.
- Task entry and guided-question views; route preview with cost and exclusion
  explanations; confirmation/approval/incident inboxes; workflow timeline,
  cancellation, recovery, and a verifiable audit view.
- Provider connection management over the credential vault (an OS-keychain vault is
  the production adapter); Docker/worker health with trusted-vs-untrusted execution
  labels; offline policy and local data export/deletion.
- Approvals are minted only from an authorized identity session, and the shell has
  no execution path, so the UI cannot bypass backend policy; no view ever carries a
  provider secret.
- Added tests proving a non-developer can complete, pause, resume, inspect, and
  recover a workflow through the service alone, and that the UI can neither bypass
  policy nor expose credentials.

Provider adapters (`codex/provider-adapters`).

- Added `workflow_gps.providers`: contract-tested provider integrations behind a
  credential vault boundary.
- Implemented a Google authorization-code/OIDC adapter (PKCE, scope mapping,
  callback validation, code exchange, refresh, revocation).
- Implemented OpenAI (with organization/project service-identity headers) and
  Anthropic (API-key and managed enterprise gateway) adapters.
- Added a shared request pipeline: capability discovery, a token-bucket rate
  limiter, spend budgets, request ids, idempotency keys (with replay caching),
  retries with classified errors, and HTTP-status → error classification.
- Kept credentials exclusively in the `SecretVault`; adapters hold references and
  mint auth headers only at call time, with redaction for logs.
- Added a single capability/revocation/idempotency/secret-leakage contract suite
  run against every adapter through an injected sandbox/remote-mock transport, plus
  per-provider flow, retry, rate-limit, budget, and service-identity tests.

Worker control plane (`codex/worker-control-plane`).

- Added `workflow_gps.worker`: a control plane that does planning and dispatch but
  holds **no execution backend and no credentials**, separated from workers that
  run code.
- Added signed, expiring, audience-bound, single-use worker task leases (HMAC),
  verified against a revocation/consumption ledger so lost (forged), duplicated
  (replayed), expired, or revoked leases cannot execute. The ledger has an
  in-memory and a durable SQLite implementation (single-use survives restarts).
- Added an isolation policy: untrusted synthesized code may run only on Docker (or
  a stronger restricted worker); the subprocess backend is restricted to explicitly
  trusted local skills. The worker enforces it before executing.
- Added worker health, capacity, cancellation (revokes the lease), wall-clock
  timeout, and failure-based quarantine.
- Added outbound-only local agents for desktop/private-network resources: they poll
  the control plane (no inbound port) and resolve local credentials themselves, so
  the control plane never receives them.
- Added tests proving the two exit-gate guarantees: the control plane never
  executes or holds credentials, and lost/duplicated/expired/revoked leases cannot
  execute.

Identity and RBAC (`codex/identity-rbac`).

- Added `workflow_gps.identity`: enforceable identity and authority replacing the
  simulation-only seams.
- Validate OIDC assertions against configured providers (issuer, audience, expiry,
  not-before; `alg: none` and algorithm-confusion rejected) behind a pluggable
  `SignatureVerifier` port — a stdlib HMAC verifier ships for local/test use; a
  JWKS-backed asymmetric verifier is the production adapter.
- Added tenant, organization, membership, group, role, and authority-grant records
  in a versioned, tenant-isolated SQLite store; every query is tenant-scoped.
- Derive reviewer/approver authority from stored grants and group roles, never from
  token text; `IdentityApprovalAuthority` mints an `ApprovalRecord` only from an
  authorized, verified session.
- Added service and device identities, server-issued sessions with expiry and
  revocation, and step-up authentication via authentication-assurance levels.
- Added policy tests for cross-tenant access, expired grants, self-approval,
  confused-deputy scope mismatch, step-up, and session expiry/revocation — proving
  no caller can self-verify an identity, self-assign a role, or reach another tenant.

Durable runtime (`codex/durable-runtime`).

- Added `workflow_gps.durable`: a restart-safe, multi-process workflow runtime
  behind deployment-neutral ports, with a versioned local SQLite adapter (the same
  table/lease/idempotency contract a PostgreSQL deployment implements).
- Added a durable task queue with leases, heartbeats, cancellation, retry with
  backoff, dead-lettering, and expired-lease reclaim; idempotent enqueue.
- Added an idempotency ledger so every externally visible mutation runs at most
  once, a transactional outbox (events/notifications staged in the same
  transaction as the state change) with an at-least-once relay, and a hash-linked,
  tamper-evident audit log that implements the `EventSink` port.
- Added durable run-state checkpoints and domain record stores (routes, accounts,
  approvals, incidents, semantic evidence, execution outcomes), content-addressed
  filesystem object storage for large artifacts, and backup/restore/retention/
  deletion workflows.
- Added `DurableWorkflowService` tying it together: a checkpoint and its
  announcement commit atomically; a crashed worker's task is reclaimed and
  re-driven from the last checkpoint without duplicating effects.
- Added tests proving restart loses or duplicates nothing (lease reclaim plus
  idempotent re-drive) and that approval/incident/audit records reconstruct the
  complete, verifiable execution history from storage alone.

Unified orchestrator (`codex/unified-orchestrator`). See ADR-0002.

- Added `workflow_gps.orchestrator`: one deterministic, resumable runtime that
  drives a workflow through intake, guided clarification, semantic grounding,
  route optimization, human-control evaluation, confirmation/approval waits,
  execution, outcome monitoring, automatic recovery or incident escalation, and
  finalization with route learning.
- Defined one versioned, serializable run state (`RunState`,
  `ORCHESTRATOR_SCHEMA_VERSION`) that round-trips losslessly; pause/resume and
  durability reduce to saving and reloading it.
- Added pause/resume for clarification, confirmation, approval, and incidents,
  with deployment-neutral ports and deterministic offline adapters that compose
  the existing skill core (Requirement and Constraint Compiler, the
  `ActionExecutor` contract, and `ExecutionOutcome`).
- Made execution safety a property of the state: the execution phase re-derives a
  hard preflight guard (requirements resolved, route not excluded, human control
  satisfied, capabilities available) on every attempt, including post-incident
  retries, so no path bypasses preflight controls.
- Added a versioned local run-state store (`workflow_runs`) through the shared
  migration runner, plus `wfgps workflow-list` / `wfgps workflow-status`.
- Added end-to-end tests for autonomous, confirmed, dual-approved, recovered, and
  escalated workflows, full serialization survival across every pause, durable
  store reopen, and preflight/capability bypass prevention.

## 0.2.0 - 2026-06-29

Stabilization baseline (`codex/stabilize-v0.2-baseline`).

- Reconciled the root and `src/` packaging into a single canonical `pyproject.toml`
  with the `wfgps` console entry point and `engine`/`docker`/`dev` extras; removed
  the duplicate `src/pyproject.toml`. There is now one supported install command:
  `pip install -e ".[engine]"`.
- Added a shared SQLite migration runner (`workflow_gps.persistence`) backed by
  `PRAGMA user_version`, and versioned every persisted schema (script cache,
  learned replies, local knowledge, crowd quarantine, skill catalog + idempotency
  ledger) through it, with a forward-compatibility guard against newer databases.
- Added forward/rollback migration tests, fresh-environment installation and CLI
  smoke tests, and a secret-hygiene test asserting no secrets reach persisted
  records, logs, fixtures, or examples.
- Configured Ruff and fixed repository-wide lint findings; formatted the tree.
- Documented experimental versus production-capable adapters in
  `docs/ADAPTER_MATURITY.md`.

Also included in this release candidate (previously unreleased):

- Added a model-free deterministic reply engine with context-gated templates.
- Added an official Telegram Bot API adapter for private text chats and a channel protocol for future LINE and other messaging adapters.
- Added local SQLite reply learning from manual Telegram Business replies, scoped per Business connection, with bot-loop prevention and short-lived pairing state.
- Added the portable skill-core foundation, ADR-0001, versioned domain records and ports, local/in-memory/remote-mock skill stores, safe skill inspection commands, and the Requirement and Constraint Compiler.
- Added an exact CLI demonstration compiler and safety-gated runtime with executable allow-lists, reduced environments, workspace fingerprints, write approvals, idempotency, timeouts, and artifact validation.

## 0.1.0 - 2026-06-28

- Stabilized the graph engine, execution contract, tier routing, self-healing dependency loop, and CLI.
- Added an opt-in local SQLite script cache that can skip synthesis for identical tasks.
- Added conservative cache signatures across prompt policy, routing models, backend configuration, package index, engine version, and schema version.
- Added cache outcome fields to graph state, workflow results, and JSON CLI output.
- Kept caching disabled by default and documented the release roadmap and security boundaries.
