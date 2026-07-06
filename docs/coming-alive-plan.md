# Coming alive: wiring OoLu to the real world

A working plan for the next builds. Today the product is a complete body
with reflexes — every organ works, but the mind is a rule table and the
door to the outside world is closed. This document reviews what already
exists in the code, then lays out the milestones that make the app alive,
in the order that pays off fastest.

## Where we stand (code review)

The surprising finding of the review: most of the "hard parts" of coming
alive are already built and tested — they are just not connected.

| Capability | State | Where |
|---|---|---|
| Chat model port | **Built, unwired.** `ChatModel` protocol takes OpenAI-style messages, returns text; `GatewayApp` constructs `ChatAssistant()` with no model, so every install runs rules + intent fallback. | `src/oolu/chat.py` (`ChatModel`), `src/oolu/gateway/app.py` |
| LLM provider adapters | **Built.** `AnthropicAdapter` and `OpenAiAdapter` with a `.chat()` method, retries and transport owned by the adapter, secrets held as `CredentialRef`s. | `src/oolu/providers/apikey.py` |
| Secret storage | **Built, in-memory only.** `SecretVault` never prints or serializes a secret, but it forgets on restart — there is no durable, encrypted keyring yet. | `src/oolu/providers/vault.py` |
| Model cost metering | **Built.** `ModelCallMeter` + `ModelPriceTable` classify calls into price tiers and total costs per purpose. | `src/oolu/billing/model_calls.py` |
| Google OAuth machinery | **Built for provider capabilities, reusable for sign-in.** `GoogleOAuthAdapter` does PKCE, authorization URL, code exchange, refresh, revoke. | `src/oolu/providers/google.py` |
| OIDC token validation | **Built.** `OidcValidator` + `ProviderConfig` + `JwksVerifier` verify third-party id_tokens against JWKS — exactly what "Sign in with Google" needs. | `src/oolu/identity/tokens.py`, `jwks.py` |
| Local accounts | **Built and live.** Password hashing, lockout, sessions; the desktop banner token flow works today. | `src/oolu/identity/accounts.py`, `sessions.py` |
| Sign-in UI | **Stubbed.** "Continue with Google" / "Continue with phone" are disabled buttons labeled "Coming soon". | `desktop-app/frontend/src/components/Login.tsx` |
| Payments | **Built, gated.** Card saving via SetupIntent, `LaunchGuard` keeps the real transaction port closed until prices settle and functions verify. | `src/oolu/billing/cards.py`, `launch.py` |
| Public API | **Built.** Keys, scopes, signed webhooks. Needs a public host to matter. | `src/oolu/identity/apikeys.py`, `gateway/notify.py` |
| Budget limits | **Built.** The settings node carries budget fields OoLu can read but not exceed. | `src/oolu/settings_node.py` |

## Milestone A — a real mind (wire an LLM into chat)

This is the highest-leverage day of work in the repo: one adapter class
turns every conversation from rule-matching into real understanding, and
the tool loop, function words, run cards, and avatar are already waiting
for it.

1. **Bridge adapter.** A small `ProviderChatModel` implementing
   `ChatModel.reply(messages)` on top of `AnthropicAdapter.chat()` /
   `OpenAiAdapter.chat()`. Lives in `src/oolu/providers/`, ~50 lines.
   The chat system prompt already teaches the JSON `{"say","task"}` +
   tool-call contract, and `_parse_model_turn` already degrades gracefully
   when a model answers in plain prose.
2. **Key intake, not in settings.** The settings catalog is deliberately
   visible data — API keys must never appear there. Add a
   `POST /v1/keys/model` route that stores the key in the vault and
   returns only a fingerprint; the settings node gets non-secret fields
   (`model.provider`, `model.tier`) so OoLu can *choose* a brain but never
   *read* the key.
3. **Durable keyring.** Give `SecretVault` an encrypted-at-rest backing
   (file keyed by a machine secret now; the OS keychain via Tauri later)
   so a developer's key survives restart. Redaction and `repr` guarantees
   stay as they are.
