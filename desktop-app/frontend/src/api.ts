import type {
  AwaitingKind,
  InboxItem,
  Listing,
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

async function authPost(path: string, body: unknown, fallback: string): Promise<LoginResponse> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 404 || res.status === 405) {
    throw new Error("this server does not offer registration yet");
  }
  const data = (await res.json().catch(() => ({}))) as
    | LoginResponse
    | { error?: { message?: string } };
  if (!res.ok) {
    const message =
      (data as { error?: { message?: string } })?.error?.message ?? fallback;
    throw new Error(message);
  }
  return data as LoginResponse;
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
): Promise<void> {
  const ok = await authPost(
    authBase(server) + "/v1/auth/register",
    { email, password },
    "registration failed",
  );
  session.set(ok.token, ok.principal, ok.tenant ?? "");
}

export function signOut(): void {
  session.clear();
  location.reload();
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
  run_id: string | null;
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
}

// ---- user file wire shapes (GET /v1/files et al.) -------------------------
export interface FileMeta {
  file_id: string;
  node_id?: string | null;
  name: string;
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
  authority_level: number;
  status: string;
  audit_mode: boolean;
  allow_autodev_data: boolean;
}

export interface NodeAccountPatch {
  admin: string;
  authority_level: number;
  status: string;
  audit_mode: boolean;
  allow_autodev_data: boolean;
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
  steps: { seq: number; event_type: string; at: string }[];
}

export interface HoldItem {
  pending_id: string;
  name: string;
  reserved: string[];
  submitted_by: string | null;
  created_at: string;
  expires_at: string | null;
}

export const api = {
  // One turn with the assistant. The conversation is client-held, so the
  // recent history rides along; a work turn comes back with the run id the
  // chat folds into the thread.
  chat: (
    message: string,
    history: { role: "user" | "assistant"; content: string }[],
  ) => req<ChatTurnReply>("POST", "/v1/chat", { message, history }),
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
      entries: { at: string; event_type: string; seq: number }[];
    }>("GET", `/v1/runs/${id}/audit`);
    return {
      items: (audit.entries ?? []).map<TimelineEvent>((e) => ({
        at: e.at,
        label: e.event_type,
        detail: "",
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
  // ---- user files: documents and sheets in the durable database --------
  // No nodeId = the Life drawer; a nodeId = that node's own files in Work.
  files: (nodeId?: string) =>
    req<{ items: FileMeta[] }>(
      "GET",
      nodeId ? `/v1/files?node_id=${encodeURIComponent(nodeId)}` : "/v1/files",
    ),
  file: (id: string) => req<FileDoc>("GET", `/v1/files/${id}`),
  createFile: (name: string, content = "", nodeId?: string) =>
    req<FileDoc>("POST", "/v1/files", {
      name,
      content,
      ...(nodeId ? { node_id: nodeId } : {}),
    }),
  saveFile: (id: string, patch: { name?: string; content?: string }) =>
    req<FileDoc>("PUT", `/v1/files/${id}`, patch),
  deleteFile: (id: string) =>
    req<{ deleted: boolean }>("DELETE", `/v1/files/${id}`),
  // ---- the Work environment: node accounts and stewardship -------------
  workNodes: () => req<{ items: WorkNode[] }>("GET", "/v1/work/nodes"),
  workAccount: (nodeId: string, patch: Partial<NodeAccountPatch>) =>
    req<NodeAccountView>("POST", `/v1/work/nodes/${nodeId}/account`, patch),
  workActivity: (nodeId: string) =>
    req<{ items: NodeRunSteps[] }>("GET", `/v1/work/nodes/${nodeId}/activity`),
  // Manual-commit queue: held contract runs (audit nodes land here).
  holds: () => req<{ items: HoldItem[] }>("GET", "/v1/runs/contract/holds"),
  decideHold: (pendingId: string, approved: boolean) =>
    req<unknown>("POST", `/v1/runs/contract/holds/${pendingId}`, { approved }),
  // Create a node as admin: contribute a draft (its first real version
  // arrives from work or a manual publish), then shape its account.
  createNode: (title: string, summary: string) =>
    req<{ node_id: string }>("POST", "/v1/nodeplace", {
      skill: {
        name: title,
        description: summary || title,
        signature: { application: "cli", adapter: "cli" },
        actions: [{ correlation_id: "draft", adapter: "cli", operation: "run" }],
      },
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
      // Frames are audit-derived: { seq, event_type, phase, at }.
      const f = JSON.parse(m.data) as {
        at?: string;
        event_type?: string;
        phase?: string;
      };
      onEvent({
        at: f.at ?? new Date().toISOString(),
        label: f.event_type ?? "event",
        detail: f.phase ?? "",
      });
    } catch {
      /* ignore malformed frame */
    }
  };
  return ws;
}
