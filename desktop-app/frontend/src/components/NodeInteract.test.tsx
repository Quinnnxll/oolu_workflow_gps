import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
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

  it("keeps an unsent draft when the user leaves and returns", () => {
    const first = render(<NodeInteract node={node()} />);
    fireEvent.change(
      screen.getByPlaceholderText("Message OoLu about Invoice Cleaner…"),
      { target: { value: "not sent yet" } },
    );
    first.unmount();

    render(<NodeInteract node={node()} />);
    expect(
      (
        screen.getByPlaceholderText(
          "Message OoLu about Invoice Cleaner…",
        ) as HTMLInputElement
      ).value,
    ).toBe("not sent yet");
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

  it("glows while the model reasons, and shows the thinking dimmed", async () => {
    // A slow turn: the promise resolves only when the test lets it.
    // Routed by path (the mount now loads server history first, V0),
    // so the gate holds exactly the CHAT call, nothing else.
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    fetchMock.mockImplementation(async (input: string | URL) => {
      if (String(input).includes("/v1/chat/history")) {
        return {
          ok: true,
          status: 200,
          text: async () => JSON.stringify({ items: [] }),
          json: async () => ({ items: [] }),
        } as unknown as Response;
      }
      await gate;
      return {
        ok: true,
        status: 200,
        text: async () =>
          JSON.stringify({
            reply: "Ordered. Anything else?",
            source: "model",
            reasoning: "The user wants an order node; checking the desk first.",
            actions: [],
            run_id: null,
          }),
        json: async () => ({}),
      } as unknown as Response;
    });
    render(<NodeInteract node={node()} />);

    fireEvent.change(
      screen.getByPlaceholderText("Message OoLu about Invoice Cleaner…"),
      { target: { value: "build an order node" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    // In flight: the node's profile photo breathes with the glow, and the
    // thinking note tells the user OoLu is still working on it.
    const face = await screen.findByRole("img", { name: /Working on it/ });
    expect(face.className).toContain("thinking");
    expect(screen.getByText(/the reply lands when it's ready/)).toBeTruthy();

    release();
    // Landed: the glow is gone, the reply speaks, and the model's brief
    // reasoning rides above it — dimmed, folded, never the answer.
    expect(await screen.findByText("Ordered. Anything else?")).toBeTruthy();
    expect(screen.queryByRole("img", { name: /Thinking/ })).toBeNull();
    // Twice: the folded one-line brief, and the full monologue behind it.
    expect(screen.getAllByText(/checking the desk first/).length).toBe(2);
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

// ---- V0: the node's thread lives on the server ----------------------------
describe("NodeInteract — the server-side thread (V0)", () => {
  it("loads the node's server thread on mount — the reply a past visit never waited for", async () => {
    routes["GET /v1/chat/history"] = {
      status: 200,
      body: {
        items: [
          { seq: 1, kind: "user", body: "tally the ledger", at: "t" },
          { seq: 2, kind: "assistant", body: "Tallied: 41 rows.", at: "t" },
        ],
      },
    };
    render(<NodeInteract node={node()} />);
    expect(await screen.findByText("Tallied: 41 rows.")).toBeTruthy();
    // The request named THIS node's thread.
    const history = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/v1/chat/history"),
    );
    expect(String(history?.[0])).toContain(encodeURIComponent("node:n1"));
  });

  it("a return mid-turn shows the working state, then the reply", async () => {
    vi.useFakeTimers();
    try {
      routes["GET /v1/chat/history"] = {
        status: 200,
        body: {
          items: [
            { seq: 1, kind: "user", body: "tally the ledger", at: "t" },
            { seq: 2, kind: "working", body: "", at: "t" },
          ],
        },
      };
      render(<NodeInteract node={node()} />);
      await act(async () => {});
      expect(screen.getByText(/the reply lands when it's ready/)).toBeTruthy();

      routes["GET /v1/chat/history"] = {
        status: 200,
        body: {
          items: [
            { seq: 1, kind: "user", body: "tally the ledger", at: "t" },
            { seq: 3, kind: "assistant", body: "Tallied: 41 rows.", at: "t" },
          ],
        },
      };
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600);
      });
      expect(screen.getByText("Tallied: 41 rows.")).toBeTruthy();
      expect(
        screen.queryByText(/the reply lands when it's ready/),
      ).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("a broken request with the server still working never fabricates failure", async () => {
    routes["POST /v1/chat"] = {
      status: 500,
      body: { error: { message: "boom" } },
    };
    routes["GET /v1/chat/history"] = {
      status: 200,
      body: {
        items: [
          { seq: 1, kind: "user", body: "tally it", at: "t" },
          { seq: 2, kind: "working", body: "", at: "t" },
        ],
      },
    };
    render(<NodeInteract node={node()} />);
    await act(async () => {});
    fireEvent.change(
      screen.getByPlaceholderText("Message OoLu about Invoice Cleaner…"),
      { target: { value: "tally it" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => {});

    expect(screen.queryByText(/didn't go through/)).toBeNull();
    expect(screen.getByText(/the reply lands when it's ready/)).toBeTruthy();
  });

  it("folds the worker's report into the node thread when its run settles (V1)", async () => {
    vi.useFakeTimers();
    try {
      routes["GET /v1/chat/history"] = { status: 200, body: { items: [] } };
      routes["POST /v1/chat"] = {
        status: 200,
        body: {
          reply: "On it!",
          source: "intent",
          actions: [],
          run_id: "r9",
          run: { run_id: "r9", phase: "intake", awaiting: null },
        },
      };
      routes["GET /v1/runs/r9"] = {
        status: 200,
        body: {
          run_id: "r9",
          intent: "tally the ledger",
          phase: "completed",
          awaiting: null,
          prompt: null,
          failure_reason: null,
          result: null,
        },
      };
      render(<NodeInteract node={node()} />);
      await act(async () => {});
      fireEvent.change(
        screen.getByPlaceholderText("Message OoLu about Invoice Cleaner…"),
        { target: { value: "tally the ledger" } },
      );
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      await act(async () => {});
      expect(screen.getByText("On it!")).toBeTruthy();

      routes["GET /v1/chat/history"] = {
        status: 200,
        body: {
          items: [
            { seq: 1, kind: "user", body: "tally the ledger", at: "t" },
            { seq: 2, kind: "assistant", body: "On it!", at: "t" },
            { seq: 3, kind: "run", body: "r9", at: "t" },
            {
              seq: 4,
              kind: "assistant",
              body: "Done — “tally the ledger” finished.",
              at: "t",
            },
          ],
        },
      };
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600);
      });
      expect(
        screen.getByText(/Done — “tally the ledger” finished\./),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("a genuine failure — reachable host, not working, no reply — still apologizes", async () => {
    routes["POST /v1/chat"] = {
      status: 500,
      body: { error: { message: "boom" } },
    };
    routes["GET /v1/chat/history"] = { status: 200, body: { items: [] } };
    render(<NodeInteract node={node()} />);
    await act(async () => {});
    fireEvent.change(
      screen.getByPlaceholderText("Message OoLu about Invoice Cleaner…"),
      { target: { value: "tally it" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText(/didn't go through/)).toBeTruthy();
  });
});
