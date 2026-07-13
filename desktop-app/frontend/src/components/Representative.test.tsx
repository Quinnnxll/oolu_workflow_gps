import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DraftsInbox, FriendThread } from "./Life";
import { RepresentativeSection } from "./SettingsPane";

// The representative in the UI: a settings section that speaks plainly,
// and a ✍ button in a friend thread whose suggestion is decided — sent,
// edited (through the decision, so the rewrite teaches), or discarded.

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

const STATUS = {
  mode: "draft",
  about: "",
  exchanges: 12,
  drafts_pending: 1,
  drafts_decided: 4,
  sent_unedited: 3,
  auto_sent: 0,
  accept_rate: 0.75,
  auto_earned: false,
  muted_peers: [] as string[],
  adapter: "base",
};

const THREAD = {
  peer: "bob",
  items: [
    {
      message_id: "m1",
      from: "bob",
      text: "can you review my PR?",
      file_id: null,
      at: "2026-07-12T10:00:00Z",
      mine: false,
      read: true,
    },
  ],
};

const SUGGESTION = {
  draft_id: "d1",
  conversation_id: "bob",
  inbound_text: "can you review my PR?",
  generated_text: "on it — will look today",
  status: "pending",
  final_text: null,
  adapter_version: "base",
};

describe("FriendThread with a representative", () => {
  it("hides the draft button when the representative is off", async () => {
    routes["GET /v1/representative"] = {
      status: 200,
      body: { ...STATUS, mode: "off" },
    };
    routes["GET /v1/friends/bob/messages"] = { status: 200, body: THREAD };
    render(<FriendThread peer="bob" onActivity={() => {}} />);
    expect(await screen.findByText("can you review my PR?")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Draft a reply in your voice" }),
    ).toBeNull();
  });

  it("drafts, shows the suggestion, and sending decides it", async () => {
    routes["GET /v1/representative"] = { status: 200, body: STATUS };
    routes["GET /v1/friends/bob/messages"] = { status: 200, body: THREAD };
    routes["POST /v1/representative/drafts"] = {
      status: 201,
      body: SUGGESTION,
    };
    routes["POST /v1/representative/drafts/d1"] = {
      status: 200,
      body: {
        ...SUGGESTION,
        status: "sent",
        final_text: SUGGESTION.generated_text,
        delivered: { message_id: "m2", at: "2026-07-12T10:01:00Z" },
      },
    };
    render(<FriendThread peer="bob" onActivity={() => {}} />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Draft a reply in your voice",
      }),
    );
    expect(await screen.findByText("on it — will look today")).toBeTruthy();
    expect(
      calls.some(
        (c) =>
          c.path === "/v1/representative/drafts" &&
          (c.body as { peer: string }).peer === "bob",
      ),
    ).toBe(true);

    fireEvent.click(
      screen.getByRole("button", { name: "Send drafted reply" }),
    );
    await screen.findByText("can you review my PR?"); // thread refreshed
    const decision = calls.find(
      (c) => c.path === "/v1/representative/drafts/d1",
    );
    expect((decision?.body as { action: string }).action).toBe("send");
  });

  it("editing routes the composer through the decision", async () => {
    routes["GET /v1/representative"] = { status: 200, body: STATUS };
    routes["GET /v1/friends/bob/messages"] = { status: 200, body: THREAD };
    routes["POST /v1/representative/drafts"] = {
      status: 201,
      body: SUGGESTION,
    };
    routes["POST /v1/representative/drafts/d1"] = {
      status: 200,
      body: { ...SUGGESTION, status: "edited", final_text: "on it — tomorrow" },
    };
    render(<FriendThread peer="bob" onActivity={() => {}} />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Draft a reply in your voice",
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const composer = screen.getByLabelText(
      "Message bob",
    ) as HTMLTextAreaElement;
    expect(composer.value).toBe("on it — will look today");
    fireEvent.change(composer, { target: { value: "on it — tomorrow" } });
    fireEvent.click(screen.getByRole("button", { name: "Send edited" }));

    await screen.findByText("can you review my PR?");
    const decision = calls.find(
      (c) => c.path === "/v1/representative/drafts/d1",
    );
    expect(decision?.body).toEqual({
      action: "edit",
      text: "on it — tomorrow",
    });
    // A plain friend-message send never happened.
    expect(
      calls.some((c) => c.path === "/v1/friends/bob/messages" && c.method === "POST"),
    ).toBe(false);
  });
});

