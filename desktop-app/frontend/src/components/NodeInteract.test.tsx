import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { WorkNode } from "../api";
import { NodeInteract, reliabilityLine } from "./NodeInteract";

let routes: Record<string, { status: number; body: unknown }>;
let calls: { method: string; path: string; body: unknown }[];

const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
  const u = new URL(String(input), "http://local.test");
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  calls.push({ method, path: u.pathname, body });
  const hit = routes[`${method} ${u.pathname}`] ?? { status: 200, body: {} };
  return {
    ok: hit.status >= 200 && hit.status < 300,
    status: hit.status,
    text: async () => JSON.stringify(hit.body),
    json: async () => hit.body,
  } as Response;
});

beforeEach(() => {
  routes = {};
  calls = [];
  localStorage.clear();
  window.__OOLU_API__ = "http://local.test";
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockClear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OOLU_API__;
});

function node(overrides: Partial<WorkNode> = {}): WorkNode {
  return {
    node_id: "n1",
    title: "Invoice Cleaner",
    status: "live",
    account: {
      node_id: "n1",
      responsible: "alice",
      admin: null,
      authority_level: null,
      is_supernode: false,
      supernode_id: null,
      status: "live",
      audit_mode: true,
      allow_autodev_data: true,
    },
    earnings_micros: 0,
    health: { verified_successes: 132, verified_failures: 1, score: 0.992 },
    ...overrides,
  };
}

describe("NodeInteract", () => {
  it("speaks the automation-reliability vision from verified runs", () => {
    expect(reliabilityLine(node())).toContain("99.2% over 133 verified runs");
    expect(
      reliabilityLine(
        node({
          health: { verified_successes: 0, verified_failures: 0, score: null },
        }),
      ),
    ).toContain("no verified runs yet");
  });

  it("sends node-scoped chat turns and shows what OoLu touched", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "Signed as Quinn:\n• clean-the-books: signed and allowed",
        source: "tool",
        actions: [{ tool: "decide_hold", name: "p1" }],
        run_id: null,
      },
    };
    render(<NodeInteract node={node()} />);

    fireEvent.change(
      screen.getByPlaceholderText("Message OoLu about Invoice Cleaner…"),
      { target: { value: "sign all as Quinn" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText(/signed and allowed/)).toBeTruthy();
    expect(screen.getByText("decided p1")).toBeTruthy();
    // The turn carried the node scope to the gateway.
    const chat = calls.find((c) => c.path === "/v1/chat");
    expect((chat?.body as { node_id: string }).node_id).toBe("n1");
  });

  it("one row — Pending sends, Sign and Build pre-fill", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "Nothing is waiting on this node right now.",
        source: "tool",
        actions: [{ tool: "node_holds" }],
        run_id: null,
      },
    };
    render(<NodeInteract node={node()} />);

    // Accelerate is nobody's button anymore — it happens by itself.
    expect(screen.queryByRole("button", { name: /accelerate/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Pending" }));
    await waitFor(() =>
      expect(calls.some((c) => c.path === "/v1/chat")).toBe(true),
    );
    expect(
      (calls.find((c) => c.path === "/v1/chat")?.body as { message: string })
        .message,
    ).toBe("pending");

    const box = screen.getByPlaceholderText(
      "Message OoLu about Invoice Cleaner…",
    ) as HTMLTextAreaElement;
    // With no known waiting task, Sign pre-fills and the id comes from
    // "pending" (or a task chip); signing still needs the typed name.
    fireEvent.click(screen.getByRole("button", { name: "Sign" }));
    expect(box.value).toBe("sign ");
    fireEvent.click(screen.getByRole("button", { name: "Build" }));
    expect(box.value).toBe("build ");
  });

  const HOLD = {
    pending_id: "a1b2c3d4e5f60718",
    name: "clean-the-books",
    reserved: ["audit-node:n1"],
    submitted_by: "consumer-1",
    created_at: "2026-07-10T10:00:00Z",
    expires_at: null,
    replies: [],
  };

  it("Sign appends the task id when exactly one task waits", () => {
    render(<NodeInteract node={node()} holds={[HOLD]} />);
    fireEvent.click(screen.getByRole("button", { name: "Sign" }));
    expect(
      (
        screen.getByPlaceholderText(
          "Message OoLu about Invoice Cleaner…",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe("sign a1b2c3d4 as ");
  });

  it("waiting tasks surface as chips; tapping one fills its sign command", () => {
    const second = {
      ...HOLD,
      pending_id: "ffee00112233",
      name: "export-raw",
    };
    render(<NodeInteract node={node()} holds={[HOLD, second]} />);

    // Both tasks visible with their ids — the click IS the id handover.
    fireEvent.click(
      screen.getByRole("button", { name: "export-raw · ffee0011" }),
    );
    expect(
      (
        screen.getByPlaceholderText(
          "Message OoLu about Invoice Cleaner…",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe("sign ffee0011 as ");

    // With several waiting, the bare Sign stays open-ended.
    fireEvent.click(screen.getByRole("button", { name: "Sign" }));
    expect(
      (
        screen.getByPlaceholderText(
          "Message OoLu about Invoice Cleaner…",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe("sign ");
  });
});
