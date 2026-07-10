import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("is a clean conversation: no button chrome, no banner text", () => {
    render(<NodeInteract node={node()} />);

    // The whole pane is thread + composer — nothing else claims space.
    expect(screen.queryByText(/Automation reliability/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Pending" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Sign" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Build" })).toBeNull();
    expect(screen.queryByRole("button", { name: /accelerate/i })).toBeNull();
    // One hint inside the empty thread teaches the typed commands —
    // task id included — and disappears with the first message.
    expect(screen.getByText(/sign <task id> as <your name>/)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Send" }),
    ).toBeTruthy();
  });

  it("typed commands still drive the desk", async () => {
    routes["POST /v1/chat"] = {
      status: 200,
      body: {
        reply: "Waiting on you:\n• clean-the-books — from consumer-1 (a1b2c3d4)",
        source: "tool",
        actions: [{ tool: "node_holds" }],
        run_id: null,
      },
    };
    render(<NodeInteract node={node()} />);

    fireEvent.change(
      screen.getByPlaceholderText("Message OoLu about Invoice Cleaner…"),
      { target: { value: "pending" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    // The listing carries each task's id — the handle Sign passes on.
    expect(await screen.findByText(/a1b2c3d4/)).toBeTruthy();
    expect(
      (calls.find((c) => c.path === "/v1/chat")?.body as { message: string })
        .message,
    ).toBe("pending");
  });
});