describe("DraftsInbox", () => {
  it("lists pending drafts and deciding one refreshes the list", async () => {
    routes["GET /v1/representative/drafts"] = {
      status: 200,
      body: { items: [SUGGESTION] },
    };
    routes["POST /v1/representative/drafts/d1"] = {
      status: 200,
      body: { ...SUGGESTION, status: "sent" },
    };
    const activity = vi.fn();
    render(<DraftsInbox onActivity={activity} onOpenThread={() => {}} />);
    expect(await screen.findByText("on it — will look today")).toBeTruthy();
    expect(screen.getByText(/can you review my PR\?/)).toBeTruthy();

    routes["GET /v1/representative/drafts"] = {
      status: 200,
      body: { items: [] },
    };
    fireEvent.click(screen.getByRole("button", { name: "Send draft to bob" }));
    expect(await screen.findByText("Nothing waiting.")).toBeTruthy();
    expect(activity).toHaveBeenCalled();
    const decision = calls.find(
      (c) => c.path === "/v1/representative/drafts/d1",
    );
    expect((decision?.body as { action: string }).action).toBe("send");
  });

  it("editing in place routes through the decision", async () => {
    routes["GET /v1/representative/drafts"] = {
      status: 200,
      body: { items: [SUGGESTION] },
    };
    routes["POST /v1/representative/drafts/d1"] = {
      status: 200,
      body: { ...SUGGESTION, status: "edited" },
    };
    render(<DraftsInbox onActivity={() => {}} onOpenThread={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Edit draft to bob"), {
      target: { value: "on it — tomorrow" },
    });
    routes["GET /v1/representative/drafts"] = {
      status: 200,
      body: { items: [] },
    };
    fireEvent.click(screen.getByRole("button", { name: "Send edited" }));
    await screen.findByText("Nothing waiting.");
    const decision = calls.find(
      (c) => c.path === "/v1/representative/drafts/d1",
    );
    expect(decision?.body).toEqual({
      action: "edit",
      text: "on it — tomorrow",
    });
  });
});

describe("FriendThread peer autonomy toggle", () => {
  it("mutes and unmutes auto-replies for this peer", async () => {
    routes["GET /v1/representative"] = {
      status: 200,
      body: { ...STATUS, mode: "auto" },
    };
    routes["GET /v1/friends/bob/messages"] = { status: 200, body: THREAD };
    routes["PUT /v1/representative/peers/bob"] = {
      status: 200,
      body: { ...STATUS, mode: "auto", muted_peers: ["bob"] },
    };
    render(<FriendThread peer="bob" onActivity={() => {}} />);
    const toggle = (await screen.findByLabelText(
      "Auto-replies to bob",
    )) as HTMLInputElement;
    expect(toggle.checked).toBe(true);

    fireEvent.click(toggle);
    const put = calls.find(
      (c) => c.method === "PUT" && c.path === "/v1/representative/peers/bob",
    );
    expect(put?.body).toEqual({ auto: false });
    // The server's answer (bob muted) is what the box now shows.
    await vi.waitFor(() => expect(toggle.checked).toBe(false));
  });
});

