import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Life, NoderThread } from "./Life";

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

const RUN = {
  run_id: "r1",
  intent: "convert report to pdf",
  phase: "completed",
  awaiting: null,
};

describe("Life", () => {
  it("opens on the OoLu conversation and can switch into Work", async () => {
    routes["GET /v1/work/nodes"] = { status: 200, body: { items: [] } };
    render(<Life />);
    expect(screen.getByRole("button", { name: "Life" })).toBeTruthy();
    // The OoLu chat is the open pane.
    expect(screen.getByPlaceholderText("Message OoLu…")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Work" }));
    expect(await screen.findByText("My nodes")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Life" }));
    expect(screen.getByPlaceholderText("Message OoLu…")).toBeTruthy();
  });

  it("lists node interactions under Noder and opens the log thread", async () => {
    routes["GET /v1/runs"] = { status: 200, body: { items: [RUN] } };
    routes["GET /v1/runs/r1/audit"] = {
      status: 200,
      body: {
        entries: [
          { at: "2026-07-06T10:00:00Z", event_type: "run.submitted", seq: 1 },
          { at: "2026-07-06T10:00:02Z", event_type: "route.chosen", seq: 2 },
        ],
      },
    };
    render(<Life />);

    fireEvent.click(await screen.findByText("convert report to pdf"));

    expect(await screen.findByText("run.submitted")).toBeTruthy();
    expect(screen.getByText("route.chosen")).toBeTruthy();
    // The messy log is labelled as such, with OoLu as the human path.
    expect(screen.getByText(/ask OoLu/i)).toBeTruthy();
  });

  it("shows the Friends placeholder pane", async () => {
    render(<Life />);
    fireEvent.click(screen.getByText("No conversations yet"));
    expect(
      await screen.findByText(/people and businesses will live here/i),
    ).toBeTruthy();
  });
});

describe("NoderThread", () => {
  it("re-triggers the node's work with the same intent", async () => {
    routes["GET /v1/runs/r1/audit"] = { status: 200, body: { entries: [] } };
    routes["POST /v1/runs"] = {
      status: 202,
      body: { ...RUN, run_id: "r2", phase: "submitted" },
    };
    const onRunAgain = vi.fn();
    render(<NoderThread run={RUN} onRunAgain={onRunAgain} />);

    fireEvent.click(screen.getByRole("button", { name: "Run again" }));

    expect(await screen.findByText(/Triggered again/)).toBeTruthy();
    const post = calls.find((c) => c.method === "POST" && c.path === "/v1/runs");
    expect(post?.body).toEqual({ intent: "convert report to pdf" });
    expect(onRunAgain).toHaveBeenCalled();
  });
});
