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

  it("quick actions send or pre-fill the command", async () => {
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

    fireEvent.click(screen.getByRole("button", { name: "Pending requests" }));
    await waitFor(() =>
      expect(calls.some((c) => c.path === "/v1/chat")).toBe(true),
    );
    expect(
      (calls.find((c) => c.path === "/v1/chat")?.body as { message: string })
        .message,
    ).toBe("pending");

    // "Sign all…" only pre-fills — signing needs the user's typed name.
    fireEvent.click(screen.getByRole("button", { name: "Sign all…" }));
    expect(
      (
        screen.getByPlaceholderText(
          "Message OoLu about Invoice Cleaner…",
        ) as HTMLTextAreaElement
      ).value,
    ).toBe("sign all as ");
  });
});
