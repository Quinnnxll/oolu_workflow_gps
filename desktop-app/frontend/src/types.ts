export interface QuestionView {
  parameter: string;
  question: string;
  suggested_values: unknown[];
  priority?: number;
}

// One step of the chosen route, with its live execution status. `failed`
// marks the EXACT node that caused the failure (never its cascade-cancelled
// dependents).
export interface PlanStep {
  id: string;
  label: string;
  status: string; // planned | succeeded | failed | blocked | cancelled
  error: string | null;
  failed: boolean;
}

// How OoLu planned the steps. `origin === "llm_rebuild"` means the model
// planned this route and wrote its code after retries ran out; `notes` is
// the model's own numbered plan.
export interface PlanView {
  route: string;
  origin: string;
  notes: string[];
  steps: PlanStep[];
}

// Why there was no route or node to search from (planning failed before a
// viable route existed): the grounding result plus every candidate route
// the optimizer excluded, each with its reason.
export interface NoRouteView {
  reason: string;
  unresolved_terms: string[];
  resolved_capabilities: string[];
  candidates: { name: string; excluded: boolean; reason: string | null }[];
}

// The exact node that caused the most recent execution failure, plus the
// retry state and — when the LLM rebuild ran and refused — its reason.
// `code` is the stable machine label (EXEC_NODE_FAILED / EXEC_BLOCKED)
// the user keeps to fix the automation later.
export interface FailureView {
  code?: string;
  node_id: string | null;
  node_label: string | null;
  error: string | null;
  attempt: number;
  user_retries: number;
  rebuild_refusal: string | null;
}

// The auto-build consent check, present on every failed/incident run.
export interface AutobuildView {
  consent: boolean;
  hint: string | null;
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
  user_retries: number;
  plan: PlanView | null;
  no_route: NoRouteView | null;
  failure: FailureView | null;
  autobuild: AutobuildView | null;
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
