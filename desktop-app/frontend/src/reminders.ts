import { TERMINAL_PHASES } from "./api";
import { conciseName } from "./naming";
import type { RunSummary } from "./types";

// The conversation with OoLu never ends, so unfinished work must not rely
// on the user scrolling back to find it. After the user has been idle for
// a while, the chat itself brings the open work forward: one reminder
// bubble listing what is still running and what is waiting on them —
// repeated at a bounded cadence, never a nag storm.

// How long the user must be idle (no message sent) before the first
// reminder may appear.
export const REMIND_IDLE_MS = 2 * 60_000;
// The minimum gap between two reminders.
export const REMIND_EVERY_MS = 5 * 60_000;
// How often the chat re-checks the run list.
export const REMIND_CHECK_MS = 30_000;

export interface ReminderClock {
  lastActivityAt: number; // the user's last message (or mount)
  lastReminderAt: number; // 0 = never reminded yet
}

export function reminderDue(clock: ReminderClock, now: number): boolean {
  if (now - clock.lastActivityAt < REMIND_IDLE_MS) return false;
  const anchor = Math.max(clock.lastReminderAt, clock.lastActivityAt);
  // The first reminder waits out the idle window; the next ones keep the
  // five-minute distance from whichever came last — a reminder or the
  // user speaking again.
  return clock.lastReminderAt === 0
    ? now - clock.lastActivityAt >= REMIND_IDLE_MS
    : now - anchor >= REMIND_EVERY_MS;
}

// The reminder's words, from the live run list — or null when there is
// nothing worth reminding about (all runs terminal).
export function reminderText(runs: RunSummary[]): string | null {
  const pending = runs.filter((r) => r.awaiting !== null);
  const ongoing = runs.filter(
    (r) => r.awaiting === null && !TERMINAL_PHASES.includes(r.phase),
  );
  if (pending.length === 0 && ongoing.length === 0) return null;

  const parts: string[] = [];
  if (pending.length > 0) {
    const items = pending
      .slice(0, 3)
      .map((r) => `“${conciseName(r.intent)}” (${awaitingWords(r.awaiting)})`)
      .join(", ");
    const more = pending.length > 3 ? ` and ${pending.length - 3} more` : "";
    parts.push(`waiting on you: ${items}${more}`);
  }
  if (ongoing.length > 0) {
    const items = ongoing
      .slice(0, 3)
      .map((r) => `“${conciseName(r.intent)}”`)
      .join(", ");
    const more = ongoing.length > 3 ? ` and ${ongoing.length - 3} more` : "";
    parts.push(`still working: ${items}${more}`);
  }
  return `A quick reminder — ${parts.join("; ")}.`;
}

function awaitingWords(awaiting: string | null): string {
  if (awaiting === "clarification") return "needs an answer";
  if (awaiting === "confirmation") return "needs a decision";
  if (awaiting === "approval") return "needs an approval";
  if (awaiting === "incident") return "hit a snag";
  return "needs you";
}
