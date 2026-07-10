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
      value: "plus",
      choices: ["free", "plus", "pro", "enterprise"],
      managed: true,
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

const WALLET = {
  mode: "test",
  default_pm: "pm_1",
  cards: [
    { pm_ref: "pm_1", brand: "visa", last4: "4242", exp_month: 7, exp_year: 2029 },
  ],
};

const STATUS = {
  open: false,
  mode: "pre_launch",
  vault_mode: "test",
  reasons: ["pre-launch: the real transaction port is not opened"],
};

describe("PaymentSection", () => {
  it("shows saved test cards with the pre-launch banner and reasons", async () => {
    routes["GET /v1/settings"] = { status: 200, body: CATALOG };
    routes["GET /v1/payment-methods"] = { status: 200, body: WALLET };
    routes["GET /v1/payments/status"] = { status: 200, body: STATUS };
    render(<SettingsPane />);

    expect(await screen.findByText(/visa •••• 4242/)).toBeTruthy();
    expect(screen.getByText(/real transaction port is closed/)).toBeTruthy();
    expect(screen.getByText(/Charging opens when/)).toBeTruthy();
  });

  it("adds a named test card — no field carries a number", async () => {
    routes["GET /v1/settings"] = { status: 200, body: CATALOG };
    routes["GET /v1/payment-methods"] = {
      status: 200,
      body: { ...WALLET, cards: [], default_pm: null },
    };
    routes["GET /v1/payments/status"] = { status: 200, body: STATUS };
    routes["POST /v1/payment-methods"] = {
      status: 201,
      body: { pm_ref: "pm_9", brand: "mastercard", last4: "4444", mode: "test" },
    };
    render(<SettingsPane />);

    const brand = (await screen.findByLabelText(
      "Test card brand",
    )) as HTMLSelectElement;
    fireEvent.change(brand, { target: { value: "mastercard" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.path === "/v1/payment-methods",
      );
      expect(post?.body).toEqual({ brand: "mastercard" });
    });
    // There is no card-number input anywhere in the pane.
    expect(document.querySelector('input[autocomplete="cc-number"]')).toBeNull();
  });
});

const MODEL_CATALOG = {
  items: [
    ...CATALOG.items,
    {
      key: "model.provider",
      group: "model",
      label: "Model provider",
      kind: "choice",
      description: "Which provider answers chat.",
      value: "auto",
      choices: ["auto", "anthropic", "openai"],
    },
  ],
};

describe("ModelKeysSection", () => {
  it("pastes a key in, gets only a fingerprint back", async () => {
    routes["GET /v1/settings"] = { status: 200, body: MODEL_CATALOG };
    routes["GET /v1/keys/model"] = { status: 200, body: { items: [] } };
    routes["POST /v1/keys/model"] = {
      status: 201,
      body: {
        provider: "anthropic",
        fingerprint: "ab12cd34ef56",
        source_switched: true,
      },
    };
    routes["POST /v1/keys/model/test"] = {
      status: 200,
      body: { ok: true, reply: "pong", source: "own-api" },
    };
    render(<SettingsPane />);

    const input = (await screen.findByLabelText("API key")) as HTMLInputElement;
    expect(input.type).toBe("password"); // never shown in the clear
    expect(
      screen.getByText(/OoLu answers with its built-in rules/),
    ).toBeTruthy();

    fireEvent.change(input, { target: { value: "sk-ant-0123456789" } });
    routes["GET /v1/keys/model"] = {
      status: 200,
      body: {
        items: [
          {
            provider: "anthropic",
            fingerprint: "ab12cd34ef56",
            added_at: "2026-07-07T10:00:00Z",
          },
        ],
      },
    };
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    // The exact key went to the one door in; the pane then shows only
    // the fingerprint and the input is emptied.
    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && c.path === "/v1/keys/model",
      );
      expect(post?.body).toEqual({
        provider: "anthropic",
        key: "sk-ant-0123456789",
      });
    });
    expect(await screen.findByText(/fingerprint ab12cd34ef56/)).toBeTruthy();
    expect(input.value).toBe("");
    // Adding a key immediately proves it works, and says it's now the
    // default model — no more "billed but is it working?" mystery.
    expect(await screen.findByText(/✓ working/)).toBeTruthy();
    expect(
      screen.getByText(/anthropic key is now the default model/),
    ).toBeTruthy();
  });

  it("tests a stored key and reports a clear pass or fail", async () => {
    routes["GET /v1/settings"] = { status: 200, body: MODEL_CATALOG };
    routes["GET /v1/keys/model"] = {
      status: 200,
      body: {
        items: [
          {
            provider: "openai",
            fingerprint: "99ff00aa11bb",
            added_at: "2026-07-07T10:00:00Z",
          },
        ],
      },
    };
    routes["POST /v1/keys/model/test"] = {
      status: 200,
      body: { ok: false, error: "openai: status 401" },
    };
    render(<SettingsPane />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Test connection" }),
    );
    expect(await screen.findByText(/✗ openai: status 401/)).toBeTruthy();
  });

  it("removes a stored key", async () => {
    routes["GET /v1/settings"] = { status: 200, body: MODEL_CATALOG };
    routes["GET /v1/keys/model"] = {
      status: 200,
      body: {
        items: [
          {
            provider: "openai",
            fingerprint: "99ff00aa11bb",
            added_at: "2026-07-07T10:00:00Z",
          },
        ],
      },
    };
    routes["DELETE /v1/keys/model/openai"] = {
      status: 200,
      body: { removed: "openai" },
    };
    render(<SettingsPane />);

    fireEvent.click(await screen.findByRole("button", { name: "remove" }));

    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.method === "DELETE" && c.path === "/v1/keys/model/openai",
        ),
      ).toBe(true);
    });
  });
});

