import type {
  AutobuildView,
  AwaitingKind,
  FailureView,
  InboxItem,
  Listing,
  NoRouteView,
  PlanView,
  QuestionView,
  TaskView,
  TimelineEvent,
  WorkerHealth,
} from "./types";

// Dev: empty base → relative URLs → Vite proxies /v1 to the loopback backend.
// Packaged: the Tauri shell injects the API origin as window.__OOLU_API__ and
// whether it is a remote host (login required) as window.__OOLU_REMOTE__.
declare global {
  interface Window {
    __OOLU_API__?: string;
    __OOLU_REMOTE__?: boolean;
    __OOLU_ENGINE_TOKEN__?: string;
  }
}

const BASE = (): string => window.__OOLU_API__ ?? "";

// ---- the loopback engine's ephemeral token --------------------------------
// `oolu desktop` mints a fresh signing secret and local-user token on every
// launch and hands it over out-of-band: the URL hash (#auth=<token>, the
// same bootstrap the gateway's own frontend uses) or an injected global.
// It authenticates THIS app to THIS machine's engine — it is not an online
// account, so it lives in sessionStorage under its own key and never makes
// the header claim the user is signed in.
const ENGINE_TOKEN_KEY = "oolu_engine_token";

export function captureEngineToken(): void {
  const match = /[#&]auth=([^&]+)/.exec(location.hash ?? "");
  if (match) {
    sessionStorage.setItem(ENGINE_TOKEN_KEY, match[1]);
    // The token must not linger in the URL (history, screenshots).
    history.replaceState(null, "", location.pathname + location.search);
  } else if (window.__OOLU_ENGINE_TOKEN__) {
    sessionStorage.setItem(ENGINE_TOKEN_KEY, window.__OOLU_ENGINE_TOKEN__);
  }
}

export const engineToken = (): string | null =>
  sessionStorage.getItem(ENGINE_TOKEN_KEY);

// Remote (online) hosts require a real sign-in; the local loopback engine
// authorizes by OS ownership of the port, so it never shows a login screen.
export const isRemote = (): boolean => window.__OOLU_REMOTE__ === true;

const TOKEN_KEY = "oolu_token";
const PRINCIPAL_KEY = "oolu_principal";
const TENANT_KEY = "oolu_tenant";
const SERVER_KEY = "oolu_server";
// The private-network server a user signed into on Edge (a static address
// on their own network, e.g. http://192.168.1.20:8787) — remembered so the
// field comes prefilled next time. Separate from SERVER_KEY: Global and
// Edge-network are different doors and must not overwrite each other's
// remembered address.
const EDGE_SERVER_KEY = "oolu_edge_server";

// localStorage (not sessionStorage) so a signed-in host survives app restarts.
export const session = {
  get token(): string | null {
    return localStorage.getItem(TOKEN_KEY);
  },
  get principal(): string | null {
    return localStorage.getItem(PRINCIPAL_KEY);
  },
  // The online server a *local* build is signed into (a remote build's server
  // is baked in as __OOLU_API__). Remembered across sign-outs so the field
  // comes prefilled next time.
  get server(): string | null {
    return localStorage.getItem(SERVER_KEY);
  },
  get edgeServer(): string | null {
    return localStorage.getItem(EDGE_SERVER_KEY);
  },
  setEdgeServer(url: string): void {
    localStorage.setItem(EDGE_SERVER_KEY, url);
  },
  signedIn(): boolean {
    return localStorage.getItem(TOKEN_KEY) !== null;
  },
  set(token: string, principal: string, tenant: string): void {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(PRINCIPAL_KEY, principal);
    localStorage.setItem(TENANT_KEY, tenant);
  },
  setServer(url: string): void {
    localStorage.setItem(SERVER_KEY, url);
  },
  clear(): void {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(PRINCIPAL_KEY);
    localStorage.removeItem(TENANT_KEY);
  },
};

export const requiresLogin = (): boolean => isRemote() && !session.signedIn();

// The Global service: OoLu's own online server. A paired install
// (OOLU_SERVER_URL, e.g. a self-hosted server) overrides it; nobody is
// ever shown a raw host:port — the choice is simply Edge or Global.
export const DEFAULT_GLOBAL_SERVER = "https://ooludomaintobedetermined";

// What this install knows before any sign-in: the online server it pairs
// with (so the sign-in screen doesn't ask) and which doors the host offers.
export interface ClientConfig {
  server?: string | null;
  google?: boolean;
  registration?: boolean;
}

export async function clientConfig(): Promise<ClientConfig> {
  try {
    const res = await fetch(BASE() + "/v1/client-config");
    if (!res.ok) return {};
    const data = (await res.json().catch(() => ({}))) as ClientConfig;
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

interface LoginResponse {
  token: string;
  principal: string;
  tenant?: string;
}

// Where auth calls go: a remote build talks to its baked-in host; a local
// build signs into whichever online server the user entered (the engine and
// all its data stay on the loopback either way).
function authBase(server?: string): string {
  if (isRemote()) return BASE();
  const url = (server ?? session.server ?? "").trim().replace(/\/+$/, "");
  if (!url) throw new Error("enter the server to sign in to");
  session.setServer(url);
  return url;
}

async function authPost(
  path: string,
  body: unknown,
  fallback: string,
  notOffered = "this server does not offer registration yet",
): Promise<LoginResponse & { verification_required?: boolean }> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 404 || res.status === 405) {
    throw new Error(notOffered);
  }
  const data = (await res.json().catch(() => ({}))) as
    | LoginResponse
    | { error?: { message?: string } };
  if (!res.ok) {
    const message =
      (data as { error?: { message?: string } })?.error?.message ?? fallback;
    throw new Error(message);
  }
  return data as LoginResponse & { verification_required?: boolean };
}

export async function login(
  username: string,
  password: string,
  server?: string,
): Promise<void> {
  const res = await fetch(authBase(server) + "/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = (await res.json().catch(() => ({}))) as
    | LoginResponse
    | { error?: { message?: string } };
  if (!res.ok) {
    const message =
      (data as { error?: { message?: string } })?.error?.message ??
      "sign-in failed";
    throw new Error(message);
  }
  const ok = data as LoginResponse;
  session.set(ok.token, ok.principal, ok.tenant ?? "");
}

// Self-serve e-mail registration on the online server. The endpoint is the
// online host's to enable; hosts without it answer 404, surfaced as a plain
// "not offered" message rather than a raw status code.
export async function register(
  email: string,
  password: string,
  server?: string,
): Promise<{ verificationRequired: boolean }> {
  const ok = await authPost(
    authBase(server) + "/v1/auth/register",
    { email, password },
    "registration failed",
  );
  // A mail-verifying host answers without a token: the account exists but
  // stays locked until the e-mailed code comes back through verifyEmail.
  if (ok.verification_required) return { verificationRequired: true };
  session.set(ok.token, ok.principal, ok.tenant ?? "");
  return { verificationRequired: false };
}

// Finish a mail-verified registration: the e-mailed code plus the password
// chosen at registration turn into a signed-in session.
export async function verifyEmail(
  email: string,
  code: string,
  password: string,
  server?: string,
): Promise<void> {
  const ok = await authPost(
    authBase(server) + "/v1/auth/verify",
    { email, code, password },
    "verification failed",
    "this server does not offer e-mail verification yet",
  );
  session.set(ok.token, ok.principal, ok.tenant ?? "");
}

// Ask for a password-reset code. The server answers 202 whether or not the
// address exists — enumeration stays impossible on this side too.
export async function requestReset(
  email: string,
  server?: string,
): Promise<void> {
  await authPost(
    authBase(server) + "/v1/auth/reset/request",
    { email },
    "could not send the reset code",
    "this server does not offer password reset yet",
  );
}

export async function confirmReset(
  email: string,
  code: string,
  password: string,
  server?: string,
): Promise<void> {
  await authPost(
    authBase(server) + "/v1/auth/reset/confirm",
    { email, code, password },
    "password reset failed",
    "this server does not offer password reset yet",
  );
}

// Sign in with Google (RFC 8252): begin on the server, open the consent
// page in the system browser, then poll finish until the browser leg lands.
// The session token travels only on this channel — never through the browser.
export async function signInWithGoogle(
  server?: string,
  opts: {
    pollMs?: number;
    timeoutMs?: number;
    open?: (url: string) => void;
  } = {},
): Promise<void> {
  const base = authBase(server);
  const res = await fetch(base + "/v1/auth/google/start");
  const data = (await res.json().catch(() => ({}))) as {
    auth_url?: string;
    state?: string;
    error?: { message?: string };
  };
  if (res.status === 404) {
    throw new Error(
      data?.error?.message ?? "this server does not offer Google sign-in yet",
    );
  }
  if (!res.ok || !data.auth_url || !data.state) {
    throw new Error(data?.error?.message ?? "could not start Google sign-in");
  }
  (opts.open ?? ((url: string) => window.open(url, "_blank")))(data.auth_url);

  const deadline = Date.now() + (opts.timeoutMs ?? 120_000);
  while (Date.now() < deadline) {
    const fin = await fetch(base + "/v1/auth/google/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: data.state }),
    });
    const body = (await fin.json().catch(() => ({}))) as {
      status?: string;
      token?: string;
      principal?: string;
      tenant?: string;
      error?: { message?: string };
    };
    if (!fin.ok) {
      throw new Error(body?.error?.message ?? "Google sign-in failed");
    }
    if (body.status === "complete" && body.token && body.principal) {
      session.set(body.token, body.principal, body.tenant ?? "");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, opts.pollMs ?? 1500));
  }
  throw new Error("Google sign-in timed out — try again");
}

