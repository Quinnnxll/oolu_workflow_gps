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
      authority_level: null,
      is_supernode: false,
      supernode_id: null,
      status: "live",
      audit_mode: false,
      allow_autodev_data: true,
    },
    earnings_micros: 12_340_000,
    health: { verified_successes: 9, verified_failures: 1, score: 0.9 },
    ...overrides,
  };
}

function supernode(): WorkNode {
  return workNode({
    node_id: "sn1",
    title: "Finance Division",
    account: {
      ...workNode().account,
      node_id: "sn1",
      is_supernode: true,
      audit_mode: true,
    },
  });
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

  it("has no auto-build checkbox — that consent lives in Settings now", async () => {
    routes["GET /v1/work/nodes"] = { status: 200, body: { items: [] } };
    render(<Work onLife={vi.fn()} />);

    expect(await screen.findByText(/No nodes yet/)).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByText(/auto-build/i)).toBeNull();
  });
});

describe("AddNode", () => {
  it("fixes the regime at creation: supernode membership, audit, auto-grow", async () => {
    routes["POST /v1/nodeplace"] = { status: 201, body: { node_id: "n9" } };
    routes["POST /v1/work/nodes/n9/account"] = {
      status: 200,
      body: workNode().account,
    };
    const onDone = vi.fn();
    render(<AddNode supernodes={[supernode()]} onDone={onDone} />);

    expect(screen.getByText(/fixed at creation/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Tax Filer" },
    });
    // Under the owned Supernode, with authority L4.
    fireEvent.change(screen.getByLabelText("Under Supernode"), {
      target: { value: "sn1" },
    });
    fireEvent.change(screen.getByLabelText("Authority"), {
      target: { value: "4" },
    });
    fireEvent.click(
      screen.getByLabelText(/Audit node — every request must be committed/),
    );
    fireEvent.click(screen.getByLabelText(/Auto-growing/));
    fireEvent.click(screen.getByRole("button", { name: "Create node" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("n9"));
    const account = calls.find((c) => c.path === "/v1/work/nodes/n9/account");
    expect(account?.body).toEqual({
      is_supernode: false,
      supernode_id: "sn1",
      audit_mode: true,
      allow_autodev_data: false,
      authority_level: 4,
    });
  });

  it("a Supernode always audits — the checkbox locks on", () => {
    render(<AddNode supernodes={[]} onDone={vi.fn()} />);

    fireEvent.click(screen.getByLabelText(/Supernode — manages many nodes/));
    const audit = screen.getByLabelText(
      /Audit node — every request must be committed/,
    ) as HTMLInputElement;
    expect(audit.checked).toBe(true);
    expect(audit.disabled).toBe(true);
  });

  it("onboarding offers no choices at all", async () => {
    routes["POST /v1/work/nodes/n7/account"] = {
      status: 200,
      body: workNode().account,
    };
    const onDone = vi.fn();
    render(<AddNode supernodes={[]} onDone={onDone} />);

    fireEvent.click(screen.getByRole("button", { name: "Onboard existing" }));
    expect(screen.queryByLabelText(/Audit node/)).toBeNull();
    expect(screen.queryByLabelText(/Auto-growing/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Node id"), {
      target: { value: "n7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Onboard" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("n7"));
    const account = calls.find((c) => c.path === "/v1/work/nodes/n7/account");
    expect(account?.body).toEqual({ onboard: true });
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
    render(
      <NodeThread node={workNode()} allNodes={[workNode()]} onChanged={vi.fn()} />,
    );

    // Function words, not event codes — the raw type stays in the tooltip.
    expect(await screen.findByText("Started working")).toBeTruthy();
    expect(screen.getByText("Carried out the actions")).toBeTruthy();
    expect(screen.getByTitle("workflow.executed")).toBeTruthy();
    expect(screen.getByText("run run12345")).toBeTruthy();
  });

  it("the regime is badges, never knobs", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    render(
      <NodeThread node={workNode()} allNodes={[workNode()]} onChanged={vi.fn()} />,
    );

    expect(
      await screen.findByText("no authority (standalone)"),
    ).toBeTruthy();
    expect(screen.getByText("Unattended runs allowed")).toBeTruthy();
    expect(
      screen.getByText("Auto-growing: data may feed development"),
    ).toBeTruthy();
    // No checkbox or select can touch the fixed regime.
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("a Supernode manages member authority and sees member holds", async () => {
    const sn = supernode();
    const member = workNode({
      node_id: "n2",
      title: "Tax Filer",
      account: {
        ...workNode().account,
        node_id: "n2",
        supernode_id: "sn1",
        authority_level: 2,
      },
    });
    routes["GET /v1/work/nodes/sn1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/runs/contract/holds"] = {
      status: 200,
      body: {
        items: [
          {
            pending_id: "p9",
            name: "file-the-taxes",
            reserved: ["audit-node:n2"],
            submitted_by: "consumer",
            created_at: "t",
            expires_at: null,
            replies: [],
          },
        ],
      },
    };
    routes["POST /v1/work/nodes/n2/account"] = {
      status: 200,
      body: member.account,
    };
    render(
      <NodeThread node={sn} allNodes={[sn, member]} onChanged={vi.fn()} />,
    );

    // The member's hold surfaces on the Supernode's desk.
    expect(await screen.findByText(/file-the-taxes/)).toBeTruthy();
    // And the member's authority is the Supernode humans' dial.
    fireEvent.change(screen.getByLabelText("Authority for Tax Filer"), {
      target: { value: "5" },
    });
    await waitFor(() => {
      const patch = calls.find(
        (c) => c.method === "POST" && c.path === "/v1/work/nodes/n2/account",
      );
      expect(patch?.body).toEqual({ authority_level: 5 });
    });
  });

  it("a held request can be allowed, signed, or replied to", async () => {
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
            replies: [],
          },
        ],
      },
    };
    routes["POST /v1/runs/contract/holds/p1"] = { status: 200, body: {} };
    routes["POST /v1/runs/contract/holds/p1/reply"] = {
      status: 200,
      body: { replies: [{ author: "alice", message: "why?", at: "t" }] },
    };
    render(
      <NodeThread node={audited} allNodes={[audited]} onChanged={vi.fn()} />,
    );
    await screen.findByText(/clean-the-books/);

    // Reply: type and send, deciding nothing.
    fireEvent.change(screen.getByLabelText("Reply to clean-the-books"), {
      target: { value: "why?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reply" }));
    await waitFor(() =>
      expect(
        calls.some((c) => c.path === "/v1/runs/contract/holds/p1/reply"),
      ).toBe(true),
    );

    // Sign & allow: the typed signature rides the approval.
    fireEvent.change(screen.getByLabelText("Sign for clean-the-books"), {
      target: { value: "Quinn M." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign & allow" }));
    await waitFor(() => {
      const decide = calls.find(
        (c) => c.method === "POST" && c.path === "/v1/runs/contract/holds/p1",
      );
      expect(decide?.body).toEqual({ approved: true, signature: "Quinn M." });
    });
  });
});
