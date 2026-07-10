import { describe, expect, it } from "vitest";
import {
  REMIND_EVERY_MS,
  REMIND_IDLE_MS,
  reminderDue,
  reminderText,
} from "./reminders";
import type { RunSummary } from "./types";

function run(overrides: Partial<RunSummary>): RunSummary {
  return {
    run_id: "r1",
    intent: "convert the quarterly report to pdf",
    phase: "execution",
    awaiting: null,
    ...overrides,
  };
}

describe("reminderDue", () => {
  const t0 = 1_000_000;

  it("stays quiet while the user is active", () => {
    expect(
      reminderDue({ lastActivityAt: t0, lastReminderAt: 0 }, t0 + 1_000),
    ).toBe(false);
  });

  it("fires once the idle window passes, then keeps its distance", () => {
    const idle = { lastActivityAt: t0, lastReminderAt: 0 };
    expect(reminderDue(idle, t0 + REMIND_IDLE_MS)).toBe(true);

    const reminded = {
      lastActivityAt: t0,
      lastReminderAt: t0 + REMIND_IDLE_MS,
    };
    expect(reminderDue(reminded, t0 + REMIND_IDLE_MS + 60_000)).toBe(false);
    expect(
      reminderDue(reminded, t0 + REMIND_IDLE_MS + REMIND_EVERY_MS),
    ).toBe(true);
  });

  it("a new user message resets the idle clock", () => {
    const spoke = {
      lastActivityAt: t0 + REMIND_EVERY_MS,
      lastReminderAt: t0,
    };
    expect(
      reminderDue(spoke, t0 + REMIND_EVERY_MS + REMIND_IDLE_MS - 1),
    ).toBe(false);
  });
});

describe("reminderText", () => {
  it("says nothing when every run is finished", () => {
    expect(reminderText([run({ phase: "completed" })])).toBeNull();
    expect(reminderText([])).toBeNull();
  });

  it("lists what waits on the user and what is still working, concisely", () => {
    const text = reminderText([
      run({ run_id: "r1", awaiting: "confirmation" }),
      run({
        run_id: "r2",
        intent: "fetch the latest exchange rates for me",
        phase: "execution",
      }),
    ]);
    expect(text).toContain("waiting on you");
    // The reminder names tasks by keywords, never the whole sentence.
    expect(text).toContain("“Convert Quarterly Report Pdf” (needs a decision)");
    expect(text).toContain("still working: “Fetch Latest Exchange Rates”");
  });

  it("caps the list and counts the rest", () => {
    const many = [1, 2, 3, 4, 5].map((i) =>
      run({ run_id: `r${i}`, awaiting: "confirmation" }),
    );
    const text = reminderText(many);
    expect(text).toContain("and 2 more");
  });
});
