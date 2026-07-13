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

    // The list names the thread by keywords — never the whole sentence —
    // and keeps the full request as the tooltip.
    const entry = await screen.findByText("Convert Report Pdf");
    expect(entry.closest("button")?.title).toBe("convert report to pdf");
    fireEvent.click(entry);

    expect(await screen.findByText("run.submitted")).toBeTruthy();
    expect(screen.getByText("route.chosen")).toBeTruthy();
    // The thread header shows the keyword name plus the full request.
    expect(screen.getByText("“convert report to pdf”")).toBeTruthy();
    // The messy log is labelled as such, with OoLu as the human path.
    expect(screen.getByText(/ask OoLu/i)).toBeTruthy();
  });

  it("keeps the honest placeholder on hosts without a friends door", async () => {
    routes["GET /v1/friends"] = {
      status: 404,
      body: { error: { message: "friends live on a server" } },
    };
    render(<Life />);
    fireEvent.click(await screen.findByText("Friends need a server"));
    expect(
      await screen.findByText(/people and businesses will live here/i),
    ).toBeTruthy();
  });

  it("lists friend conversations with unread counts and opens the thread", async () => {
    routes["GET /v1/friends"] = {
      status: 200,
      body: {
        items: [
          {
            peer: "bob",
            last_text: "you there?",
            last_from: "bob",
            last_at: "2026-07-10T10:00:00Z",
            unread: 2,
          },
        ],
      },
    };
    routes["GET /v1/friends/bob/messages"] = {
      status: 200,
      body: {
        peer: "bob",
        items: [
          {
            message_id: "m1",
            from: "bob",
            text: "you there?",
            file_id: null,
            at: "2026-07-10T10:00:00Z",
            mine: false,
            read: true,
          },
        ],
      },
    };
    routes["POST /v1/friends/bob/messages"] = {
      status: 201,
      body: {
        message_id: "m2",
        from: "me",
        text: "here now!",
        file_id: null,
        at: "2026-07-10T10:01:00Z",
        mine: true,
        read: false,
      },
    };
    render(<Life />);

    // The peer list carries the unread count on the name.
    fireEvent.click(await screen.findByText("bob · 2 new"));
    // Opening the thread fetched (and thereby read) the messages.
    expect(await screen.findByText("you there?")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("Message bob…"), {
      target: { value: "here now!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("here now!")).toBeTruthy();
    const post = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/friends/bob/messages",
    );
    expect(post?.body).toEqual({ text: "here now!" });
  });

  it("starts a conversation by exact name — no directory to browse", async () => {
    routes["GET /v1/friends"] = { status: 200, body: { items: [] } };
    routes["GET /v1/friends/requests"] = { status: 200, body: { items: [] } };
    routes["POST /v1/friends/lookup"] = {
      status: 200,
      body: { username: "carol", relationship: "friends" },
    };
    routes["GET /v1/friends/carol/messages"] = {
      status: 200,
      body: { peer: "carol", items: [] },
    };
    render(<Life />);

    fireEvent.click(await screen.findByText("Start a conversation"));
    expect(await screen.findByText(/no directory to browse/i)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("username or e-mail"), {
      target: { value: "carol@mphepo.io" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));

    // An existing friend: Open chat leads into the thread.
    fireEvent.click(await screen.findByRole("button", { name: "Open chat" }));
    expect(
      await screen.findByPlaceholderText("Message carol…"),
    ).toBeTruthy();
    const lookup = calls.find((c) => c.path === "/v1/friends/lookup");
    expect(lookup?.body).toEqual({ query: "carol@mphepo.io" });
  });

  it("Friends and Noder fold away for a clear view, and stay folded", async () => {
    routes["GET /v1/runs"] = { status: 200, body: { items: [RUN] } };
    const { unmount } = render(<Life />);

    // Open by default, with the count on the Noder header.
    const noder = await screen.findByRole("button", { name: /Noder \(1\)/ });
    expect(await screen.findByText("Convert Report Pdf")).toBeTruthy();

    fireEvent.click(noder);
    expect(screen.queryByText("Convert Report Pdf")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Friends/ }));
    expect(screen.queryByText("Start a conversation")).toBeNull();

    // The folded state survives a remount (it lives in localStorage).
    unmount();
    render(<Life />);
    expect(screen.queryByText("Start a conversation")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Friends/ }));
    expect(await screen.findByText("Start a conversation")).toBeTruthy();
  });

  it("keeps Settings right below Files, above the conversations", async () => {
    routes["GET /v1/settings"] = { status: 200, body: { items: [] } };
    routes["GET /v1/runs"] = { status: 200, body: { items: [RUN] } };
    const { container } = render(<Life />);
    const entry = screen
      .getByText("Settings", { selector: ".convo-name" })
      .closest("button");
    // Directly after Files (OoLu, Files, Settings), so a long Friends or
    // Noder list can never push it below the fold.
    const rows = Array.from(container.querySelectorAll("aside .convo"));
    const names = rows.map(
      (r) => r.querySelector(".convo-name")?.textContent ?? "",
    );
    expect(names.indexOf("Settings")).toBe(names.indexOf("Files") + 1);

    fireEvent.click(entry as HTMLElement);
    expect(await screen.findByText("Settings", { selector: "div" })).toBeTruthy();
  });
});

