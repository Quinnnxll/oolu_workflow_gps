export interface QuestionView {
  parameter: string;
  question: string;
  suggested_values: unknown[];
  priority: number;
}

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

export interface InboxItem {
  run_id: string;
  kind: "confirmation" | "approval" | "incident";
  intent: string;
  prompt: string;
  created_at: string;
}

export interface TimelineEvent {
  at: string;
  label: string;
  detail: string;
}

export interface SkillCard {
  skill_id: string;
  semver: string;
  name: string;
  summary: string;
  tags: string[];
  score?: number;
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