4. **Model router (the enterprise story, v0).** A priority list with
   fallback: try the configured primary, fall back to the next on
   transport failure, always fall back to the existing model-less intent
   path so chat never dies. Every call goes through `ModelCallMeter`;
   when the metered total crosses the settings-node budget, the router
   refuses politely and says so in chat.
5. **BYO key (developer/local mode).** A field in Settings → paste key →
   stored in the local vault, never synced. This honors the Issue-1
   promise: developers can stay fully local.
6. **The avatar gets an honest source.** Extend the model reply contract
   with an optional `"mood"` field; when present it replaces the word-list
   heuristics in `desktop-app/frontend/src/avatar.ts` (which remain the
   offline fallback, as designed).

**Done when:** with a pasted Anthropic or OpenAI key, a chat turn is
answered by the real model, tools work in the loop, the cost shows in the
meter, pulling the network cable degrades to the intent path, and no test
or log ever contains the key.

**Quinn's part:** pick the launch providers and have an API key ready for
live verification.

## Milestone B — real people (Google sign-in, email, phone)

1. **Google sign-in, desktop-grade.** Use the RFC 8252 native-app flow:
   the Tauri shell opens the system browser to Google's consent page
   (PKCE via the existing `GoogleOAuthAdapter` machinery, no client
   secret needed for a desktop OAuth client), Google redirects to a
   loopback port the local gateway listens on, and the returned id_token
   is verified by the existing `OidcValidator` against Google's JWKS.
2. **Identity linking, not replacement.** A verified Google identity
   attaches to a `UserAccount` (new `identities` table: provider,
   subject, email). Crucially, a local-mode user can attach a Google
   identity *later* without losing anything — learned paths and skills
   stay in the local DB, exactly as promised in the sign-in screen.
3. **Enable the button.** `Login.tsx`'s "Continue with Google" stops being
   disabled; it calls the gateway to start the flow and polls for
   completion.
4. **Email registration** needs an outbound mail sender for verification
   codes; wire the code path now, choose the sender (SES/Resend/Postmark)
   when the online host exists.
5. **Phone** stays "Coming soon" until an SMS provider is chosen — it is
   the least valuable of the three and the only one with per-message
   costs.

**Done when:** a fresh install can create an account with Google alone,
sign out, sign back in with Google, and still see its local data; the
id_token verification is covered by tests using a fake JWKS.

**Quinn's part:** create a Google Cloud project and a **Desktop app**
OAuth client ID (no server required for this flow) and drop the client id
into config.

## Milestone C — going online (the public host)

Everything above works on one machine. The public host unlocks Friends,
webhooks, the public API, and web-based OAuth — but it is deliberately
*after* A and B because it is operations, not product.

1. Domain + a small VM (or managed container) + TLS.
2. Postgres behind `DurableConnection` (the storage layer already speaks
   both SQLite and Postgres).
3. `OOLU_SERVER_URL` in the Tauri config + CSP update, so the desktop app
   can pair local execution with a remote account.
4. This unblocks: Stripe webhooks (Milestone D), public-API webhook
   deliveries to third parties, email verification, and the Friends pane.

**Quinn's part:** pick and buy the domain; choose a cloud provider.

## Milestone D — money and senses (after A–C)

- **Stripe live keys** go in only when `LaunchGuard` says so — the
  transaction-port switch, the price-settlement window, and the minimum
  verified-success count are already enforced in code; this milestone is
  just configuration plus a real SetupIntent smoke test.
- **Native wake word** ("OoLu", exactly) as a Tauri-side listener, so the
  mic doesn't have to stay open in the webview.
- **Audio analyser** feeding real voice volume into
  `updateAvatarSignals` — the avatar's agitation is currently proxied by
  listening/speaking booleans; a WebAudio `AnalyserNode` makes it track
  the actual waveform.

## Suggested order for tomorrow

Do **Milestone A end-to-end** (steps 1–5; step 6 is optional polish). It
is one focused day, needs nothing bought or provisioned, and every demo
after it feels categorically different because OoLu actually understands.
If time remains, start B at step 1 — the OAuth machinery is already
tested, so the work is mostly the loopback listener and the `identities`
table.