export function signOut(): void {
  session.clear();
  location.reload();
}

// The account console: commitments (the subscription) live on their own
// page. Local mode opens this gateway's console with the engine token
// handed over in the hash (same bootstrap the shell itself uses); a
// signed-in remote session opens the server's console with its token.
export function accountConsoleUrl(): string {
  if (isRemote() || session.signedIn()) {
    const base = isRemote() ? BASE() : (session.server ?? "");
    return `${base}/account#auth=${session.token ?? ""}`;
  }
  return `${BASE()}/account#auth=${engineToken() ?? ""}`;
}

// Which credential authenticates API calls: the loopback engine's own
// ephemeral token in local mode, the online session everywhere else.
function apiToken(): string | null {
  if (!isRemote()) return engineToken();
  return session.token;
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = apiToken();
  if (token) headers["Authorization"] = "Bearer " + token;

  const res = await fetch(BASE() + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && token) {
    if (!isRemote()) {
      // A stale engine token (the sidecar restarted): drop it and reload
      // so the shell re-bootstraps. The online account is not at fault.
      sessionStorage.removeItem(ENGINE_TOKEN_KEY);
      location.reload();
    } else {
      // The token expired or was revoked: drop it and return to sign-in.
      signOut();
    }
    throw new Error("signed out");
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw new Error(data?.error?.message ?? data?.error ?? `${res.status}`);
  }
  return data as T;
}