describe("RepresentativeSection", () => {
  it("renders the record and saves mode and about", async () => {
    routes["GET /v1/representative"] = { status: 200, body: STATUS };
    routes["PUT /v1/representative"] = {
      status: 200,
      body: { ...STATUS, mode: "auto" },
    };
    render(<RepresentativeSection />);
    expect(await screen.findByText("Representative")).toBeTruthy();
    expect(screen.getByText(/75% sent as written/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Representative mode"), {
      target: { value: "auto" },
    });
    const modePut = calls.find(
      (c) => c.method === "PUT" && c.path === "/v1/representative",
    );
    expect(modePut?.body).toEqual({ mode: "auto" });
    // Auto on but unearned: the pane says so instead of pretending.
    expect(
      await screen.findByText(/not yet earned/),
    ).toBeTruthy();

    fireEvent.change(screen.getByLabelText("About you"), {
      target: { value: "keeps it short" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      calls.some(
        (c) =>
          c.method === "PUT" &&
          c.path === "/v1/representative" &&
          (c.body as { about?: string }).about === "keeps it short",
      ),
    ).toBe(true);
  });

  it("disappears on hosts without the representative door", async () => {
    routes["GET /v1/representative"] = {
      status: 404,
      body: { error: { code: "not_found", message: "no door" } },
    };
    const { container } = render(<RepresentativeSection />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container.textContent).toBe("");
  });
});

describe("the OoLu window's quick toggle and filter strip", () => {
  it("turns the representative on, sweeps, and filters inline", async () => {
    routes["GET /v1/representative"] = {
      status: 200,
      body: { ...STATUS, mode: "off", drafts_pending: 0 },
    };
    routes["PUT /v1/representative"] = {
      status: 200,
      body: { ...STATUS, mode: "draft", drafts_pending: 0 },
    };
    routes["POST /v1/representative/sweep"] = {
      status: 200,
      body: { drafted: [SUGGESTION], pending: 1, model_error: null },
    };
    routes["GET /v1/representative/drafts"] = {
      status: 200,
      body: { items: [SUGGESTION] },
    };
    const { Life } = await import("./Life");
    render(<Life />);

    // Off: the toggle says so, and no strip crowds the chat.
    const toggle = await screen.findByRole("button", {
      name: "✍ Representative: off",
    });
    expect(screen.queryByText("Drafts awaiting your word")).toBeNull();

    fireEvent.click(toggle);
    // On: the PUT went out, the sweep drafted, and the strip appears
    // inline with the draft ready to filter.
    expect(
      await screen.findByRole("button", { name: "✍ Representative: on" }),
    ).toBeTruthy();
    const put = calls.find(
      (c) => c.method === "PUT" && c.path === "/v1/representative",
    );
    expect(put?.body).toEqual({ mode: "draft" });
    expect(
      calls.some(
        (c) => c.method === "POST" && c.path === "/v1/representative/sweep",
      ),
    ).toBe(true);
    expect(await screen.findByText("Drafts awaiting your word")).toBeTruthy();
    expect(screen.getByText("on it — will look today")).toBeTruthy();
  });
});

describe("SecuritySection: 2FA and order consent", () => {
  it("enrolls 2FA and authorizes a waiting order with amount + code", async () => {
    const { SecuritySection } = await import("./SettingsPane");
    routes["GET /v1/2fa"] = { status: 200, body: { enrolled: false } };
    routes["GET /v1/payment-authorizations"] = {
      status: 200,
      body: {
        items: [
          {
            auth_id: "o1",
            merchant: "Kifaru Books",
            amount_micros: 24990000,
            currency: "USD",
            description: "1 book",
            status: "pending",
          },
        ],
      },
    };
    routes["POST /v1/2fa/enroll"] = {
      status: 200,
      body: { secret: "ABCD2345", uri: "otpauth://totp/OoLu%3Aalice?..." },
    };
    routes["POST /v1/2fa/confirm"] = { status: 200, body: { enrolled: true } };
    routes["POST /v1/payment-authorizations/o1"] = {
      status: 200,
      body: { auth_id: "o1", status: "authorized" },
    };
    render(<SecuritySection />);

    expect(await screen.findByText("Kifaru Books")).toBeTruthy();
    expect(screen.getByText("USD 24.99")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Set up" }));
    expect(await screen.findByText("ABCD2345")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Authenticator code"), {
      target: { value: "123456" },
    });
    routes["GET /v1/2fa"] = { status: 200, body: { enrolled: true } };
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    const confirm = calls.find((c) => c.path === "/v1/2fa/confirm");
    expect((confirm?.body as { code: string }).code).toBe("123456");
    // 2FA now on: the Authorize button enables.
    await screen.findByText("On — orders can be authorized.");

    fireEvent.change(screen.getByLabelText("Re-enter the exact amount"), {
      target: { value: "24.99" },
    });
    fireEvent.change(
      screen.getByLabelText("Authenticator code — Kifaru Books"),
      { target: { value: "654321" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Authorize & pay" }));
    const authz = calls.find(
      (c) => c.path === "/v1/payment-authorizations/o1",
    );
    expect(authz?.body).toEqual({
      confirm_amount_micros: 24990000,
      code: "654321",
    });
  });

  it("disappears on a host without the 2FA door", async () => {
    const { SecuritySection } = await import("./SettingsPane");
    routes["GET /v1/2fa"] = {
      status: 404,
      body: { error: { code: "not_found", message: "no 2fa" } },
    };
    const { container } = render(<SecuritySection />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.textContent).toBe("");
  });
});
