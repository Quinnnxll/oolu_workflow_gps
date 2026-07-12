import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FriendThread } from "./Life";
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