describe("NoderThread", () => {
  it("is a record, not a control panel — rerunning is asked of OoLu", async () => {
    routes["GET /v1/runs/r1/audit"] = { status: 200, body: { entries: [] } };
    render(<NoderThread run={RUN} />);

    // No Run again button: a button-made rerun minted a fresh entry with
    // a fresh id, orphaning the history. The hint points at the chat,
    // where OoLu re-fires the SAME task through its own route and node.
    expect(screen.queryByRole("button", { name: "Run again" })).toBeNull();
    expect(
      await screen.findByText(/To run this again,\s*ask OoLu in the chat/),
    ).toBeTruthy();
    // Nothing was submitted by merely looking at the record.
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/runs")).toBe(
      false,
    );
  });
});

describe("the foldable list and the one-pane phone flow", () => {
  it("folds the list for a wide conversation window, and remembers", async () => {
    routes["GET /v1/work/nodes"] = { status: 200, body: { items: [] } };
    const { container } = render(<Life />);
    const life = container.querySelector(".life")!;
    expect(life.className).not.toContain("sidebar-folded");

    fireEvent.click(screen.getByRole("button", { name: "Hide the list" }));
    expect(life.className).toContain("sidebar-folded");
    expect(localStorage.getItem("oolu_sidebar_folded")).toBe("1");

    // ☰ brings it back.
    fireEvent.click(screen.getByRole("button", { name: "Show the list" }));
    expect(life.className).not.toContain("sidebar-folded");

    // The choice survives a remount.
    localStorage.setItem("oolu_sidebar_folded", "1");
    cleanup();
    const again = render(<Life />).container.querySelector(".life")!;
    expect(again.className).toContain("sidebar-folded");
  });

  it("opening a conversation opens the pane; back returns to the list", async () => {
    const { container } = render(<Life />);
    const life = container.querySelector(".life")!;
    expect(life.className).not.toContain("pane-open");

    fireEvent.click(screen.getByRole("button", { name: /Files/ }));
    expect(life.className).toContain("pane-open");

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(life.className).not.toContain("pane-open");
  });
});

describe("StartConversation: friend requests, not messages", () => {
  it("finds someone and sends a request instead of a message", async () => {
    const { StartConversation } = await import("./Life");
    routes["GET /v1/friends/requests"] = { status: 200, body: { items: [] } };
    routes["POST /v1/friends/lookup"] = {
      status: 200,
      body: { username: "bob", relationship: "none" },
    };
    routes["POST /v1/friends/requests"] = {
      status: 200,
      body: { username: "bob", relationship: "pending_out" },
    };
    render(<StartConversation onOpen={() => {}} />);

    fireEvent.change(screen.getByLabelText("username or e-mail"), {
      target: { value: "bob" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Send friend request" }),
    );
    await vi.waitFor(() =>
      expect(
        calls.find(
          (c) => c.method === "POST" && c.path === "/v1/friends/requests",
        ),
      ).toBeTruthy(),
    );
    const sent = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/friends/requests",
    );
    expect((sent?.body as { username: string }).username).toBe("bob");
    // A request, never a message: no thread was opened.
    expect(
      calls.some((c) => c.path.includes("/messages")),
    ).toBe(false);
  });

  it("shows incoming requests and accepts one", async () => {
    const { StartConversation } = await import("./Life");
    routes["GET /v1/friends/requests"] = {
      status: 200,
      body: { items: ["carol"] },
    };
    routes["POST /v1/friends/requests/carol"] = {
      status: 200,
      body: { username: "carol", relationship: "friends" },
    };
    render(<StartConversation onOpen={() => {}} />);

    expect(await screen.findByText("carol")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await vi.waitFor(() =>
      expect(
        calls.find((c) => c.path === "/v1/friends/requests/carol"),
      ).toBeTruthy(),
    );
    const decided = calls.find(
      (c) => c.path === "/v1/friends/requests/carol",
    );
    expect((decided?.body as { action: string }).action).toBe("accept");
  });
});
