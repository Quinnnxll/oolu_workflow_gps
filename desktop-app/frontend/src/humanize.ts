import type { TaskView } from "./types";
import { t } from "./ui";

// The function words: what OoLu says about what the engine did, so a user
// can verify every action from the talk instead of decoding event codes.
// The words live in the ui dictionary (keys "event.<event_type>"), so the
// feed follows app.language like every other piece of chrome.

export function humanizeEvent(eventType: string): string {
  const words = t(`event.${eventType}`);
  // Unknown types degrade to readable words, never to raw codes.
  return words !== `event.${eventType}`
    ? words
    : eventType.replace(/[._]/g, " ");
}

// One sentence for a run's current state — the run card's voice.
export function statusSentence(task: TaskView): string {
  if (task.awaiting === "clarification") return t("voice.clarification");
  if (task.awaiting === "confirmation") return t("voice.confirmation");
  if (task.awaiting === "approval") return t("voice.approval");
  if (task.awaiting === "incident") return t("voice.incident");
  if (task.phase === "completed") return t("voice.completed");
  if (task.phase === "failed") return t("voice.failed");
  if (task.phase === "cancelled") return t("voice.cancelledSentence");
  return t("voice.working");
}
