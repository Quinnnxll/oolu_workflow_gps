import { afterEach, describe, expect, it, vi } from "vitest";
import {
  deriveTone,
  deriveUserMood,
  identityHue,
  moodOf,
  onAvatarSignals,
  resetAvatarSignals,
  updateAvatarSignals,
} from "./avatar";
import type { AvatarSignals } from "./avatar";

afterEach(() => resetAvatarSignals());

function signals(patch: Partial<AvatarSignals>): AvatarSignals {
  return {
    listening: false,
    speaking: false,
    workload: 0,
    tone: "neutral",
    userMood: "neutral",
    ...patch,
  };
}

describe("the avatar's mind", () => {
  it("reads the user's mood from their message", () => {
    expect(deriveUserMood("thanks, that was great")).toBe("positive");
    expect(deriveUserMood("this is broken and useless")).toBe("negative");
    expect(deriveUserMood("I need this ASAP!!")).toBe("urgent");
    expect(deriveUserMood("WHY IS THIS HAPPENING")).toBe("negative");
    expect(deriveUserMood("convert the report to pdf")).toBe("neutral");
  });

  it("reads the conversation's tone from the reply", () => {
    expect(deriveTone("It didn't work.")).toBe("bad");
    expect(deriveTone("Which one do you mean: a, b?")).toBe("asking");
    expect(deriveTone("Done — here's the verified result.")).toBe("good");
    expect(deriveTone("Your tasks:\n(none)")).toBe("neutral");
  });

  it("maps signals to moods the way a face should", () => {
    expect(moodOf(signals({ listening: true })).mood).toBe("excited");
    expect(moodOf(signals({ tone: "bad" })).mood).toBe("worried");
    expect(moodOf(signals({ userMood: "negative" })).mood).toBe("worried");
    expect(moodOf(signals({ workload: 3 })).mood).toBe("thinking");
    expect(moodOf(signals({ tone: "good" })).mood).toBe("happy");
    expect(moodOf(signals({})).mood).toBe("calm");
    // Trouble outranks busyness: worried wins over workload.
    expect(moodOf(signals({ tone: "bad", workload: 3 })).mood).toBe("worried");
    // A model turn in flight: the face thinks — and churns harder than idle.
    expect(moodOf(signals({ thinking: true })).mood).toBe("thinking");
    expect(
      moodOf(signals({ thinking: true })).agitation,
    ).toBeGreaterThan(moodOf(signals({})).agitation);
  });

  it("keeps agitation in bounds and voice drives it", () => {
    const idle = moodOf(signals({}));
    const loud = moodOf(
      signals({ listening: true, speaking: true, workload: 4, userMood: "urgent" }),
    );
    expect(idle.agitation).toBeGreaterThan(0);
    expect(loud.agitation).toBeLessThanOrEqual(1);
    expect(loud.agitation).toBeGreaterThan(idle.agitation);
  });

  it("publishes signal changes to subscribers", () => {
    const seen = vi.fn();
    const off = onAvatarSignals(seen);
    updateAvatarSignals({ workload: 2 });
    expect(seen).toHaveBeenLastCalledWith(
      expect.objectContaining({ workload: 2 }),
    );
    off();
    updateAvatarSignals({ workload: 5 });
    expect(seen).toHaveBeenCalledTimes(2); // initial + one update, not the third
  });

  it("gives every account a stable identity hue", () => {
    expect(identityHue("Invoice Cleaner")).toBe(identityHue("Invoice Cleaner"));
    expect(identityHue("Invoice Cleaner")).not.toBe(identityHue("Tax Filer"));
    expect(identityHue("anything")).toBeGreaterThanOrEqual(0);
    expect(identityHue("anything")).toBeLessThan(360);
  });
});