// The gateway spreads a run across several endpoints: the base view-model
// (`GET /v1/runs/{id}`) plus per-pause detail (/questions, /route, /incidents).
// The desktop components want one flat `TaskView`, so this layer composes them.
interface RunDict {
  run_id: string;
  intent: string;
  phase: string;
  awaiting: string | null;
  prompt: string | null;
  failure_reason: string | null;
  result: Record<string, unknown> | null;
  user_retries?: number;
  plan?: PlanView | null;
  no_route?: NoRouteView | null;
  failure?: FailureView | null;
  autobuild?: AutobuildView | null;
}

export const TERMINAL_PHASES = ["completed", "failed", "cancelled"];

async function composeTask(run: RunDict): Promise<TaskView> {
  let questions: QuestionView[] = [];
  let prompt = run.prompt;

  // Enrich the pause with whatever detail its actioning screen needs. Detail
  // fetches are best-effort: a failure still yields a usable base view.
  if (run.awaiting === "clarification") {
    const data = await req<{ questions: QuestionView[] }>(
      "GET",
      `/v1/runs/${run.run_id}/questions`,
    ).catch(() => ({ questions: [] as QuestionView[] }));
    questions = data.questions ?? [];
  } else if (run.awaiting === "incident" && !prompt) {
    const data = await req<{ incidents: { reason: string }[] }>(
      "GET",
      `/v1/runs/${run.run_id}/incidents`,
    ).catch(() => ({ incidents: [] as { reason: string }[] }));
    prompt = data.incidents[0]?.reason ?? "An incident needs a decision.";
  } else if (run.awaiting === "confirmation" && !prompt) {
    const route = await req<{ chosen?: string; total_cost?: number }>(
      "GET",
      `/v1/runs/${run.run_id}/route`,
    ).catch(() => null);
    prompt = route?.chosen
      ? `Confirm route "${route.chosen}".`
      : "Confirm the planned route.";
  }

  return {
    run_id: run.run_id,
    intent: run.intent,
    phase: run.phase,
    awaiting: run.awaiting,
    prompt,
    questions,
    can_cancel: !TERMINAL_PHASES.includes(run.phase),
    failure_reason: run.failure_reason,
    result: run.result,
    user_retries: run.user_retries ?? 0,
    plan: run.plan ?? null,
    no_route: run.no_route ?? null,
    failure: run.failure ?? null,
    autobuild: run.autobuild ?? null,
  };
}

