import { TERMINAL_PHASES } from "./api";
import { conciseName } from "./naming";
import type { RunSummary } from "./types";

// The conversation with OoLu never ends, so unfinished work must not rely
// on the user scrolling back to find it. After the user has been idle a
// while, the chat brings the open work forward ONCE: the reminder bubble
// stays at the bottom of the thread, and while it sits there unanswered,
// repeating it adds nothing — that is a nag storm, not presence. After a
// longer absence the loop goes dormant instead of posting into an empty
// room; the user coming BACK (their next message) is what earns the next
// reminder.

// How long the user must be idle (no message sent) before the one
// reminder of the idle stretch may appear.
export const REMIND_IDLE_MS = 2 * 60_000;
// Past this much idleness the user is not "about to look": the loop goes
// dormant and waits for their return instead of reminding nobody.
export const REMIND_DORMANT_MS = 15 * 60_000;
// How often the chat re-checks the run list.
export const REMIND_CHECK_MS = 30_000;

export interface ReminderClock {
  lastActivityAt: number; // the user's last message (or mount)
  lastReminderAt: number; // 0 = never reminded yet
}

export function reminderDue(clock: ReminderClock, now: number): boolean {
  const idle = now - clock.lastActivityAt;
  if (idle < REMIND_IDLE_MS) return false; // the user is here and active
  // One reminder per idle stretch: while it is already the last thing in
  // the thread, saying it again is noise.
  if (clock.lastReminderAt > clock.lastActivityAt) return false;
  // The user has been gone too long to be reading: dormant. Their return
  // is the event that surfaces the open work again (returnedFromAway).
  if (idle >= REMIND_DORMANT_MS) return false;
  return true;
}

// Was the user away long enough that the loop went dormant on them? Their
// next message then deserves one fresh look at the open work — the
// welcome-back reminder the chat posts after answering them.
export function returnedFromAway(clock: ReminderClock, now: number): boolean {
  return now - clock.lastActivityAt >= REMIND_DORMANT_MS;
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

// The reminder's arrows: the same runs as the words, as jump targets —
// what waits on the user first, then what is still moving. Each arrow
// points straight back to the task's action window (its run card).
export interface ReminderRun {
  runId: string;
  label: string;
  awaiting: string | null;
}

export function reminderRuns(runs: RunSummary[]): ReminderRun[] {
  const pending = runs.filter((r) => r.awaiting !== null);
  const ongoing = runs.filter(
    (r) => r.awaiting === null && !TERMINAL_PHASES.includes(r.phase),
  );
  return [...pending, ...ongoing].slice(0, 6).map((r) => ({
    runId: r.run_id,
    label: conciseName(r.intent),
    awaiting: r.awaiting,
  }));
}

function awaitingWords(awaiting: string | null): string {
  if (awaiting === "clarification") return "needs an answer";
  if (awaiting === "confirmation") return "needs a decision";
  if (awaiting === "approval") return "needs an approval";
  if (awaiting === "incident") return "hit a snag";
  return "needs you";
}
