import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { SettingsPane } from "./SettingsPane";

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

const CATALOG = {
  items: [
    {
      key: "app.theme",
      group: "app",
      label: "Theme",
      kind: "choice",
      description: "Colour theme.",
      value: "system",
      choices: ["system", "light", "dark"],
    },
    {
      key: "subscription.plan",
      group: "subscription",
      label: "Plan",
      kind: "choice",
      description: "Tier.",
      value: "free",
      choices: ["free", "plus", "pro", "enterprise"],
    },
    {
      key: "budget.hard_cap",
      group: "budget",
      label: "Hard spending cap",
      kind: "number",
      description: "Refuse above this.",
      value: 0,
      minimum: 0,
      maximum: 100000,
    },
  ],
};

describe("SettingsPane", () => {
  it("renders controls generated from the catalog, grouped", async () => {
    routes["GET /v1/settings"] = { status: 200, body: CATALOG };
    render(<SettingsPane />);

    expect(await screen.findByText("Theme")).toBeTruthy();
    expect(screen.getByText("App")).toBeTruthy();
    expect(screen.getByText("Budget")).toBeTruthy();
    // Subscription note is honest about billing not existing.
    expect(screen.getByText(/Billing isn't enabled yet/)).toBeTruthy();
    // The plan choices are exactly the declared closed set.
    const plan = screen.getByLabelText("Plan") as HTMLSelectElement;
    expect([...plan.options].map((o) => o.value)).toEqual([
      "free",
      "plus",
      "pro",
      "enterprise",
    ]);
  });

  it("saves a change through the node", async () => {
    routes["GET /v1/settings"] = { status: 200, body: CATALOG };
    routes["PUT /v1/settings"] = { status: 200, body: CATALOG };
    render(<SettingsPane />);

    const cap = (await screen.findByLabelText(
      "Hard spending cap",
    )) as HTMLInputElement;
    fireEvent.change(cap, { target: { value: "50" } });
    fireEvent.blur(cap);

    await waitFor(() => {
      const put = calls.find((c) => c.method === "PUT");
      expect(put?.body).toEqual({ changes: { "budget.hard_cap": 50 } });
    });
  });

  it("surfaces a node refusal instead of accepting it silently", async () => {
    routes["GET /v1/settings"] = { status: 200, body: CATALOG };
    routes["PUT /v1/settings"] = {
      status: 400,
      body: { error: { message: "budget.hard_cap must be at most 100000.0" } },
    };
    render(<SettingsPane />);

    const cap = (await screen.findByLabelText(
      "Hard spending cap",
    )) as HTMLInputElement;
    fireEvent.change(cap, { target: { value: "999999999" } });
    fireEvent.blur(cap);

    expect(await screen.findByText(/must be at most/)).toBeTruthy();
  });
});
