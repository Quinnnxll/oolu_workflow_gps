import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { AddNode, NodeThread, Work } from "./Work";
import type { WorkNode } from "../api";

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

function workNode(overrides: Partial<WorkNode> = {}): WorkNode {
  return {
    node_id: "n1",
    title: "Invoice Cleaner",
    status: "live",
    account: {
      node_id: "n1",
      responsible: "alice",
      admin: "ops",
      authority_level: 3,
      status: "live",
      audit_mode: false,
      allow_autodev_data: true,
    },
    earnings_micros: 12_340_000,
    health: { verified_successes: 9, verified_failures: 1, score: 0.9 },
    ...overrides,
  };
}

describe("Work", () => {
  it("lists node accounts with earnings and health", async () => {
    routes["GET /v1/work/nodes"] = {
      status: 200,
      body: { items: [workNode()] },
    };
    render(<Work onLife={vi.fn()} />);

    expect(await screen.findByText("Invoice Cleaner")).toBeTruthy();
    expect(screen.getByText("$12.34 · 90% healthy")).toBeTruthy();
  });

  it("shows the empty state and the auto-build consent", async () => {
    routes["GET /v1/work/nodes"] = { status: 200, body: { items: [] } };
    render(<Work onLife={vi.fn()} />);

    expect(await screen.findByText(/No nodes yet/)).toBeTruthy();
    const consent = screen.getByRole("checkbox") as HTMLInputElement;
    expect(consent.checked).toBe(true);
    fireEvent.click(consent);
    expect(localStorage.getItem("oolu_autobuild")).toBe("off");
  });
});

describe("AddNode", () => {
  it("creates a node as admin and shapes its account", async () => {
    routes["POST /v1/nodeplace"] = { status: 201, body: { node_id: "n9" } };
    routes["POST /v1/work/nodes/n9/account"] = {
      status: 200,
      body: workNode().account,
    };
    const onDone = vi.fn();
    render(<AddNode onDone={onDone} />);

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Tax Filer" },
    });
    fireEvent.click(
      screen.getByLabelText(/Audit node — every request must be committed/),
    );
    fireEvent.click(
      screen.getByLabelText(/Allow data passing this node/),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create node" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("n9"));
    const create = calls.find((c) => c.path === "/v1/nodeplace");
    expect((create?.body as { title: string }).title).toBe("Tax Filer");
    const account = calls.find((c) => c.path === "/v1/work/nodes/n9/account");
    expect(account?.body).toEqual({
      audit_mode: true,
      allow_autodev_data: false,
    });
  });

  it("onboards an existing node as the responsible", async () => {
    routes["POST /v1/work/nodes/n7/account"] = {
      status: 200,
      body: workNode().account,
    };
    const onDone = vi.fn();
    render(<AddNode onDone={onDone} />);

    fireEvent.click(screen.getByRole("button", { name: "Onboard existing" }));
    fireEvent.change(screen.getByLabelText("Node id"), {
      target: { value: "n7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Onboard" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("n7"));
    expect(calls.some((c) => c.path === "/v1/nodeplace")).toBe(false);
  });
});

describe("NodeThread", () => {
  it("shows the execution feed cursor-style", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: {
        items: [
          {
            run_id: "run12345678",
            gross: 0.42,
            steps: [
              { seq: 1, event_type: "workflow.started", at: "t1" },
              { seq: 2, event_type: "workflow.executed", at: "t2" },
            ],
          },
        ],
      },
    };
    render(<NodeThread node={workNode()} onChanged={vi.fn()} />);

    expect(await screen.findByText("workflow.started")).toBeTruthy();
    expect(screen.getByText("workflow.executed")).toBeTruthy();
    expect(screen.getByText("run run12345")).toBeTruthy();
  });

  it("updates the account from the header controls", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["POST /v1/work/nodes/n1/account"] = {
      status: 200,
      body: workNode().account,
    };
    const onChanged = vi.fn();
    render(<NodeThread node={workNode()} onChanged={onChanged} />);

    fireEvent.click(screen.getByLabelText(/Data may train auto-development/));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const patch = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/work/nodes/n1/account",
    );
    expect(patch?.body).toEqual({ allow_autodev_data: false });
  });

  it("audit nodes surface pending commits and commit them manually", async () => {
    const audited = workNode({
      account: { ...workNode().account, audit_mode: true },
    });
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/runs/contract/holds"] = {
      status: 200,
      body: {
        items: [
          {
            pending_id: "p1",
            name: "clean-the-books",
            reserved: ["audit-node:n1"],
            submitted_by: "consumer",
            created_at: "t",
            expires_at: null,
          },
          {
            pending_id: "p2",
            name: "other",
            reserved: ["cli/apply"],
            submitted_by: "someone",
            created_at: "t",
            expires_at: null,
          },
        ],
      },
    };
    routes["POST /v1/runs/contract/holds/p1"] = { status: 200, body: {} };
    render(<NodeThread node={audited} onChanged={vi.fn()} />);

    expect(await screen.findByText(/clean-the-books/)).toBeTruthy();
    expect(screen.queryByText(/other · from someone/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Commit" }));

    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" &&
            c.path === "/v1/runs/contract/holds/p1" &&
            (c.body as { approved: boolean }).approved === true,
        ),
      ).toBe(true),
    );
  });
});
