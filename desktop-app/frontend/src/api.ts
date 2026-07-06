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
  }
}

const BASE = (): string => window.__OOLU_API__ ?? "";

// Remote (online) hosts require a real sign-in; the local loopback engine
// authorizes by OS ownership of the port, so it never shows a login screen.
export const isRemote = (): boolean => window.__OOLU_REMOTE__ === true;

const TOKEN_KEY = "oolu_token";
const PRINCIPAL_KEY = "oolu_principal";
const TENANT_KEY = "oolu_tenant";

// localStorage (not sessionStorage) so a signed-in host survives app restarts.
export const session = {
  get token(): string | null {
    return localStorage.getItem(TOKEN_KEY);
  },
  get principal(): string | null {
    return localStorage.getItem(PRINCIPAL_KEY);
  },
  signedIn(): boolean {
    return localStorage.getItem(TOKEN_KEY) !== null;
  },
  set(token: string, principal: string, tenant: string): void {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(PRINCIPAL_KEY, principal);
    localStorage.setItem(TENANT_KEY, tenant);
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

export async function login(username: string, password: string): Promise<void> {
  const res = await fetch(BASE() + "/v1/auth/login", {
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

export function signOut(): void {
  session.clear();
  location.reload();
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = session.token;
  if (token) headers["Authorization"] = "Bearer " + token;

  const res = await fetch(BASE() + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && token) {
    // The token expired or was revoked: drop it and return to sign-in.
    signOut();
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

const TERMINAL_PHASES = ["completed", "failed", "cancelled"];

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

export const api = {
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
  const token = session.token;
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
