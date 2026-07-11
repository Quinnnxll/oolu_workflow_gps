import { describe, expect, it } from "vitest";
import {
  REMIND_DORMANT_MS,
  REMIND_IDLE_MS,
  reminderDue,
  reminderText,
  returnedFromAway,
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

  it("fires ONCE per idle stretch — never again while it sits at the bottom", () => {
    const idle = { lastActivityAt: t0, lastReminderAt: 0 };
    expect(reminderDue(idle, t0 + REMIND_IDLE_MS)).toBe(true);

    const reminded = {
      lastActivityAt: t0,
      lastReminderAt: t0 + REMIND_IDLE_MS,
    };
    // Five minutes later, an hour later: the bubble is already the last
    // thing in the thread — repeating it is a nag storm, not presence.
    expect(reminderDue(reminded, t0 + REMIND_IDLE_MS + 5 * 60_000)).toBe(
      false,
    );
    expect(reminderDue(reminded, t0 + 60 * 60_000)).toBe(false);
  });

  it("a new user message opens a fresh stretch", () => {
    const spoke = {
      lastActivityAt: t0 + 10 * 60_000,
      lastReminderAt: t0 + REMIND_IDLE_MS, // the previous stretch's bubble
    };
    expect(
      reminderDue(spoke, t0 + 10 * 60_000 + REMIND_IDLE_MS - 1),
    ).toBe(false);
    expect(reminderDue(spoke, t0 + 10 * 60_000 + REMIND_IDLE_MS)).toBe(true);
  });

  it("goes dormant after a long absence instead of posting into an empty room", () => {
    // Nothing was ever posted this stretch (the work turned pending late),
    // but the user has been gone past the dormancy line: wait for their
    // return — that is what earns the next reminder.
    const gone = { lastActivityAt: t0, lastReminderAt: 0 };
    expect(reminderDue(gone, t0 + REMIND_DORMANT_MS)).toBe(false);
    expect(reminderDue(gone, t0 + 3 * REMIND_DORMANT_MS)).toBe(false);
  });
});

describe("returnedFromAway", () => {
  const t0 = 1_000_000;

  it("a short pause is not an absence", () => {
    expect(
      returnedFromAway(
        { lastActivityAt: t0, lastReminderAt: 0 },
        t0 + REMIND_DORMANT_MS - 1,
      ),
    ).toBe(false);
  });

  it("past the dormancy line, the return earns one fresh reminder", () => {
    expect(
      returnedFromAway(
        { lastActivityAt: t0, lastReminderAt: 0 },
        t0 + REMIND_DORMANT_MS,
      ),
    ).toBe(true);
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
