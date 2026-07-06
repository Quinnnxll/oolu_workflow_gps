import { describe, expect, it } from "vitest";
import { humanizeEvent, statusSentence } from "./humanize";
import type { TaskView } from "./types";

function task(overrides: Partial<TaskView>): TaskView {
  return {
    run_id: "r1",
    intent: "x",
    phase: "executing",
    awaiting: null,
    prompt: null,
    questions: [],
    can_cancel: true,
    failure_reason: null,
    result: null,
    ...overrides,
  };
}

describe("humanize", () => {
  it("speaks the audit vocabulary in words", () => {
    expect(humanizeEvent("workflow.started")).toBe("Started working");
    expect(humanizeEvent("workflow.completed")).toBe("Finished the job");
    expect(humanizeEvent("contract.held")).toBe(
      "Held the request for a manual commit",
    );
    expect(humanizeEvent("skill.blocked")).toBe("Blocked an unsafe action");
  });

  it("degrades unknown events to readable words, never raw codes", () => {
    expect(humanizeEvent("payments.settlement_posted")).toBe(
      "payments settlement posted",
    );
  });

  it("gives every run state a sentence", () => {
    expect(statusSentence(task({ awaiting: "clarification" }))).toMatch(
      /need an answer/,
    );
    expect(statusSentence(task({ awaiting: "confirmation" }))).toMatch(
      /go-ahead/,
    );
    expect(statusSentence(task({ phase: "completed" }))).toMatch(/verified/);
    expect(statusSentence(task({ phase: "failed" }))).toMatch(/didn't work/);
    expect(statusSentence(task({}))).toMatch(/watch every step/);
  });
});
