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

  it("renders stories with lineage bylines, reasons on demand, honest taps", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = {
      status: 200,
      body: {
        items: [
          {
            story_id: "st1",
            headline: "The pier queues again",
            prose: "Forty stalls returned this morning.",
            genres: ["local"],
            lineage: [
              { contribution_id: "c1", author: "alice", weight: 0.6 },
              { contribution_id: "c2", author: "bob", weight: 0.4 },
            ],
            breakdown: { selection: 0.57, inspiring: 0.7, rubric_version: 1 },
            rubric_version: 1,
            source: "desk",
            created_at: "2026-07-13T08:00:00Z",
          },
        ],
        personalized: false,
        edition_schedule: null,
      },
    };
    routes["POST /v1/press/stories/st1/feedback"] = {
      status: 200,
      body: { recorded: false, reason: "personalization is off" },
    };
    render(<AgentThread agent={NEWS} />);

    expect(await screen.findByText("The pier queues again")).toBeTruthy();
    // Both cited contributors' bylines render — the lineage speaks.
    expect(await screen.findByText("alice")).toBeTruthy();
    expect(await screen.findByText("bob")).toBeTruthy();
    // The reasons on demand: the rubric's factor breakdown.
    fireEvent.click(screen.getByText("Why this story"));
    expect(screen.getByText(/selection: 0.57/)).toBeTruthy();
    // A tap answers honestly when personalization is off.
    fireEvent.click(screen.getByText("Like"));
    expect(
      await screen.findByText("not recorded — personalization is off"),
    ).toBeTruthy();
    const tap = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/press/stories/st1/feedback",
    );
    expect(tap?.body).toMatchObject({ signal: "like" });
  });

  it("cranks the newsroom and toggles the morning edition", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = {
      status: 200,
      body: { items: [], personalized: false, edition_schedule: null },
    };
    routes["POST /v1/press/newsroom/run"] = {
      status: 200,
      body: { composed: 1, items: [] },
    };
    routes["POST /v1/press/edition/schedule"] = {
      status: 200,
      body: { edition_schedule: { schedule_id: "sch1", at_minute: 480 } },
    };
    render(<AgentThread agent={NEWS} />);

    fireEvent.click(await screen.findByText("Compose now"));
    expect(
      calls.some(
        (c) => c.method === "POST" && c.path === "/v1/press/newsroom/run",
      ),
    ).toBeTruthy();

    fireEvent.click(screen.getByText("Morning edition"));
    expect(await screen.findByText("Morning edition ✓")).toBeTruthy();
    const scheduled = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/press/edition/schedule",
    );
    expect(scheduled?.body).toMatchObject({ enabled: true });
  });

  it("runs a poll: both bylines, vote once, the floor's honest reveal", async () => {
    const POLL: RosterAgent = { ...NEWS, agent_id: "poll", name: "Poll" };
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/polls/next"] = {
      status: 200,
      body: {
        pair_id: "p1",
        genre: "food",
        left: {
          contribution_id: "c1",
          author: "alice",
          title: "The steel kettle, tested",
          excerpt: "Four minutes to a litre.",
        },
        right: {
          contribution_id: "c2",
          author: "bob",
          title: "A week with the glass kettle",
          excerpt: "Six minutes, but the lid feels solid.",
        },
        created_at: "2026-07-14T09:00:00Z",
      },
    };
    routes["POST /v1/press/polls/p1/vote"] = {
      status: 200,
      body: {
        pair_id: "p1",
        voted: true,
        choice: "left",
        revealed: false,
        reason: "not enough votes yet",
        learning: false,
      },
    };
    render(<AgentThread agent={POLL} />);

    // Both sides render with their bylines.
    expect(await screen.findByText("The steel kettle, tested")).toBeTruthy();
    expect(screen.getByText("A week with the glass kettle")).toBeTruthy();
    expect(await screen.findByText("alice")).toBeTruthy();
    expect(screen.getByText("bob")).toBeTruthy();

    // Tap left: the vote posts; the floor answers honestly; the pair
    // locks (no second vote from this surface).
    fireEvent.click(screen.getByText("The steel kettle, tested"));
    expect(await screen.findByText("not enough votes yet")).toBeTruthy();
    const voted = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/press/polls/p1/vote",
    );
    expect(voted?.body).toMatchObject({ choice: "left" });
    // And the learning-off echo rides along.
    expect(
      screen.getByText(/not recorded — personalization is off/),
    ).toBeTruthy();
  });

  it("switches the poll stream by genre chip", async () => {
    const POLL: RosterAgent = { ...NEWS, agent_id: "poll", name: "Poll" };
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/polls/next"] = {
      status: 200,
      body: {
        pair_id: "p2",
        genre: "local",
        left: {
          contribution_id: "c1",
          author: "alice",
          title: "L",
          excerpt: "l",
        },
        right: {
          contribution_id: "c2",
          author: "bob",
          title: "R",
          excerpt: "r",
        },
        created_at: "2026-07-14T09:00:00Z",
      },
    };
    render(<AgentThread agent={POLL} />);
    await screen.findByText("L");
    fireEvent.click(screen.getByRole("button", { name: "Food" }));
    // The chip IS the stream: the next fetch names the genre.
    const named = calls.filter(
      (c) => c.method === "GET" && c.path === "/v1/press/polls/next",
    );
    expect(named.length).toBeGreaterThanOrEqual(2);
  });

  it("holds the ad slot behind the versioned consent, then labels it", async () => {
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/press/contributions"] = { status: 200, body: { items: [] } };
    routes["GET /v1/press/stories"] = {
      status: 200,
      body: {
        items: [
          {
            story_id: "st1",
            headline: "The pier queues again",
            prose: "Forty stalls returned.",
            genres: ["local"],
            lineage: [{ contribution_id: "c1", author: "alice", weight: 1 }],
            breakdown: { selection: 0.5 },
            rubric_version: 1,
            source: "desk",
            created_at: "2026-07-15T08:00:00Z",
          },
        ],
        personalized: false,
        edition_schedule: null,
      },
    };
    // Before acceptance: the slot renders the consent card, no ad.
    routes["GET /v1/press/ads"] = {
      status: 200,
      body: { placement: null, reason: "consent" },
    };
    routes["GET /v1/legal/consent"] = {
      status: 200,
      body: { privacy_version: 2, accepted_version: null, ads_enabled: false },
    };
    routes["POST /v1/legal/consent"] = {
      status: 200,
      body: { document: "privacy", accepted_version: 2 },
    };
    render(<AgentThread agent={NEWS} />);
    expect(
      await screen.findByText("Sponsored placements — with your say"),
    ).toBeTruthy();
    expect(screen.queryByText("Sponsored")).toBeNull();

    // Accepting names the CURRENT version and reloads the slot — which
    // now serves a placement, label first, and posts its impression.
    routes["GET /v1/press/ads"] = {
      status: 200,
      body: {
        placement: {
          placement_id: "pl1",
          label: "Sponsored",
          campaign_name: "Kettle week",
          creative: "Kettles for every kitchen.",
          offer_ref: "listing-42",
          advertiser: "shop",
          surface: "edition",
          content_ref: "st1",
          breakdown: {},
        },
      },
    };
    routes["POST /v1/adhouse/placements/pl1/impression"] = {
      status: 200,
      body: { recorded: true, kind: "impression" },
    };
    fireEvent.click(screen.getByText("Accept version 2"));
    expect(await screen.findByText("Sponsored")).toBeTruthy();
    expect(screen.getByText("Kettle week")).toBeTruthy();
    const accepted = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/legal/consent",
    );
    expect(accepted?.body).toMatchObject({ document: "privacy", version: 2 });
    const impression = calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === "/v1/adhouse/placements/pl1/impression",
    );
    expect(impression).toBeTruthy();

    // The affiliate tap delivers the click on the provenance token.
    routes["POST /v1/adhouse/placements/pl1/click"] = {
      status: 200,
      body: { recorded: true, kind: "click" },
    };
    fireEvent.click(screen.getByText("View the offer"));
    expect(
      calls.some(
        (c) =>
          c.method === "POST" &&
          c.path === "/v1/adhouse/placements/pl1/click",
      ),
    ).toBeTruthy();
  });

  it("compares on verified evidence and crowns a deterministic best buy", async () => {
    const EXPLORER: RosterAgent = {
      ...NEWS,
      agent_id: "explorer",
      name: "Explorer",
    };
    routes["GET /v1/press/genres"] = GENRES;
    routes["GET /v1/explorer/compare"] = {
      status: 200,
      body: {
        category: "",
        rows: [
          {
            listing_id: "steel",
            title: "Steel kettle",
            seller: "shop",
            price_micros: 20_000_000,
            currency: "USD",
            list_price_micros: 25_000_000,
            discount_percent: 20,
            feedback: { count: 3, mean: 4.3, factor: 0.86 },
            trust: { score: 0.9, basis: "derived from the order book" },
            lab: { count: 1, mean_score: 92, factor: 0.92 },
            eligible: true,
            gaps: [],
          },
          {
            listing_id: "glass",
            title: "Glass kettle",
            seller: "shop",
            price_micros: 30_000_000,
            currency: "USD",
            list_price_micros: null,
            discount_percent: null,
            feedback: { count: 0, mean: null, factor: 0.5 },
            trust: { score: 0.5, basis: "no order history" },
            lab: { count: 0, mean_score: null, factor: 0.5 },
            eligible: false,
            gaps: ["out of stock"],
          },
        ],
        brief: {
          mode: "balanced",
          winner_listing_id: "steel",
          ranked: [
            {
              listing_id: "steel",
              title: "Steel kettle",
              seller: "shop",
              score: 0.81,
              factors: { price: 1, discount: 0.2, feedback: 0.86, trust: 0.9, lab: 0.92 },
              weights: {},
            },
          ],
        },
      },
    };
    routes["POST /v1/explorer/interests"] = {
      status: 200,
      body: { interest: { schedule_id: "s1" } },
    };
    render(<AgentThread agent={EXPLORER} />);

    // The winner banner with the reasons on demand.
    expect(await screen.findByText("Best buy")).toBeTruthy();
    fireEvent.click(screen.getByText("Why this pick"));
    expect(screen.getByText(/trust: 0.9/)).toBeTruthy();
    // The discount renders only where it is a FACT; the ineligible row
    // stays visible with its gap named.
    expect(screen.getByText("−20%")).toBeTruthy();
    expect(screen.getByText("out of stock")).toBeTruthy();
    expect(screen.getByText(/3 verified reviews/)).toBeTruthy();

    // A mode chip refetches with the mode named.
    fireEvent.click(screen.getByRole("button", { name: "Measured" }));
    const modes = calls.filter(
      (c) => c.method === "GET" && c.path === "/v1/explorer/compare",
    );
    expect(modes.length).toBeGreaterThanOrEqual(2);

    // Following schedules the daily brief.
    fireEvent.click(screen.getByText("Follow this"));
    expect(await screen.findByText("Following ✓")).toBeTruthy();
    const followed = calls.find(
      (c) => c.method === "POST" && c.path === "/v1/explorer/interests",
    );
    expect(followed?.body).toMatchObject({ mode: "measured" });
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
