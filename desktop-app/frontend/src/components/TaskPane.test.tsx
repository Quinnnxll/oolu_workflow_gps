import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { TaskPane } from "./TaskPane";
import type { TaskView } from "../types";

// The Timeline opens a WebSocket; a silent stub keeps these tests about the
// task view itself.
class FakeSocket {
  onopen: (() => void) | null = null;
  onmessage: ((m: unknown) => void) | null = null;
  onerror: (() => void) | null = null;
  close() {}
}

beforeEach(() => {
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ entries: [] }),
      json: async () => ({ entries: [] }),
    })) as unknown as typeof fetch,
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function task(overrides: Partial<TaskView>): TaskView {
  return {
    run_id: "r1",
    intent: "fetch the report",
    phase: "recovery",
    awaiting: null,
    prompt: null,
    questions: [],
    can_cancel: true,
    failure_reason: null,
    result: null,
    user_retries: 0,
    plan: null,
    no_route: null,
    failure: null,
    autobuild: null,
    ...overrides,
  };
}

const FAILED_PLAN = {
  route: "two-step",
  origin: "planned",
  notes: [],
  steps: [
    { id: "a1", label: "test/one", status: "succeeded", error: null, failed: false },
    { id: "a2", label: "test/two", status: "failed", error: "two broke", failed: true },
  ],
};

describe("TaskPane", () => {
  it("shows the plan steps and marks the exact failing node", () => {
    render(
      <TaskPane
        task={task({ plan: FAILED_PLAN })}
        setTask={() => {}}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByText("Route: two-step")).toBeTruthy();
    expect(screen.getByText("test/one")).toBeTruthy();
    expect(screen.getByText("test/two")).toBeTruthy();
    expect(screen.getByText("failed here")).toBeTruthy();
    expect(screen.getByText("two broke")).toBeTruthy();
  });

  it("names the failing node, the retries, and counts down to the AI rebuild", () => {
    render(
      <TaskPane
        task={task({
          awaiting: "incident",
          prompt: "execution escalated to an incident; operator decision required",
          user_retries: 1,
          failure: {
            node_id: "a2",
            node_label: "test/two",
            error: "two broke",
            attempt: 3,
            user_retries: 1,
            rebuild_refusal: null,
          },
        })}
        setTask={() => {}}
        onChanged={() => {}}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Retry (1 left before AI rebuild)" }),
    ).toBeTruthy();
    expect(screen.getByText("test/two")).toBeTruthy();
    expect(screen.getByText(/1 retry so far/)).toBeTruthy();
  });

  it("surfaces the auto-build hint when consent is off after a failure", () => {
    render(
      <TaskPane
        task={task({
          awaiting: "incident",
          autobuild: {
            consent: false,
            hint: "Turn on 'Auto-build nodes on my paths' in Settings to let OoLu plan and write the code itself when retries keep failing, then hit Retry again.",
          },
        })}
        setTask={() => {}}
        onChanged={() => {}}
      />,
    );
    expect(
      screen.getByText(/Auto-build nodes on my paths/),
    ).toBeTruthy();
  });

  it("badges an AI-rebuilt route and shows the model's own plan", () => {
    render(
      <TaskPane
        task={task({
          phase: "completed",
          plan: {
            route: "AI rebuild",
            origin: "llm_rebuild",
            notes: ["Compute the answer directly.", "Emit it through the contract."],
            steps: [
              {
                id: "s1",
                label: "script/run",
                status: "succeeded",
                error: null,
                failed: false,
              },
            ],
          },
        })}
        setTask={() => {}}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByText("AI rebuild", { selector: ".plan-badge" })).toBeTruthy();
    expect(screen.getByText("Compute the answer directly.")).toBeTruthy();
    expect(screen.getByText("script/run")).toBeTruthy();
  });

  it("explains why there was no route or node to search from", () => {
    render(
      <TaskPane
        task={task({
          phase: "failed",
          failure_reason: "no viable route: unresolved capabilities: teleport",
          no_route: {
            reason: "no viable route: unresolved capabilities: teleport",
            unresolved_terms: ["teleport"],
            resolved_capabilities: ["fetch"],
            candidates: [
              {
                name: "beam-route",
                excluded: true,
                reason: "unresolved capabilities: teleport",
              },
            ],
          },
        })}
        setTask={() => {}}
        onChanged={() => {}}
      />,
    );
    expect(
      screen.getByText("OoLu could not find a route to execute this."),
    ).toBeTruthy();
    expect(screen.getByText(/Nothing to search from for:/)).toBeTruthy();
    expect(screen.getByText("beam-route")).toBeTruthy();
  });
});