async function mutateRun(
  method: string,
  path: string,
  body?: unknown,
): Promise<TaskView> {
  // Every run mutation endpoint returns the fresh run dict, so compose from it.
  return composeTask(await req<RunDict>(method, path, body));
}

// A tool the assistant used during the turn — shown as a chip so the user
// can verify what was touched.
export interface ChatAction {
  tool: string;
  name?: string;
}

export interface ChatTurnReply {
  reply: string;
  source: string;
  actions?: ChatAction[];
  // OoLu asking for one of THIS device's senses: "location" | "camera" |
  // "file". The client renders a grant button — the user decides.
  device?: string | null;
  run_id: string | null;
}

// A turn of the account's own OoLu thread, as the server remembers it —
// what a fresh device loads so every client shows the same conversation.
export interface ChatHistoryTurn {
  seq: number;
  kind: "user" | "assistant" | "run";
  body: string;
  at: string;
}

// ---- friends wire shapes ----------------------------------------------------
// A conversation in the peer list: who, what was said last, what waits.
export interface FriendConversation {
  peer: string;
  last_text: string;
  last_from: string;
  last_at: string;
  unread: number;
}

export interface FriendMessage {
  message_id: string;
  from: string;
  text: string;
  file_id: string | null;
  at: string;
  mine: boolean;
  read: boolean;
}

// ---- payments wire shapes --------------------------------------------------
export interface SavedCard {
  pm_ref: string;
  brand: string;
  last4: string;
  exp_month: number;
  exp_year: number;
}

export interface PaymentProfileView {
  mode: string; // "test" pre-launch, "live" later
  default_pm: string | null;
  cards: SavedCard[];
}

export interface PaymentsStatus {
  open: boolean;
  mode: string;
  vault_mode: string;
  reasons: string[];
}

// ---- settings node wire shape (GET /v1/settings) --------------------------
export interface SettingItem {
  key: string;
  group: string;
  label: string;
  kind: "bool" | "number" | "choice" | "text";
  description: string;
  value: unknown;
  minimum?: number | null;
  maximum?: number | null;
  choices?: string[] | null;
  max_length?: number | null;
  // Display-only here: owned by a dedicated flow (the account console).
  managed?: boolean;
  // What a number means; for money fields the gateway resolves this to
  // the tenant's regional currency code (e.g. "EUR").
  unit?: string | null;
}