describe("SettingsPane", () => {
  it("renders controls generated from the catalog, grouped", async () => {
    routes["GET /v1/settings"] = { status: 200, body: CATALOG };
    render(<SettingsPane />);

    expect(await screen.findByText("Theme")).toBeTruthy();
    expect(screen.getByText("App")).toBeTruthy();
    expect(screen.getByText("Budget")).toBeTruthy();
    // The subscription is a commitment: display-only, console-managed.
    expect(screen.getByText(/managed in the account console/)).toBeTruthy();
  });

  it("shows the plan without offering a knob, and opens the console", async () => {
    routes["GET /v1/settings"] = { status: 200, body: CATALOG };
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    sessionStorage.setItem("oolu_engine_token", "engine-tok");
    try {
      const { container } = render(<SettingsPane />);

      // The managed plan renders as text — there is no control to change it.
      const shown = await screen.findByText("plus", {
        selector: ".managed-value",
      });
      expect(shown).toBeTruthy();
      expect(screen.queryByLabelText("Plan")).toBeNull();
      expect(container.querySelector("select[aria-label='Plan']")).toBeNull();

      fireEvent.click(
        screen.getByRole("button", { name: "Open the account console" }),
      );
      // Local mode: this gateway's console, engine token handed over in
      // the hash (the console scrubs it on load).
      expect(String(opened.mock.calls[0][0])).toBe(
        "http://local.test/account#auth=engine-tok",
      );
    } finally {
      sessionStorage.clear();
    }
  });

  it("explains the model section: plan, own API override, local", async () => {
    routes["GET /v1/settings"] = {
      status: 200,
      body: {
        items: [
          {
            key: "model.source",
            group: "model",
            label: "Default model",
            kind: "choice",
            description: "Where the brain lives.",
            value: "subscription",
            choices: ["subscription", "own-api", "local"],
          },
          {
            key: "model.local_url",
            group: "model",
            label: "Local model URL",
            kind: "text",
            description: "OpenAI-compatible endpoint.",
            value: "http://127.0.0.1:11434/v1",
            max_length: 200,
          },
        ],
      },
    };
    render(<SettingsPane />);

    // One clear section: the source dial, the local endpoint, and the
    // words that say own API overrides the plan's Claude-first default.
    expect(await screen.findByLabelText("Default model")).toBeTruthy();
    expect(screen.getByLabelText("Local model URL")).toBeTruthy();
    expect(screen.getByText(/override the plan/)).toBeTruthy();
    expect(screen.getByText(/no key, no cloud/)).toBeTruthy();
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
