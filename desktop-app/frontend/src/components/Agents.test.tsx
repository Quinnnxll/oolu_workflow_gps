import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AgentThread } from "./Agents";
import type { RosterAgent } from "../api";

// The press panel (A1): the shelf and the contribute form live at the
// head of the News thread; everything renders from the server's own
// words (taxonomy, license terms), and publishing walks the one door
// with consent carried explicitly.

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

const NEWS: RosterAgent = {
  agent_id: "news",
  name: "News",
  tagline: "Stories from members",
  scope: "I collect member contributions.",
  ahead: "Stories arrive in phase A2.",
  seat: "news.compose",
};

const GENRES = {
  status: 200,
  body: {
    taxonomy_version: 1,
    items: [
      { key: "local", label: "Around me", description: "Nearby happenings." },
      { key: "food", label: "Food", description: "Cooking and eating." },
    ],
    licenses: [
      {
        key: "oolu-members-1",
        name: "OoLu members license v1",
        terms: "Visible to members; credited; revocable-forward.",
      },
    ],
  },
};

describe("PressPanel (inside the News thread)", () => {
  it("renders the shelf with bylines and credits a retelling", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: {
        items: [
          {
            contribution_id: "c1",
            author: "bob",
            title: "The harbor market reopened",
            body: "Forty stalls this morning.",
            genres: ["local"],
            license: "oolu-members-1",
            media: [],
            similar_to: "c0",
            similarity: 0.7,
            taxonomy_version: 1,
            created_at: "2026-07-12T09:00:00Z",
            superseded_at: null,
          },
        ],
      },
    };
    routes["GET /v1/profiles/bob"] = {
      status: 200,
      body: { username: "bob", display_name: "Bob R.", has_photo: false },
    };
    render(<AgentThread agent={NEWS} />);

    expect(await screen.findByText("The harbor market reopened")).toBeTruthy();
    // The byline resolves the author's display name...
    expect(await screen.findByText("Bob R.")).toBeTruthy();
    // ...the genre chip speaks the taxonomy's label, not the key...
    expect(screen.getByText("Around me")).toBeTruthy();
    // ...and a flagged retelling credits the original, visibly.
    expect(screen.getByText("retells an earlier piece")).toBeTruthy();
    // Not my piece: no unpublish control.
    expect(screen.queryByText("Unpublish")).toBeNull();
  });

  it("publishes through the one door with consent carried explicitly", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/files"] = { status: 200, body: { items: [] } };
    routes["POST /v1/press/contributions"] = {
      status: 201,
      body: {
        contribution_id: "c9",
        author: "alice",
        title: "Rain reached the valley",
        body: "The terraces flooded with runoff.",
        genres: ["local"],
        license: "oolu-members-1",
        media: [],
        similar_to: null,
        similarity: null,
        taxonomy_version: 1,
        created_at: "2026-07-12T10:00:00Z",
        superseded_at: null,
      },
    };
    render(<AgentThread agent={NEWS} />);

    fireEvent.click(await screen.findByText("Write a piece"));
    // The consent line and the license terms are the server's own words.
    expect(screen.getByText("OoLu members license v1")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Title"), {
      target: { value: "Rain reached the valley" },
    });
    fireEvent.change(
      screen.getByPlaceholderText("What happened? Write it as you saw it."),
      { target: { value: "The terraces flooded with runoff." } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Around me" }));
    fireEvent.click(
      screen.getByLabelText(
        "I've read the license and consent to publish under it",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));

    expect(await screen.findByText("Rain reached the valley")).toBeTruthy();
    const post = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/press/contributions",
    );
    expect(post?.body).toMatchObject({
      title: "Rain reached the valley",
      genres: ["local"],
      license: "oolu-members-1",
      consent: true,
    });
  });

  it("shows the gate's refusal verbatim as the direction", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: { items: [] },
    };
    routes["GET /v1/files"] = { status: 200, body: { items: [] } };
    routes["POST /v1/press/contributions"] = {
      status: 400,
      body: {
        error: {
          code: "invalid_request",
          message: "publishing this would leak an email address — remove it",
        },
      },
    };
    render(<AgentThread agent={NEWS} />);
    fireEvent.click(await screen.findByText("Write a piece"));
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(
      await screen.findByText(/would leak an email address/),
    ).toBeTruthy();
  });

  it("offers unpublish only on your own pieces", async () => {
    localStorage.setItem("oolu_token", "tok");
    localStorage.setItem("oolu_principal", "alice");
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = {
      status: 200,
      body: {
        items: [
          {
            contribution_id: "c2",
            author: "alice",
            title: "My own piece",
            body: "Words of mine.",
            genres: ["food"],
            license: "oolu-members-1",
            media: [],
            similar_to: null,
            similarity: null,
            taxonomy_version: 1,
            created_at: "2026-07-12T09:00:00Z",
            superseded_at: null,
          },
        ],
      },
    };
    render(<AgentThread agent={NEWS} />);
    expect(await screen.findByText("My own piece")).toBeTruthy();
    fireEvent.click(screen.getByText("Unpublish"));
    const unpublish = calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === "/v1/press/contributions/c2/unpublish",
    );
    expect(unpublish).toBeTruthy();
  });

  it("stays silent on hosts from before the press", async () => {
    routes["GET /v1/press/genres"] = { status: 404, body: {} };
    render(<AgentThread agent={NEWS} />);
    // The agent's welcome still stands; no press chrome renders.
    expect(
      await screen.findByText(/I collect member contributions\./),
    ).toBeTruthy();
    expect(screen.queryByText("Contributions")).toBeNull();
  });
});