// ---- model key wire shape (GET /v1/keys/model) -----------------------------
// Fingerprints only — the key itself never comes back from the server.
export interface ModelKeyView {
  provider: string;
  fingerprint: string;
  added_at: string;
}

// ---- user file wire shapes (GET /v1/files et al.) -------------------------
export interface FileMeta {
  file_id: string;
  node_id?: string | null;
  name: string;
  // Where the file sits inside its drawer: a '/'-separated folder path,
  // "" = the drawer's root.
  folder?: string;
  media_type: string;
  size: number;
  created_at: string;
  updated_at: string;
}

export interface FileDoc extends FileMeta {
  content: string;
}

// ---- Work environment wire shapes (GET /v1/work/nodes et al.) ------------
export interface NodeAccountView {
  node_id: string;
  responsible: string;
  admin: string | null;
  // Authority exists only under a Supernode; standalone nodes carry null.
  authority_level: number | null;
  is_supernode: boolean;
  supernode_id: string | null;
  status: string;
  audit_mode: boolean;
  allow_autodev_data: boolean;
}

// The regime fixed at creation — audit, auto-growing, supernode-ness and
// membership can never be changed afterwards.
export interface NodeAccountCreate {
  is_supernode: boolean;
  supernode_id: string | null;
  audit_mode: boolean;
  allow_autodev_data: boolean;
  authority_level: number | null;
  // The Node Policy, agreed UPFRONT: it authorizes clone/fraud/zombie
  // detection with restriction or removal. The server refuses creation
  // without it.
  accept_policy: boolean;
}

// The mutable slice after creation — authority, like the rest of the
// regime, is fixed for everyone, the Supernode's humans included.
export interface NodeAccountPatch {
  admin: string;
  status: string;
}

export interface WorkNode {
  node_id: string;
  title: string;
  status: string;
  account: NodeAccountView;
  earnings_micros: number;
  health: {
    verified_successes: number;
    verified_failures: number;
    score: number | null;
  };
}

export interface NodeRunSteps {
  run_id: string;
  gross: number;
  // The node that EXECUTED this run — a Supernode's feed aggregates its
  // members', each tagged with the executing node's name.
  node_title?: string;
  steps: { seq: number; event_type: string; at: string }[];
}

export interface HoldItem {
  pending_id: string;
  name: string;
  reserved: string[];
  submitted_by: string | null;
  created_at: string;
  expires_at: string | null;
  replies: { author: string; message: string; at: string }[];
}

// Supernode KYC: a verified legal entity earns a global trust multiplier.
export interface KycApplication {
  node_id: string;
  legal_name: string;
  company_email: string;
  registration_no: string;
  screen: "fast_track" | "standard";
  screen_note: string;
  status: "pending_review" | "verified" | "rejected";
  decision_note: string;
  multiplier: number;
}

export interface KycView {
  application: KycApplication | null;
  trust_multiplier: number;
  // KYC binds only on the Global service; edge installs answer false and
  // the UI shows no KYC block at all.
  required?: boolean;
}

