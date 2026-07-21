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

  it("marks an un-onboarded node and warns to keep its id private", async () => {
    const unclaimed = workNode({
      account: {
        ...workNode().account,
        responsible: "",
        admin: null,
        supernode_id: "sn1",
        authority_level: 4,
      },
    });
    routes["GET /v1/work/nodes"] = {
      status: 200,
      body: { items: [unclaimed] },
    };
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    render(<Work onLife={vi.fn()} />);

    fireEvent.click(await screen.findByText("Invoice Cleaner"));
    // The thread says no one answers yet — never an empty "responsible".
    expect(await screen.findByText(/not onboarded yet/)).toBeTruthy();
    // ...and warns that the node id is the claim ticket.
    expect(
      screen.getByText(/Do not show its node id publicly/),
    ).toBeTruthy();
    expect(screen.getByText(/No one answers for this node yet/)).toBeTruthy();
  });

  it("shows the responsible user ID once the node is onboarded", async () => {
    routes["GET /v1/work/nodes"] = {
      status: 200,
      body: { items: [workNode()] },
    };
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    render(<Work onLife={vi.fn()} />);

    fireEvent.click(await screen.findByText("Invoice Cleaner"));
    expect(await screen.findByText(/responsible alice/)).toBeTruthy();
    expect(screen.queryByText(/Do not show its node id publicly/)).toBeNull();
  });

  it("shows a reviewer the KYC queue and clears a row on the verdict", async () => {
    routes["GET /v1/work/nodes"] = { status: 200, body: { items: [] } };
    routes["GET /v1/kyc/reviews"] = {
      status: 200,
      body: {
        items: [
          {
            node_id: "sn1",
            legal_name: "Mphepo Ltd",
            company_email: "quinn@mphepo.io",
            registration_no: "12345",
            screen: "fast_track",
            screen_note: "trusted",
            status: "pending_review",
            decision_note: "",
            multiplier: 1.0,
          },
        ],
      },
    };
    render(<Work onLife={vi.fn()} />);

    expect(
      await screen.findByText(/KYC reviews awaiting your verdict \(1\)/),
    ).toBeTruthy();
    expect(screen.getByText(/Mphepo Ltd · quinn@mphepo.io · reg 12345/)).toBeTruthy();
    expect(screen.getByText(/fast lane — trusted domain/)).toBeTruthy();

    routes["POST /v1/work/nodes/sn1/kyc/decide"] = {
      status: 200,
      body: { status: "verified" },
    };
    routes["GET /v1/kyc/reviews"] = { status: 200, body: { items: [] } };
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(screen.queryByText(/KYC reviews awaiting/)).toBeNull(),
    );
    const decide = calls.find(
      (c) => c.path === "/v1/work/nodes/sn1/kyc/decide",
    );
    expect(decide?.body).toEqual({ approved: true });
  });

  it("shows no KYC queue to accounts the host refuses (403)", async () => {
    routes["GET /v1/work/nodes"] = { status: 200, body: { items: [] } };
    routes["GET /v1/kyc/reviews"] = {
      status: 403,
      body: { error: { message: "forbidden" } },
    };
    render(<Work onLife={vi.fn()} />);

    expect(await screen.findByText(/No nodes yet/)).toBeTruthy();
    expect(screen.queryByText(/KYC reviews awaiting/)).toBeNull();
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
    render(<AddNode onDone={onDone} />);

    expect(screen.getByText(/fixed at creation/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Tax Filer" },
    });
    // Membership moved to the Supernode's own Access desk: the + makes
    // standalone nodes only.
    expect(screen.queryByLabelText("Under Supernode")).toBeNull();
    fireEvent.click(
      screen.getByLabelText(/Audit node — every request must be committed/),
    );
    fireEvent.click(screen.getByLabelText(/Auto-growing/));

    // The Node Policy is agreed UPFRONT or nothing is created at all.
    fireEvent.click(screen.getByRole("button", { name: "Create node" }));
    expect(
      await screen.findByText(/Please agree to the Node Policy first/),
    ).toBeTruthy();
    expect(calls.find((c) => c.path === "/v1/nodeplace")).toBeUndefined();

    fireEvent.click(screen.getByLabelText(/I agree to the Node Policy/));
    fireEvent.click(screen.getByRole("button", { name: "Create node" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("n9"));
    const account = calls.find((c) => c.path === "/v1/work/nodes/n9/account");
    expect(account?.body).toEqual({
      is_supernode: false,
      supernode_id: null,
      audit_mode: true,
      allow_autodev_data: false,
      authority_level: null,
      accept_policy: true,
    });
  });

  it("carries a developer's uploaded function into the created node", async () => {
    routes["POST /v1/nodeplace"] = { status: 201, body: { node_id: "n5" } };
    routes["POST /v1/work/nodes/n5/account"] = {
      status: 200,
      body: workNode().account,
    };
    const onDone = vi.fn();
    render(<AddNode onDone={onDone} />);

    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "CSV Normalizer" },
    });
    fireEvent.change(
      screen.getByLabelText(/Function \(optional — bring your own code\)/),
      {
        target: {
          value: "from _oolu_runtime import emit_result\nemit_result('ok')",
        },
      },
    );
    fireEvent.click(screen.getByLabelText(/I agree to the Node Policy/));
    fireEvent.click(screen.getByRole("button", { name: "Create node" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("n5"));
    const contribute = calls.find((c) => c.path === "/v1/nodeplace");
    const skill = (contribute?.body as { skill: Record<string, unknown> })
      .skill;
    // The node is born a SCRIPT node carrying the developer's function.
    expect((skill.signature as { adapter: string }).adapter).toBe("script");
    const action = (skill.actions as { adapter: string; parameters: { script: string } }[])[0];
    expect(action.adapter).toBe("script");
    expect(action.parameters.script).toContain("emit_result('ok')");
  });

  it("a Supernode always audits — the checkbox locks on", () => {
    render(<AddNode onDone={vi.fn()} />);

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
    render(<AddNode onDone={onDone} />);

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
      <NodeThread node={workNode()} allNodes={[workNode()]} />,
    );

    // Function words, not event codes — the full detail (time, raw type,
    // run id) stays in the tooltip and the daily log files.
    expect(await screen.findByText("Started working")).toBeTruthy();
    expect(screen.getByText("Carried out the actions")).toBeTruthy();
    expect(
      screen.getByTitle("t2 · workflow.executed · run run12345678"),
    ).toBeTruthy();
    expect(screen.getByText("run run12345")).toBeTruthy();
  });

  it("egress consent: grant a host, see it listed, withdraw it again", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["POST /v1/work/nodes/n1/account"] = {
      status: 200,
      body: { ...workNode().account, network_hosts: ["api.example.com"] },
    };
    render(<NodeThread node={workNode()} allNodes={[workNode()]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));

    // Fail closed is spoken, not implied: no grant means no web at all.
    expect(await screen.findByText("Network access")).toBeTruthy();
    expect(screen.getByText(/No hosts granted/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Host to grant"), {
      target: { value: "API.example.com" },
    });
    fireEvent.click(screen.getByText("Grant host"));

    expect(await screen.findByText("api.example.com")).toBeTruthy();
    const post = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/work/nodes/n1/account",
    );
    // Lowercased, and the body carries ONLY the grant — never the regime.
    expect(post?.body).toEqual({ network_hosts: ["api.example.com"] });

    routes["POST /v1/work/nodes/n1/account"] = {
      status: 200,
      body: { ...workNode().account, network_hosts: [] },
    };
    fireEvent.click(screen.getByText("Withdraw"));
    expect(await screen.findByText(/No hosts granted/)).toBeTruthy();
  });

  it("the regime is one concise tag — silent about what the node isn't", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    // Auto-growing ON, audit off: only Auto-growing is mentioned.
    render(
      <NodeThread node={workNode()} allNodes={[workNode()]} />,
    );
    expect(await screen.findByText("(Auto-growing)")).toBeTruthy();
    // No checkbox or select can touch the fixed regime.
    expect(screen.queryByRole("checkbox")).toBeNull();
    cleanup();

    // L4 + audit, auto-growing OFF: "(L4, Audit)" and not a word more.
    const strict = workNode({
      account: {
        ...workNode().account,
        supernode_id: "sn1",
        authority_level: 4,
        audit_mode: true,
        allow_autodev_data: false,
      },
    });
    routes["GET /v1/runs/contract/holds"] = { status: 200, body: { items: [] } };
    render(
      <NodeThread node={strict} allNodes={[strict]} />,
    );
    expect(await screen.findByText("(L4, Audit)")).toBeTruthy();
    expect(screen.queryByText(/auto-grow/i)).toBeNull();
  });

  it("a Supernode can be created under a Supernode, from the Access desk", async () => {
    const sn = supernode();
    routes["GET /v1/work/nodes/sn1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/runs/contract/holds"] = { status: 200, body: { items: [] } };
    routes["GET /v1/work/nodes/sn1/kyc"] = {
      status: 200,
      body: { application: null, trust_multiplier: 1.0, required: false },
    };
    routes["POST /v1/nodeplace"] = { status: 201, body: { node_id: "n8" } };
    routes["POST /v1/work/nodes/n8/account"] = {
      status: 200,
      body: workNode().account,
    };
    render(<NodeThread node={sn} allNodes={[sn]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));

    fireEvent.change(
      await screen.findByLabelText("new member node name"),
      { target: { value: "Tax Office" } },
    );
    fireEvent.click(screen.getByLabelText("Supernode member"));
    fireEvent.change(screen.getByLabelText("Authority"), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create member" }));

    await waitFor(() =>
      expect(calls.find((c) => c.path === "/v1/nodeplace")).toBeTruthy(),
    );
    const account = calls.find((c) => c.path === "/v1/work/nodes/n8/account");
    expect(account?.body).toEqual({
      is_supernode: true,
      supernode_id: "sn1",
      // Only the org's ROOT Supernode always audits — a nested one
      // takes the creator's choice (Issue 15), default off.
      audit_mode: false,
      allow_autodev_data: true,
      authority_level: 3,
      accept_policy: true,
    });
  });

  it("a Supernode sees member holds; member authority is display-only", async () => {
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
    render(
      <NodeThread node={sn} allNodes={[sn, member]} />,
    );

    // The member's hold surfaces on the Supernode's desk.
    expect(await screen.findByText(/file-the-taxes/)).toBeTruthy();
    // But the member's authority is fixed at creation — even for the
    // Supernode's humans it is a tag to read, never a dial to turn.
    fireEvent.click(screen.getByRole("button", { name: "Access" }));
    expect(await screen.findByText("(L2, Auto-growing)")).toBeTruthy();
    expect(screen.queryByLabelText("Authority for Tax Filer")).toBeNull();
    expect(
      calls.find(
        (c) => c.method === "POST" && c.path === "/v1/work/nodes/n2/account",
      ),
    ).toBeUndefined();
  });

  it("a Supernode applies for KYC; a personal mailbox is refused in words", async () => {
    const sn = supernode();
    routes["GET /v1/work/nodes/sn1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/runs/contract/holds"] = { status: 200, body: { items: [] } };
    routes["GET /v1/work/nodes/sn1/kyc"] = {
      status: 200,
      body: { application: null, trust_multiplier: 1.0 },
    };
    routes["POST /v1/work/nodes/sn1/kyc"] = {
      status: 400,
      body: {
        error: {
          message:
            "a company mailbox is required — gmail.com is a personal mailbox provider",
        },
      },
    };
    render(<NodeThread node={sn} allNodes={[sn]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));

    expect(await screen.findByText("KYC — legal entity")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Legal entity name"), {
      target: { value: "Mphepo Ltd" },
    });
    fireEvent.change(screen.getByLabelText("Company email"), {
      target: { value: "quinn@gmail.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(
      await screen.findByText(/personal mailbox provider/),
    ).toBeTruthy();
  });

  it("a verified Supernode wears the global trust badge, no form", async () => {
    const sn = supernode();
    routes["GET /v1/work/nodes/sn1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/runs/contract/holds"] = { status: 200, body: { items: [] } };
    routes["GET /v1/work/nodes/sn1/kyc"] = {
      status: 200,
      body: {
        application: {
          node_id: "sn1",
          legal_name: "Mphepo Ltd",
          company_email: "quinn@mphepo.io",
          registration_no: "",
          screen: "fast_track",
          screen_note: "",
          status: "verified",
          decision_note: "",
          multiplier: 1.5,
        },
        trust_multiplier: 1.5,
      },
    };
    render(<NodeThread node={sn} allNodes={[sn]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));

    expect(
      await screen.findByText(/KYC verified · global trust ×1.5/),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("reads the activity feed in human words: node name, seconds, plan status", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: {
        items: [
          {
            run_id: "r1234567890",
            gross: 0.5,
            node_title: "Tax Filer",
            steps: [
              {
                seq: 1,
                event_type: "workflow.executed",
                at: "2026-07-10T10:00:02.123456+00:00",
              },
            ],
          },
        ],
      },
    };
    render(<NodeThread node={workNode()} allNodes={[workNode()]} />);

    // The executing node by NAME, never a raw run id...
    expect(await screen.findByText("Tax Filer")).toBeTruthy();
    expect(screen.queryByText(/run r1234567/)).toBeNull();
    // ...the clock down to the second, not the ISO blob...
    expect(screen.getByText("10:00:02")).toBeTruthy();
    // ...and the step in plan/status words, not the function call.
    expect(screen.getByText("Carried out the actions")).toBeTruthy();
    expect(screen.queryByText("workflow.executed")).toBeNull();
  });

  it("shows no KYC block at all on an Edge install", async () => {
    const sn = supernode();
    routes["GET /v1/work/nodes/sn1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/runs/contract/holds"] = { status: 200, body: { items: [] } };
    routes["GET /v1/work/nodes/sn1/kyc"] = {
      status: 200,
      // Edge: KYC does not bind and no subscription is required.
      body: { application: null, trust_multiplier: 1.0, required: false },
    };
    render(<NodeThread node={sn} allNodes={[sn]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));

    // The thread settles with no KYC section, no form, no plan nag.
    expect(await screen.findByText(/Finance Division/)).toBeTruthy();
    await waitFor(() =>
      expect(
        calls.some((c) => c.path === "/v1/work/nodes/sn1/kyc"),
      ).toBe(true),
    );
    expect(screen.queryByText("KYC — legal entity")).toBeNull();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("member nodes fold away for a clear view", async () => {
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
    routes["GET /v1/runs/contract/holds"] = { status: 200, body: { items: [] } };
    routes["GET /v1/work/nodes/sn1/kyc"] = {
      status: 200,
      body: { application: null, trust_multiplier: 1.0, required: false },
    };
    render(<NodeThread node={sn} allNodes={[sn, member]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));

    // Open by default, with the count on the header.
    const header = await screen.findByRole("button", {
      name: /Member nodes \(1\)/,
    });
    expect(screen.getByText(/Tax Filer/)).toBeTruthy();

    fireEvent.click(header);
    expect(screen.queryByText(/Tax Filer/)).toBeNull();
    fireEvent.click(header);
    expect(screen.getByText(/Tax Filer/)).toBeTruthy();
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
      <NodeThread node={audited} allNodes={[audited]} />,
    );
    await screen.findByText(/clean-the-books/);

    // Reply: type and press Enter — deciding nothing.
    fireEvent.change(screen.getByLabelText("Reply to clean-the-books"), {
      target: { value: "why?" },
    });
    fireEvent.keyDown(screen.getByLabelText("Reply to clean-the-books"), {
      key: "Enter",
    });
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

describe("Imitate: the guided lesson on the tab row", () => {
  it("teaches step by step and builds from the demonstration", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/work/nodes/n1/imitate"] = {
      status: 200,
      body: { lesson: null },
    };
    const recording = {
      lesson_id: "l1",
      node_id: "n1",
      goal: "normalize supplier invoices",
      status: "recording",
      created_at: "2026-07-15T10:00:00+00:00",
      ended_at: null,
      built_node_id: "",
      steps: [] as unknown[],
    };
    routes["POST /v1/work/nodes/n1/imitate"] = {
      status: 201,
      body: { lesson: recording },
    };
    routes["POST /v1/work/nodes/n1/imitate/step"] = {
      status: 200,
      body: {
        lesson: {
          ...recording,
          steps: [
            { seq: 1, kind: "say", text: "download the csvs", at: "t" },
          ],
        },
      },
    };
    routes["POST /v1/work/nodes/n1/imitate/stop"] = {
      status: 200,
      body: {
        lesson: { ...recording, status: "built", built_node_id: "new9" },
        say: "Built a NEW node “Normalize Supplier Invoices” (new9) …",
      },
    };
    render(<NodeThread node={workNode()} allNodes={[workNode()]} />);

    // The button rides the Activity/Interact/Files row.
    fireEvent.click(await screen.findByRole("button", { name: /Imitate/ }));
    // The honest capability note IS the guidance: no screen recording.
    expect(
      await screen.findByText(/no screen or key recording/i),
    ).toBeTruthy();

    fireEvent.change(screen.getByLabelText("what should the new node do?"), {
      target: { value: "normalize supplier invoices" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start the lesson" }));
    // Recording: the row's button now says so.
    expect(await screen.findByText(/Learning…/)).toBeTruthy();

    fireEvent.change(
      await screen.findByLabelText("describe the next step…"),
      { target: { value: "download the csvs" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Add step" }));
    expect(await screen.findByText("download the csvs")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Stop & build the node" }),
    );
    // The build's own words come back, and the lesson closes.
    expect(await screen.findByText(/Built a NEW node/)).toBeTruthy();
    const stop = calls.find((c) => c.path === "/v1/work/nodes/n1/imitate/stop");
    expect(stop?.body).toEqual({ build: true });
  });

  it("cannot build an empty lesson — showing comes first", async () => {
    routes["GET /v1/work/nodes/n1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    // A lesson already recording re-opens its panel on arrival.
    routes["GET /v1/work/nodes/n1/imitate"] = {
      status: 200,
      body: {
        lesson: {
          lesson_id: "l1",
          node_id: "n1",
          goal: "teach me",
          status: "recording",
          created_at: "2026-07-15T10:00:00+00:00",
          ended_at: null,
          built_node_id: "",
          steps: [],
        },
      },
    };
    render(<NodeThread node={workNode()} allNodes={[workNode()]} />);
    const build = (await screen.findByRole("button", {
      name: "Stop & build the node",
    })) as HTMLButtonElement;
    expect(build.disabled).toBe(true);
  });
});

describe("the SOP dial: a member's execution order", () => {
  it("shows the fleet's orders and lets the owner set one", async () => {
    routes["GET /v1/work/nodes/sn1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    const sn = supernode();
    const member = workNode({
      node_id: "m1",
      title: "Order Intake",
      account: {
        ...workNode().account,
        node_id: "m1",
        supernode_id: "sn1",
        authority_level: 1,
        exec_order: null,
      },
    });
    routes["POST /v1/work/nodes/m1/order"] = {
      status: 200,
      body: { ...member.account, exec_order: 2 },
    };
    render(<NodeThread node={sn} allNodes={[sn, member]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));

    // Unordered members read as on-demand — called whenever needed.
    expect(await screen.findByText("on demand")).toBeTruthy();
    const dial = screen.getByLabelText("Execution order for Order Intake");
    fireEvent.change(dial, { target: { value: "2" } });
    fireEvent.keyDown(dial, { key: "Enter" });

    expect(await screen.findByText("step 2")).toBeTruthy();
    const posted = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/work/nodes/m1/order",
    );
    expect(posted?.body).toEqual({ order: 2 });
  });

  it("a member's name is the door to its own node card", async () => {
    routes["GET /v1/work/nodes/sn1/activity"] = {
      status: 200,
      body: { items: [] },
    };
    const sn = supernode();
    const member = workNode({
      node_id: "m1",
      title: "Order Intake",
      account: {
        ...workNode().account,
        node_id: "m1",
        supernode_id: "sn1",
        authority_level: 1,
      },
    });
    const onOpenNode = vi.fn();
    render(
      <NodeThread node={sn} allNodes={[sn, member]} onOpenNode={onOpenNode} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Access" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /Order Intake/ }),
    );
    expect(onOpenNode).toHaveBeenCalledWith("m1");
  });
});
