import type {
  InboxItem,
  SkillCard,
  TaskView,
  TimelineEvent,
  WorkerHealth,
} from "./types";

// Dev: empty base → relative URLs → Vite proxies /v1 to the loopback backend.
// Packaged: the Tauri shell injects the sidecar origin as window.__OOLU_API__.
declare global {
  interface Window {
    __OOLU_API__?: string;
  }
}

const BASE = (): string => window.__OOLU_API__ ?? "";

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE() + path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw new Error(data?.error ?? `${res.status}`);
  }
  return data as T;
}

export const api = {
  submitTask: (intent: string) =>
    req<TaskView>("POST", "/v1/tasks", { intent }),
  task: (id: string) => req<TaskView>("GET", `/v1/tasks/${id}`),
  answer: (id: string, answers: Record<string, unknown>) =>
    req<TaskView>("POST", `/v1/tasks/${id}/answers`, { answers }),
  confirm: (id: string, approved: boolean) =>
    req<TaskView>("POST", `/v1/tasks/${id}/confirm`, { approved }),
  resolveIncident: (id: string, decision: string) =>
    req<TaskView>("POST", `/v1/tasks/${id}/resolve-incident`, { decision }),
  cancel: (id: string) => req<TaskView>("POST", `/v1/tasks/${id}/cancel`),
  timeline: (id: string) =>
    req<{ items: TimelineEvent[] }>("GET", `/v1/tasks/${id}/timeline`),
  inbox: (kind?: string) =>
    req<{ items: InboxItem[] }>(
      "GET",
      kind ? `/v1/inbox?kind=${encodeURIComponent(kind)}` : "/v1/inbox",
    ),
  skills: (q?: string) =>
    req<{ items: SkillCard[] }>(
      "GET",
      q ? `/v1/skills?q=${encodeURIComponent(q)}` : "/v1/skills",
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
  const ws = new WebSocket(`${origin}/v1/tasks/${runId}/events`);
  ws.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data) as TimelineEvent);
    } catch {
      /* ignore malformed frame */
    }
  };
  return ws;
}