export const api = {
  // One turn with the assistant. The conversation is client-held, so the
  // recent history rides along; a work turn comes back with the run id the
  // chat folds into the thread.
  chat: (
    message: string,
    history: { role: "user" | "assistant"; content: string }[],
    nodeId?: string,
    mood?: string,
  ) =>
    req<ChatTurnReply>("POST", "/v1/chat", {
      message,
      history,
      ...(nodeId ? { node_id: nodeId } : {}),
      ...(mood ? { mood } : {}),
    }),
  // The server-side OoLu thread: what a fresh device loads. Hosts that
  // don't keep history answer 404 — callers fall back to local storage.
  chatHistory: () =>
    req<{ items: ChatHistoryTurn[] }>("GET", "/v1/chat/history"),
  // The data-subject's rights, self-serve: everything as one JSON
  // document, and erasure that says exactly what it removed.
  exportAccount: () => req<Record<string, unknown>>("GET", "/v1/account/export"),
  deleteAccount: (password: string) =>
    req<{ account: string; erased: Record<string, number>; notes: string[] }>(
      "POST",
      "/v1/account/delete",
      { password },
    ),
  // Friends: the peer list, exact-lookup (username or e-mail — never a
  // directory), one thread per person (opening it marks it read), send.
  friends: () => req<{ items: FriendConversation[] }>("GET", "/v1/friends"),
  friendLookup: (query: string) =>
    req<{ username: string }>("POST", "/v1/friends/lookup", { query }),
  friendMessages: (peer: string) =>
    req<{ peer: string; items: FriendMessage[] }>(
      "GET",
      `/v1/friends/${encodeURIComponent(peer)}/messages`,
    ),
  sendFriendMessage: (peer: string, text: string, fileId?: string) =>
    req<FriendMessage>(
      "POST",
      `/v1/friends/${encodeURIComponent(peer)}/messages`,
      { text, ...(fileId ? { file_id: fileId } : {}) },
    ),
  submitTask: (intent: string) => mutateRun("POST", "/v1/runs", { intent }),
  task: async (id: string) =>
    composeTask(await req<RunDict>("GET", `/v1/runs/${id}`)),
  answer: (id: string, answers: Record<string, unknown>) =>
    mutateRun("POST", `/v1/runs/${id}/answers`, { answers }),
  confirm: (id: string, approved: boolean) =>
    mutateRun("POST", `/v1/runs/${id}/confirmation`, { approved }),
  resolveIncident: (id: string, decision: string) =>
    mutateRun("POST", `/v1/runs/${id}/incidents`, { decision }),
  cancel: (id: string) => mutateRun("POST", `/v1/runs/${id}/cancel`, {}),
  timeline: async (id: string) => {
    const audit = await req<{
      entries: { at: string; event_type: string; seq: number; detail?: string }[];
    }>("GET", `/v1/runs/${id}/audit`);
    return {
      items: (audit.entries ?? []).map<TimelineEvent>((e) => ({
        at: e.at,
        label: e.event_type,
        detail: e.detail ?? "",
      })),
    };
  },
  // No /v1/inbox on the gateway: derive it from the run list. Any run waiting
  // on a human (`awaiting` set) is something the operator must act on.
  inbox: async () => {
    const data = await req<{ items: (RunDict & { awaiting: string })[] }>(
      "GET",
      "/v1/runs",
    );
    const items = (data.items ?? [])
      .filter((r) => r.awaiting)
      .map<InboxItem>((r) => ({
        run_id: r.run_id,
        kind: r.awaiting as AwaitingKind,
        intent: r.intent,
        prompt: r.prompt ?? `Needs ${r.awaiting}.`,
      }));
    return { items };
  },
  // The Noder conversation list: every node interaction (run), newest
  // first, regardless of whether it needs a human right now.
  runs: async () => {
    const data = await req<{ items: RunDict[] }>("GET", "/v1/runs");
    const items = (data.items ?? []).map((r) => ({
      run_id: r.run_id,
      intent: r.intent,
      phase: r.phase,
      awaiting: r.awaiting,
    }));
    return { items: items.reverse() };
  },
  // ---- payments: card on file (pre-launch: test vault, port closed) ----
  paymentMethods: () => req<PaymentProfileView>("GET", "/v1/payment-methods"),
  addTestCard: (brand: string) =>
    req<{ pm_ref: string }>("POST", "/v1/payment-methods", { brand }),
  removeCard: (pmRef: string) =>
    req<{ removed: boolean }>("DELETE", `/v1/payment-methods/${pmRef}`),
  setDefaultCard: (pmRef: string) =>
    req<{ default_pm: string }>("POST", `/v1/payment-methods/${pmRef}/default`),
  paymentsStatus: () =>
    req<PaymentsStatus>("GET", "/v1/payments/status"),
  // ---- the settings node: bounded, declared configuration -------------
  settings: () => req<{ items: SettingItem[] }>("GET", "/v1/settings"),
  setSettings: (changes: Record<string, unknown>) =>
    req<{ items: SettingItem[] }>("PUT", "/v1/settings", { changes }),
  // ---- model keys: the BYO-key door. A key goes in once; only its
  // fingerprint ever comes back.
  modelKeys: () => req<{ items: ModelKeyView[] }>("GET", "/v1/keys/model"),
  addModelKey: (provider: string, key: string) =>
    req<{ provider: string; fingerprint: string; source_switched?: boolean }>(
      "POST",
      "/v1/keys/model",
      { provider, key },
    ),
  // The definitive "is my key working?" check: one real model call.
  testModelKey: () =>
    req<{ ok: boolean; reply?: string; source?: string; error?: string }>(
      "POST",
      "/v1/keys/model/test",
      {},
    ),
  removeModelKey: (provider: string) =>
    req<{ removed: string }>("DELETE", `/v1/keys/model/${provider}`),
  // ---- user files: documents and sheets in the durable database --------
  // No nodeId = the Life drawer; a nodeId = that node's own files in Work.
  files: (nodeId?: string) =>
    req<{ items: FileMeta[] }>(
      "GET",
      nodeId ? `/v1/files?node_id=${encodeURIComponent(nodeId)}` : "/v1/files",
    ),
  file: (id: string) => req<FileDoc>("GET", `/v1/files/${id}`),
  createFile: (
    name: string,
    content = "",
    nodeId?: string,
    folder = "",
    mediaType?: string,
  ) =>
    req<FileDoc>("POST", "/v1/files", {
      name,
      content,
      ...(nodeId ? { node_id: nodeId } : {}),
      ...(folder ? { folder } : {}),
      ...(mediaType ? { media_type: mediaType } : {}),
    }),
  saveFile: (
    id: string,
    patch: { name?: string; content?: string; folder?: string },
  ) => req<FileDoc>("PUT", `/v1/files/${id}`, patch),
  deleteFile: (id: string) =>
    req<{ deleted: boolean }>("DELETE", `/v1/files/${id}`),
  // ---- the Work environment: node accounts and stewardship -------------
  workNodes: () => req<{ items: WorkNode[] }>("GET", "/v1/work/nodes"),
  workAccountCreate: (nodeId: string, fixed: NodeAccountCreate) =>
    req<NodeAccountView>("POST", `/v1/work/nodes/${nodeId}/account`, fixed),
  workOnboard: (nodeId: string) =>
    req<NodeAccountView>("POST", `/v1/work/nodes/${nodeId}/account`, {
      onboard: true,
    }),
  workAccount: (nodeId: string, patch: Partial<NodeAccountPatch>) =>
    req<NodeAccountView>("POST", `/v1/work/nodes/${nodeId}/account`, patch),
  workActivity: (nodeId: string) =>
    req<{ items: NodeRunSteps[] }>("GET", `/v1/work/nodes/${nodeId}/activity`),
  // Supernode KYC: status + apply (a reviewer decides platform-side).
  kycStatus: (nodeId: string) =>
    req<KycView>("GET", `/v1/work/nodes/${nodeId}/kyc`),
  kycApply: (
    nodeId: string,
    body: { legal_name: string; company_email: string; registration_no?: string },
  ) => req<KycApplication>("POST", `/v1/work/nodes/${nodeId}/kyc`, body),
  // The reviewer's inbox (permission-gated: 403 for everyone else) and
  // the human verdict on an application.
  kycReviews: () =>
    req<{ items: KycApplication[] }>("GET", "/v1/kyc/reviews"),
  kycDecide: (nodeId: string, approved: boolean, note?: string) =>
    req<KycApplication>("POST", `/v1/work/nodes/${nodeId}/kyc/decide`, {
      approved,
      ...(note ? { note } : {}),
    }),
  // Manual-commit queue: held contract runs (audit nodes land here).
  holds: () => req<{ items: HoldItem[] }>("GET", "/v1/runs/contract/holds"),
  decideHold: (pendingId: string, approved: boolean, signature?: string) =>
    req<unknown>("POST", `/v1/runs/contract/holds/${pendingId}`, {
      approved,
      ...(signature ? { signature } : {}),
    }),
  holdReply: (pendingId: string, message: string) =>
    req<{ replies: HoldItem["replies"] }>(
      "POST",
      `/v1/runs/contract/holds/${pendingId}/reply`,
      { message },
    ),
  // Create a node as admin: contribute a draft (its first real version
  // arrives from work or a manual publish), then shape its account.
  // Create a node. With a function uploaded (a developer's own code,
  // written outside OoLu), the node is born WITH it: a script action the
  // sandbox runs and verifies, screened before it is ever stored. Without
  // one, an empty draft whose function arrives from work or a later push.
  createNode: (
    title: string,
    summary: string,
    functionScript?: string,
    io?: { inputs?: { name: string; type: string }[]; outputs?: { name: string; type: string }[] },
  ) =>
    req<{ node_id: string }>("POST", "/v1/nodeplace", {
      skill: {
        name: title,
        description: summary || title,
        signature: functionScript
          ? { application: "script", adapter: "script" }
          : { application: "cli", adapter: "cli" },
        parameters: (io?.inputs ?? []).map((i) => ({
          name: i.name,
          value_type: i.type,
          required: true,
        })),
        actions: functionScript
          ? [
              {
                correlation_id: "function",
                adapter: "script",
                operation: "run",
                parameters: {
                  goal: summary || title,
                  script: functionScript,
                  node_key: `upload:${title}`,
                },
              },
            ]
          : [{ correlation_id: "draft", adapter: "cli", operation: "run" }],
      },
      ...(io?.inputs?.length
        ? {
            consumes: io.inputs.map((i) => ({
              name: i.name,
              value_type: i.type,
              role: "input",
            })),
          }
        : {}),
      ...(io?.outputs?.length
        ? {
            produces: io.outputs.map((o) => ({
              name: o.name,
              value_type: o.type,
              role: "result",
            })),
          }
        : {}),
      title,
      summary: summary || title,
      visibility: "public",
    }),
  // The desktop "Skills" tab browses the marketplace's published listings —
  // the gateway's discovery surface.
  skills: (q?: string) =>
    req<{ items: Listing[] }>(
      "GET",
      q ? `/v1/listings?q=${encodeURIComponent(q)}` : "/v1/listings",
    ),
  workerHealth: () => req<WorkerHealth>("GET", "/v1/worker-health"),
};

export function timelineSocket(
  runId: string,
  onEvent: (e: TimelineEvent) => void,
): WebSocket {
  const base = BASE();
  const origin = base
    ? base.replace(/^http/, "ws")
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  const url = `${origin}/v1/runs/${runId}/events`;
  // The gateway accepts a bearer token over WS via the ["bearer", <token>]
  // subprotocol (browsers cannot set Authorization on a WebSocket handshake).
  const token = apiToken();
  const ws = token ? new WebSocket(url, ["bearer", token]) : new WebSocket(url);
  ws.onmessage = (m) => {
    try {
      // Frames are audit-derived: { seq, event_type, phase, at, detail }.
      const f = JSON.parse(m.data) as {
        at?: string;
        event_type?: string;
        phase?: string;
        detail?: string;
      };
      onEvent({
        at: f.at ?? new Date().toISOString(),
        label: f.event_type ?? "event",
        detail: f.detail || (f.phase ?? ""),
      });
    } catch {
      /* ignore malformed frame */
    }
  };
  return ws;
}
