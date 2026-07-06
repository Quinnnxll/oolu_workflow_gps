export interface QuestionView {
  parameter: string;
  question: string;
  suggested_values: unknown[];
  priority?: number;
}

// The gateway's run view-model (`GET /v1/runs/{id}`). `questions` and
// `can_cancel` are not part of the wire shape — the api layer derives them
// (fetching /questions when awaiting clarification, deriving cancel from phase).
export interface TaskView {
  run_id: string;
  intent: string;
  phase: string;
  awaiting: string | null;
  prompt: string | null;
  questions: QuestionView[];
  can_cancel: boolean;
  failure_reason: string | null;
  result: Record<string, unknown> | null;
}

// Pause kinds surfaced by the gateway (`_PAUSE_VALUE`). Any of these means the
// run is waiting on a human, so the api layer treats them all as inbox items.
export type AwaitingKind =
  | "clarification"
  | "confirmation"
  | "approval"
  | "incident";

// Synthesised client-side from `GET /v1/runs` (there is no /v1/inbox endpoint):
// every run whose `awaiting` is set becomes an item.
export interface InboxItem {
  run_id: string;
  kind: AwaitingKind;
  intent: string;
  prompt: string;
  created_at?: string;
}

export interface TimelineEvent {
  at: string;
  label: string;
  detail: string;
}

// One row in the Noder conversation list: a node interaction (a run) whose
// audit log is that thread's message history.
export interface RunSummary {
  run_id: string;
  intent: string;
  phase: string;
  awaiting: string | null;
}

// A marketplace listing (`GET /v1/listings`). The desktop "Skills" tab browses
// published nodes, which is the only discovery surface the gateway exposes.
export interface Listing {
  listing_id: string;
  version_id?: string;
  title: string;
  summary: string;
  status: string;
  tags: string[];
  maturity_label?: string;
}

export interface ExecutionLabel {
  trust_level: string;
  allowed_backends: string[];
  isolated: boolean;
  label: string;
}

export interface WorkerHealth {
  docker_available: boolean;
  labels: ExecutionLabel[];
}
