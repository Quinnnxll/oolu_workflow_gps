import type { TaskView } from "./types";

// The function words: what OoLu says about what the engine did, so a user
// can verify every action from the talk instead of decoding event codes.
// Keys are the audit log's real event_type vocabulary.
const EVENT_WORDS: Record<string, string> = {
  "workflow.submitted": "Accepted the job",
  "workflow.started": "Started working",
  "workflow.advance": "Moved to the next step",
  "workflow.advanced": "Moved to the next step",
  "workflow.executed": "Carried out the actions",
  "workflow.paused": "Paused — waiting on you",
  "workflow.resumed": "Picked it back up",
  "workflow.completed": "Finished the job",
  "workflow.failed": "Hit a failure",
  "workflow.incident": "Ran into a problem",
  "workflow.cancelled": "Stopped on your request",
  "workflow.preflight_failed": "Stopped before running — the preflight checks failed",
  "contract.held": "Held the request for a manual commit",
  "contract.approved": "An approver committed the request",
  "contract.declined": "An approver declined the request",
  "contract.expired": "The held request expired undecided",
  "feedback.received": "Noted your feedback",
  "skill.blocked": "Blocked an unsafe action",
};

export function humanizeEvent(eventType: string): string {
  // Unknown types degrade to readable words, never to raw codes.
  return EVENT_WORDS[eventType] ?? eventType.replace(/[._]/g, " ");
}

// One sentence for a run's current state — the run card's voice.
export function statusSentence(task: TaskView): string {
  if (task.awaiting === "clarification")
    return "I need an answer from you to continue.";
  if (task.awaiting === "confirmation")
    return "I need your go-ahead before I act.";
  if (task.awaiting === "approval")
    return "This needs an authorized approval before I act.";
  if (task.awaiting === "incident")
    return "Something went wrong — tell me how to proceed.";
  if (task.phase === "completed") return "Done — here's the verified result.";
  if (task.phase === "failed") return "It didn't work.";
  if (task.phase === "cancelled") return "Stopped, as you asked.";
  return "I'm on it — you can watch every step below.";
}
